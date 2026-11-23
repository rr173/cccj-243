"""Tests for the one-shot multi-batch merge entry point.

The tests cover identity-domain construction, whitelist validation, the
rewrite discipline (injectivity, locked values, nulls/missing fields),
cross-batch conflict witnesses, determinism, the independent pack auditor and
CLI behaviour.  An independent naive cross-batch checker re-derives the safety
properties from the packaged rows without sharing code with the builder.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen import merge_cli  # noqa: E402
from fixturegen.contract import parse_contract  # noqa: E402
from fixturegen.engine import (  # noqa: E402
    evaluate_rules,
    verify_fixture,
)
from fixturegen.merge import (  # noqa: E402
    MergeConflictError,
    MergeInputError,
    build_identity_domains,
    merge_fixtures,
    parse_whitelist,
    verify_pack,
    write_pack_atomic,
)
from tests.helpers import contract, slot, table  # noqa: E402


# ---------------------------------------------------------------------------
# fixture builders


def fixture_doc(c, choices, verify=True):
    """Build a verified fixture/1 document from a slot->candidate_id choice."""
    assignment = {}
    selection = {}
    for s in c.slots:
        candidate = next(x for x in s.candidates
                         if x.candidate_id == choices[s.id])
        assignment[s.id] = candidate
        selection[s.id] = {"candidate_id": candidate.candidate_id,
                           "row": dict(candidate.row)}
    doc = {
        "schema": "fixture/1",
        "kind": "normal",
        "target": None,
        "slots": selection,
        "rules": evaluate_rules(c, assignment),
    }
    if verify:
        assert verify_fixture(c, doc)["ok"]
    return doc


def isolated_doc(c, choices, target):
    doc = fixture_doc(c, choices, verify=False)
    doc["kind"] = "isolated"
    doc["target"] = target
    assert verify_fixture(c, doc)["ok"]
    return doc


def wl(fields):
    return {"schema": "rewrite-whitelist/1",
            "fields": [{"table": t, "field": f} for t, f in fields]}


# ---------------------------------------------------------------------------
# independent naive cross-batch checks over the final packaged rows


def naive_pack_rows(pack, c):
    """batch_id -> slot_id -> Candidate-like row dict (only present fields)."""
    out = {}
    for table_name, entries in pack["tables"].items():
        for entry in entries:
            bid = entry["source"]["batch_id"]
            sid = entry["source"]["slot_id"]
            out.setdefault(bid, {})[sid] = entry["row"]
    return out


def naive_get(row, field):
    from tests.helpers import _MISSING
    return row[field] if field in row else _MISSING


def naive_key(row, fields):
    values = []
    for f in fields:
        v = naive_get(row, f)
        from tests.helpers import _MISSING
        if v is _MISSING:
            return None
        values.append(v)
    return tuple(values)


def naive_ref(row, fields):
    values = []
    for f in fields:
        v = naive_get(row, f)
        from tests.helpers import _MISSING
        if v is _MISSING:
            return ("missing", ())
        values.append(v)
    if all(v is not None for v in values):
        return ("value", tuple(values))
    if all(v is None for v in values):
        return ("null", ())
    return ("partial", ())


def naive_cross_batch_violations(pack, c):
    """Every cross-batch equality the merge must rule out (naive product)."""
    rows = naive_pack_rows(pack, c)
    batches = sorted(rows)
    violations = []
    slots_by_table = {}
    for s in c.slots:
        slots_by_table.setdefault(s.table, []).append(s.id)

    for rule in c.rules:
        p = rule.params
        if rule.kind == "unique":
            indexed = {}
            for bid in batches:
                for sid in slots_by_table.get(p["table"], []):
                    key = naive_key(rows[bid][sid], p["key"])
                    if key is None:
                        continue
                    indexed.setdefault(key, []).append((bid, sid))
            for key, hits in indexed.items():
                if len({bid for bid, _ in hits}) > 1:
                    violations.append((rule.id, key, hits))
        else:
            parents = {}
            for bid in batches:
                for sid in slots_by_table.get(p["parent"], []):
                    key = naive_key(rows[bid][sid], p["parent_key"])
                    if key is not None:
                        parents.setdefault(key, []).append((bid, sid))
            for bid in batches:
                for sid in slots_by_table.get(p["child"], []):
                    status, values = naive_ref(rows[bid][sid],
                                               p["child_fields"])
                    if status != "value":
                        continue
                    for parent in parents.get(values, ()):
                        if parent[0] != bid:
                            violations.append(
                                (rule.id, values, [(bid, sid), parent]))
    return violations


def naive_per_batch_truth(pack, doc):
    """Independent rule truth per batch using the raw helper evaluator."""
    from tests.helpers import naive_all_truth
    rows = naive_pack_rows(pack, None)
    return {bid: naive_all_truth(doc, rows[bid]) for bid in sorted(rows)}


# ---------------------------------------------------------------------------
# contracts used across tests


def transitive_doc():
    """DETAIL.code joins SETTLEMENT.code and REPORT.code in one domain."""
    return contract(
        [
            table("SETTLEMENT", "parent",
                  [("code", "str", False), ("total", "int", False)], ["code"]),
            table("REPORT", "parent",
                  [("code", "str", False), ("region", "str", True)], ["code"]),
            table("DETAIL", "child",
                  [("code", "str", False), ("amt", "int", False)]),
        ],
        [
            slot("s1", "SETTLEMENT", [("a", {"code": "X", "total": 10})]),
            slot("s2", "SETTLEMENT", [("a", {"code": "Y", "total": 5})]),
            slot("r1", "REPORT", [("a", {"code": "X", "region": "north"})]),
            slot("r2", "REPORT", [("a", {"code": "Y", "region": "south"})]),
            slot("d1", "DETAIL", [("a", {"code": "X", "amt": 10})]),
            slot("d2", "DETAIL", [("a", {"code": "Y", "amt": 5})]),
        ],
        [
            {"id": "U-s", "kind": "unique", "table": "SETTLEMENT",
             "key": ["code"]},
            {"id": "U-r", "kind": "unique", "table": "REPORT",
             "key": ["code"]},
            {"id": "F-s", "kind": "fk", "child": "DETAIL",
             "child_fields": ["code"], "parent": "SETTLEMENT",
             "parent_key": ["code"]},
            {"id": "F-r", "kind": "fk", "child": "DETAIL",
             "child_fields": ["code"], "parent": "REPORT",
             "parent_key": ["code"]},
            {"id": "A-s", "kind": "aggregate", "child": "DETAIL",
             "child_fields": ["code"], "child_amount": "amt",
             "parent": "SETTLEMENT", "parent_key": ["code"],
             "parent_amount": "total", "reference_rule": "F-s"},
        ],
    )


ALL_CODE_FIELDS = [
    ("SETTLEMENT", "code"), ("REPORT", "code"), ("DETAIL", "code")]


class IdentityDomainTests(unittest.TestCase):
    def test_transitive_domain_merges_three_columns(self):
        c = parse_contract(transitive_doc())
        domains = {d.domain_id: set(d.columns)
                   for d in build_identity_domains(c)}
        self.assertEqual(len(domains), 1)
        self.assertEqual(set(domains["D01"]), set(ALL_CODE_FIELDS))

    def test_unconnected_key_columns_are_separate_domains(self):
        doc = contract(
            [
                table("P", "parent",
                      [("a", "str", False), ("b", "str", False)], ["a", "b"]),
                table("C", "child",
                      [("x", "str", False), ("y", "str", False)]),
            ],
            [
                slot("p", "P", [("k", {"a": "1", "b": "2"})]),
                slot("c", "C", [("k", {"x": "1", "y": "2"})]),
            ],
            [
                {"id": "U", "kind": "unique", "table": "P",
                 "key": ["a", "b"]},
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["x"], "parent": "P",
                 "parent_key": ["a"]},
            ],
        )
        c = parse_contract(doc)
        domains = {frozenset(d.columns) for d in build_identity_domains(c)}
        # x<->a (and the U-rule node a) share one domain; b is its own.
        self.assertIn(frozenset({("P", "a"), ("C", "x")}), domains)
        self.assertIn(frozenset({("P", "b")}), domains)
        self.assertNotIn(("C", "y"),
                         {col for d in domains for col in d})

    def test_composite_positions_are_matched_pairwise(self):
        doc = contract(
            [
                table("P", "parent",
                      [("a", "str", False), ("b", "str", False)], ["a", "b"]),
                table("C", "child",
                      [("x", "str", False), ("y", "str", False)]),
            ],
            [
                slot("p", "P", [("k", {"a": "1", "b": "2"})]),
                slot("c", "C", [("k", {"x": "1", "y": "2"})]),
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["x", "y"], "parent": "P",
                 "parent_key": ["a", "b"]},
            ],
        )
        c = parse_contract(doc)
        groups = {frozenset(d.columns) for d in build_identity_domains(c)}
        self.assertIn(frozenset({("P", "a"), ("C", "x")}), groups)
        self.assertIn(frozenset({("P", "b"), ("C", "y")}), groups)


class WhitelistTests(unittest.TestCase):
    def setUp(self):
        self.c = parse_contract(transitive_doc())

    def test_non_string_field_rejected(self):
        with self.assertRaises(MergeInputError):
            parse_whitelist(wl([("SETTLEMENT", "total")]), self.c)

    def test_non_rule_field_rejected(self):
        doc = transitive_doc()
        doc["tables"][2]["fields"].append(
            {"name": "memo", "type": "str", "nullable": True})
        doc["slots"][4]["candidates"][0]["row"]["memo"] = "note"
        c = parse_contract(doc)
        with self.assertRaises(MergeInputError) as ctx:
            parse_whitelist(wl([("DETAIL", "memo")]), c)
        self.assertIn("does not participate in any rule key",
                      str(ctx.exception))

    def test_unknown_table_field_and_bad_schema_rejected(self):
        with self.assertRaises(MergeInputError):
            parse_whitelist({"schema": "nope", "fields": []}, self.c)
        with self.assertRaises(MergeInputError):
            parse_whitelist(wl([("GHOST", "code")]), self.c)
        with self.assertRaises(MergeInputError):
            parse_whitelist(wl([("DETAIL", "ghost")]), self.c)

    def test_duplicate_entry_rejected(self):
        with self.assertRaises(MergeInputError):
            parse_whitelist(wl([("DETAIL", "code"), ("DETAIL", "code")]),
                            self.c)

    def test_empty_whitelist_is_valid(self):
        self.assertEqual(parse_whitelist(wl([]), self.c), ())


class MergeSuccessTests(unittest.TestCase):
    def setUp(self):
        self.doc = transitive_doc()
        self.c = parse_contract(self.doc)
        choices = {s.id: s.candidates[0].candidate_id for s in self.c.slots}
        # Two batches with identical hot codes.
        self.b1 = fixture_doc(self.c, choices)
        self.b2 = fixture_doc(self.c, choices)
        self.pack = merge_fixtures(
            self.c, [("B1", self.b1), ("B2", self.b2)],
            wl(ALL_CODE_FIELDS))

    def test_pack_shape_and_provenance(self):
        self.assertEqual(self.pack["schema"], "merge-pack/1")
        self.assertEqual(sorted(e["source"]["batch_id"] for e
                                in self.pack["tables"]["DETAIL"]),
                         ["B1", "B1", "B2", "B2"])
        # one row per batch per slot, every row source recoverable
        for table_name, entries in self.pack["tables"].items():
            self.assertEqual(len(entries), 2 * len(
                [s for s in self.c.slots if s.table == table_name]))
            sources = {(e["source"]["batch_id"], e["source"]["slot_id"])
                       for e in entries}
            for bid in ("B1", "B2"):
                for s in self.c.slots:
                    if s.table == table_name:
                        self.assertIn((bid, s.id), sources)

    def test_renames_are_injective_and_consistent_across_three_tables(self):
        # The same original X in one batch maps identically in all three
        # columns even though the two FKs do not share a parent table.
        per = {}
        for item in self.pack["rewrites"]:
            per.setdefault((item["batch_id"], item["domain_id"]), {})[
                item["original"]] = item["replacement"]
        for bid in ("B1", "B2"):
            maps = per[(bid, "D01")]
            self.assertEqual(len(set(maps.values())), len(maps))
            for entries in self.pack["tables"].values():
                for entry in entries:
                    if entry["source"]["batch_id"] != bid:
                        continue
                    row = entry["row"]
                    if "code" in row:
                        self.assertEqual(row["code"], maps[
                            self.b1["slots"][entry["source"]["slot_id"]]
                            ["row"]["code"]])

    def test_replacements_globally_unique_and_disjoint_from_originals(self):
        originals = {v for f in self.b1["slots"].values()
                     for k, v in f["row"].items() if isinstance(v, str)}
        replacements = [item["replacement"] for item in self.pack["rewrites"]]
        self.assertEqual(len(replacements), len(set(replacements)))
        self.assertFalse(originals.intersection(replacements))

    def test_naive_cross_batch_check_is_clean(self):
        self.assertEqual(
            naive_cross_batch_violations(self.pack, self.c), [])

    def test_per_batch_rule_truth_preserved_naively(self):
        truth = naive_per_batch_truth(self.pack, self.doc)
        for bid in ("B1", "B2"):
            self.assertTrue(all(truth[bid].values()))

    def test_integers_and_non_key_fields_untouched(self):
        for entry in self.pack["tables"]["SETTLEMENT"]:
            self.assertEqual(entry["row"]["total"],
                             self.b1["slots"][entry["source"]["slot_id"]]
                             ["row"]["total"])

    def test_independent_auditor_accepts_pack(self):
        self.assertTrue(verify_pack(self.c, self.pack)["ok"])

    def test_input_order_does_not_change_pack(self):
        swapped = merge_fixtures(
            self.c, [("B2", self.b2), ("B1", self.b1)],
            wl(ALL_CODE_FIELDS))
        self.assertEqual(swapped, self.pack)

    def test_tampered_pack_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["tables"]["DETAIL"][0]["row"]["amt"] = 9999
        result = verify_pack(self.c, bad)
        self.assertFalse(result["ok"])
        self.assertTrue(any("does not follow" in e for e in result["errors"]))

    def test_tampered_rewrite_ledger_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["rewrites"][0]["replacement"] = "X"  # collides with an original
        result = verify_pack(self.c, bad)
        self.assertFalse(result["ok"])

    def test_rewrite_naming_unknown_batch_or_domain_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["rewrites"][0]["batch_id"] = "GHOST"
        self.assertFalse(verify_pack(self.c, bad)["ok"])
        bad = copy.deepcopy(self.pack)
        bad["rewrites"][0]["domain_id"] = "D99"
        self.assertFalse(verify_pack(self.c, bad)["ok"])

    def test_extra_field_in_pack_row_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["tables"]["SETTLEMENT"][0]["row"]["injected"] = "boom"
        self.assertFalse(verify_pack(self.c, bad)["ok"])

    def test_phantom_batch_row_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["tables"]["SETTLEMENT"][0]["source"]["batch_id"] = "GHOST"
        self.assertFalse(verify_pack(self.c, bad)["ok"])

    def test_wrong_contract_id_rejected(self):
        bad = copy.deepcopy(self.pack)
        bad["contract_id"] = "other"
        self.assertFalse(verify_pack(self.c, bad)["ok"])


    def test_three_batches_merge_and_stay_separable(self):
        doc = transitive_doc()
        c = parse_contract(doc)
        base = {s.id: s.candidates[0].candidate_id for s in c.slots}
        # second candidate on the X slots for batch 3
        for index in (0, 2, 4):
            doc["slots"][index]["candidates"].append(
                {"candidate_id": "b",
                 "row": dict(doc["slots"][index]["candidates"][0]["row"],
                             **{"code": "Q"})})
        c = parse_contract(doc)
        b1 = fixture_doc(c, base)
        b2 = fixture_doc(c, base)
        b3_choices = dict(base)
        for index in (0, 2, 4):
            b3_choices[c.slots[index].id] = "b"
        b3 = fixture_doc(c, b3_choices)
        pack = merge_fixtures(
            c, [("B1", b1), ("B2", b2), ("B3", b3)],
            wl(ALL_CODE_FIELDS))
        self.assertTrue(verify_pack(c, pack)["ok"])
        self.assertEqual(naive_cross_batch_violations(pack, c), [])
        # every batch/slot pair is retrievable from the packaged rows
        found = {(e["source"]["batch_id"], e["source"]["slot_id"])
                 for entries in pack["tables"].values() for e in entries}
        for bid in ("B1", "B2", "B3"):
            for s in c.slots:
                self.assertIn((bid, s.id), found)
        # B1 and B2 share the same originals but receive distinct replacements
        b1_codes = {r["replacement"] for r in pack["rewrites"]
                    if r["batch_id"] == "B1"}
        b2_codes = {r["replacement"] for r in pack["rewrites"]
                    if r["batch_id"] == "B2"}
        self.assertFalse(b1_codes & b2_codes)
        # arbitrary reordering of the three inputs is byte-identical
        reordered = merge_fixtures(
            c, [("B3", b3), ("B1", b1), ("B2", b2)],
            wl(ALL_CODE_FIELDS))
        self.assertEqual(reordered, pack)


class LockedValueTests(unittest.TestCase):
    def setUp(self):
        # DETAIL.code is deliberately NOT whitelisted; its values lock the
        # whole transitive domain per batch.
        self.c = parse_contract(transitive_doc())
        choices = {s.id: s.candidates[0].candidate_id for s in self.c.slots}
        self.b1 = fixture_doc(self.c, choices)
        self.b2 = fixture_doc(self.c, choices)

    def test_locked_value_is_kept_everywhere_and_conflict_reported(self):
        with self.assertRaises(MergeConflictError) as ctx:
            merge_fixtures(
                self.c, [("B1", self.b1), ("B2", self.b2)],
                wl([("SETTLEMENT", "code"), ("REPORT", "code")]))
        rule_ids = {w["rule_id"] for w in ctx.exception.witnesses}
        self.assertIn("U-s", rule_ids)
        for witness in ctx.exception.witnesses:
            self.assertEqual(
                {witness["a"]["batch_id"], witness["b"]["batch_id"]},
                {"B1", "B2"})
            self.assertTrue(witness["fixed_key"])

    def test_locking_one_value_keeps_other_values_rewritable(self):
        # Layout per batch:
        #   s1/r1/d1 carry X, s2/r2/d2 carry Y, both FKs and the aggregate
        #   hold.  An extra parent-only slot s3 carries FREE, which no child
        #   references.  DETAIL.code is NOT whitelisted, so X/Y are locked
        #   (they appear in the child column), while FREE appears only on the
        #   whitelisted parent column and stays free.  B2 renames its shared
        #   X/Y picks to W/V to avoid any fixed cross-batch equality.
        doc = contract(
            [
                table("SETTLEMENT", "parent",
                      [("code", "str", False), ("total", "int", False)],
                      ["code"]),
                table("REPORT", "parent",
                      [("code", "str", False), ("region", "str", True)],
                      ["code"]),
                table("DETAIL", "child",
                      [("code", "str", False), ("amt", "int", False)]),
            ],
            [
                slot("s1", "SETTLEMENT",
                     [("a", {"code": "X", "total": 10}),
                      ("b", {"code": "W", "total": 10})]),
                slot("s2", "SETTLEMENT",
                     [("a", {"code": "Y", "total": 5}),
                      ("b", {"code": "V", "total": 5})]),
                slot("s3", "SETTLEMENT",
                     [("a", {"code": "FREE", "total": 0})]),
                slot("r1", "REPORT",
                     [("a", {"code": "X", "region": "n"}),
                      ("b", {"code": "W", "region": "n"})]),
                slot("r2", "REPORT",
                     [("a", {"code": "Y", "region": "s"}),
                      ("b", {"code": "V", "region": "s"})]),
                slot("d1", "DETAIL",
                     [("a", {"code": "X", "amt": 10}),
                      ("b", {"code": "W", "amt": 10})]),
                slot("d2", "DETAIL",
                     [("a", {"code": "Y", "amt": 5}),
                      ("b", {"code": "V", "amt": 5})]),
            ],
            [
                {"id": "U-s", "kind": "unique", "table": "SETTLEMENT",
                 "key": ["code"]},
                {"id": "U-r", "kind": "unique", "table": "REPORT",
                 "key": ["code"]},
                {"id": "F-s", "kind": "fk", "child": "DETAIL",
                 "child_fields": ["code"], "parent": "SETTLEMENT",
                 "parent_key": ["code"]},
                {"id": "F-r", "kind": "fk", "child": "DETAIL",
                 "child_fields": ["code"], "parent": "REPORT",
                 "parent_key": ["code"]},
                {"id": "A-s", "kind": "aggregate", "child": "DETAIL",
                 "child_fields": ["code"], "child_amount": "amt",
                 "parent": "SETTLEMENT", "parent_key": ["code"],
                 "parent_amount": "total", "reference_rule": "F-s"},
            ],
        )
        c = parse_contract(doc)
        b1_choices = {s.id: "a" for s in c.slots}
        b2_choices = {s.id: ("b" if len(s.candidates) > 1 else "a")
                      for s in c.slots}
        b1 = fixture_doc(c, b1_choices)
        b2 = fixture_doc(c, b2_choices)
        pack = merge_fixtures(
            c, [("B1", b1), ("B2", b2)],
            wl([("SETTLEMENT", "code"), ("REPORT", "code")]))
        b1_renames = {r["original"]: r["replacement"]
                      for r in pack["rewrites"] if r["batch_id"] == "B1"}
        self.assertNotIn("X", b1_renames)             # locked: stays literal
        self.assertNotIn("Y", b1_renames)
        self.assertIn("FREE", b1_renames)             # parent-only: renamed
        # X stays literally X wherever it appears in B1
        for entries in pack["tables"].values():
            for entry in entries:
                if entry["source"]["batch_id"] != "B1":
                    continue
                if entry["row"].get("code") == "X":
                    self.assertEqual(entry["row"]["code"], "X")
        self.assertEqual(naive_cross_batch_violations(pack, c), [])


class UnresolvableConflictTests(unittest.TestCase):
    def _int_doc(self):
        return contract(
            [
                table("P", "parent",
                      [("id", "int", False), ("total", "int", False)],
                      ["id"]),
                table("C", "child",
                      [("pref", "int", True), ("amt", "int", False)]),
            ],
            [
                slot("p1", "P", [("a", {"id": 7, "total": 3})]),
                slot("c1", "C", [("a", {"pref": 7, "amt": 3})]),
            ],
            [
                {"id": "U", "kind": "unique", "table": "P", "key": ["id"]},
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )

    def test_integer_key_collision_cannot_be_rewritten(self):
        c = parse_contract(self._int_doc())
        choices = {s.id: s.candidates[0].candidate_id for s in c.slots}
        b1 = fixture_doc(c, choices)
        b2 = fixture_doc(c, choices)
        with self.assertRaises(MergeConflictError) as ctx:
            merge_fixtures(c, [("B1", b1), ("B2", b2)], wl([]))
        rule_ids = {w["rule_id"] for w in ctx.exception.witnesses}
        self.assertEqual(rule_ids, {"U", "F", "A"})
        witness = next(w for w in ctx.exception.witnesses if w["rule_id"] == "F")
        self.assertEqual(witness["a"]["slot_id"], "c1")
        self.assertEqual(witness["b"]["slot_id"], "p1")
        self.assertEqual(witness["fixed_key"], [7])

    def test_null_string_collision_is_fixed_and_reported(self):
        doc = contract(
            [
                table("P", "parent",
                      [("id", "str", True)], ["id"]),
                table("C", "child",
                      [("pref", "str", True)]),
            ],
            [
                slot("p1", "P", [("a", {"id": None})]),
                slot("p2", "P", [("a", {"id": None})]),
                slot("c1", "C", [("a", {"pref": None})]),
            ],
            [
                {"id": "U", "kind": "unique", "table": "P", "key": ["id"]},
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
            ],
        )
        c = parse_contract(doc)
        choices = {s.id: s.candidates[0].candidate_id for s in c.slots}
        # Two null parent keys collide within one fixture; only an isolation
        # for U is a verified way to carry that outcome.
        b1 = isolated_doc(c, choices, "U")
        b2 = isolated_doc(c, choices, "U")
        # Nulls cannot be rewritten: duplicate null keys collide across batches.
        with self.assertRaises(MergeConflictError) as ctx:
            merge_fixtures(c, [("B1", b1), ("B2", b2)],
                           wl([("P", "id"), ("C", "pref")]))
        self.assertIn("U", {w["rule_id"] for w in ctx.exception.witnesses})

    def test_missing_key_field_does_not_index_and_never_conflicts(self):
        doc = contract(
            [table("P", "parent", [("id", "str", True)], ["id"])],
            [slot("p", "P", [("a", {})])],
            [{"id": "U", "kind": "unique", "table": "P", "key": ["id"]}],
        )
        c = parse_contract(doc)
        b1 = fixture_doc(c, {"p": "a"})
        b2 = fixture_doc(c, {"p": "a"})
        pack = merge_fixtures(c, [("B1", b1), ("B2", b2)],
                              wl([("P", "id")]))
        self.assertTrue(verify_pack(c, pack)["ok"])
        # absent field stays absent
        for entry in pack["tables"]["P"]:
            self.assertNotIn("id", entry["row"])


class IntraBatchBehaviourTests(unittest.TestCase):
    def test_existing_within_batch_unique_collision_is_preserved(self):
        doc = contract(
            [table("P", "parent",
                   [("id", "str", False)], ["id"])],
            [
                slot("p1", "P", [("a", {"id": "X"})]),
                slot("p2", "P", [("a", {"id": "X"})]),
            ],
            [{"id": "U", "kind": "unique", "table": "P", "key": ["id"]}],
        )
        c = parse_contract(doc)
        # verify_fixture accepts the collision only for an isolated fixture
        # whose target is U.
        b1 = isolated_doc(c, {"p1": "a", "p2": "a"}, "U")
        b2 = isolated_doc(c, {"p1": "a", "p2": "a"}, "U")
        pack = merge_fixtures(
            c, [("B1", b1), ("B2", b2)], wl([("P", "id")]))
        truth = naive_per_batch_truth(pack, doc)
        for bid in ("B1", "B2"):
            self.assertFalse(truth[bid]["U"])  # collision retained
        # but no cross-batch equality survives
        self.assertEqual(naive_cross_batch_violations(pack, c), [])

    def test_isolation_fixture_merge_keeps_its_false_rule(self):
        # B1 isolates F-r: one DETAIL row references a code that has no REPORT
        # parent.  SETTLEMENT still has a parent for it, so F-s and A-s hold.
        doc = transitive_doc()
        # A second pick for the settlement parent so ZZ has a SETTLEMENT row.
        doc["slots"][0]["candidates"].append(
            {"candidate_id": "b", "row": {"code": "ZZ", "total": 7}})
        # The isolated DETAIL row points at ZZ (unknown to REPORT only).
        doc["slots"][4]["candidates"].append(
            {"candidate_id": "b", "row": {"code": "ZZ", "amt": 7}})
        c = parse_contract(doc)
        normal_choices = {s.id: s.candidates[0].candidate_id for s in c.slots}
        iso_choices = dict(normal_choices)
        iso_choices["s1"] = "b"
        iso_choices["d1"] = "b"
        iso = isolated_doc(c, iso_choices, "F-r")
        b2 = fixture_doc(c, normal_choices)
        pack = merge_fixtures(
            c, [("B1", iso), ("B2", b2)], wl(ALL_CODE_FIELDS))
        self.assertTrue(verify_pack(c, pack)["ok"])
        truth = naive_per_batch_truth(pack, doc)
        self.assertFalse(truth["B1"]["F-r"])
        self.assertTrue(truth["B2"]["F-r"])
        self.assertEqual(naive_cross_batch_violations(pack, c), [])


class HotCodeScalingTests(unittest.TestCase):
    def test_many_rows_reusing_one_hot_code_need_no_pairwise_enumeration(self):
        # 50 slots per side, every row reuses the same hot code; each batch is
        # internally consistent.  The merge succeeds with linear indexing and
        # the independent O(n^2) checker finds nothing afterwards.
        parent_slots = [
            slot(f"p{i}", "P", [("a", {"id": "HOT", "total": 1})])
            for i in range(25)]
        child_slots = [
            slot(f"c{i}", "C", [("a", {"pref": "HOT", "amt": 1})])
            for i in range(25)]
        doc = contract(
            [
                table("P", "parent",
                      [("id", "str", False), ("total", "int", False)],
                      ["id"]),
                table("C", "child",
                      [("pref", "str", False), ("amt", "int", False)]),
            ],
            parent_slots + child_slots,
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
            ],
        )
        c = parse_contract(doc)
        choices = {s.id: s.candidates[0].candidate_id for s in c.slots}
        b1 = fixture_doc(c, choices)
        b2 = fixture_doc(c, choices)
        pack = merge_fixtures(
            c, [("B1", b1), ("B2", b2)],
            wl([("P", "id"), ("C", "pref")]))
        self.assertTrue(verify_pack(c, pack)["ok"])
        self.assertEqual(naive_cross_batch_violations(pack, c), [])
        # every occurrence of HOT in one batch shares one replacement
        b1_renames = {r["original"]: r["replacement"]
                      for r in pack["rewrites"]
                      if r["batch_id"] == "B1" and r["original"] == "HOT"}
        self.assertEqual(len(b1_renames), 1)


class BadInputTests(unittest.TestCase):
    def setUp(self):
        self.c = parse_contract(transitive_doc())
        choices = {s.id: s.candidates[0].candidate_id for s in self.c.slots}
        self.b1 = fixture_doc(self.c, choices)
        self.b2 = fixture_doc(self.c, choices)

    def test_single_batch_rejected(self):
        with self.assertRaises(MergeInputError):
            merge_fixtures(self.c, [("B1", self.b1)], wl(ALL_CODE_FIELDS))

    def test_duplicate_batch_id_rejected(self):
        with self.assertRaises(MergeInputError):
            merge_fixtures(self.c, [("B1", self.b1), ("B1", self.b2)],
                           wl(ALL_CODE_FIELDS))

    def test_no_fixture_document_rejected(self):
        bad = {"schema": "no-fixture/1", "kind": "normal"}
        with self.assertRaises(MergeInputError):
            merge_fixtures(self.c, [("B1", bad), ("B2", self.b2)],
                           wl(ALL_CODE_FIELDS))

    def test_tampered_fixture_rejected(self):
        bad = copy.deepcopy(self.b1)
        bad["slots"]["s1"]["row"]["code"] = "FORGED"
        with self.assertRaises(MergeInputError):
            merge_fixtures(self.c, [("B1", bad), ("B2", self.b2)],
                           wl(ALL_CODE_FIELDS))

    def test_self_reported_rule_table_not_trusted(self):
        # A lying rules table is rejected even though the rows are fine.
        bad = copy.deepcopy(self.b1)
        bad["rules"]["U-s"] = False
        with self.assertRaises(MergeInputError):
            merge_fixtures(self.c, [("B1", bad), ("B2", self.b2)],
                           wl(ALL_CODE_FIELDS))

    def test_atomic_write_failure_leaves_no_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = os.path.join(tmp, "keep.json")
            with open(keep, "w") as fh:
                fh.write("keep me")
            # A non-existent output directory makes the temp-file creation
            # fail; no file may appear and existing files stay untouched.
            target = os.path.join(tmp, "nodir", "out.json")
            with self.assertRaises(OSError):
                write_pack_atomic({"x": 1}, target)
            with open(keep) as fh:
                self.assertEqual(fh.read(), "keep me")
            leftovers = [n for n in os.listdir(tmp)
                         if n.startswith(".merge-pack-")]
            self.assertEqual(leftovers, [])


class MergeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.contract_path = os.path.join(self.dir, "contract.json")
        with open(self.contract_path, "w") as fh:
            json.dump(transitive_doc(), fh)
        c = parse_contract(transitive_doc())
        choices = {s.id: s.candidates[0].candidate_id for s in c.slots}
        self.b1_path = os.path.join(self.dir, "b1.json")
        self.b2_path = os.path.join(self.dir, "b2.json")
        with open(self.b1_path, "w") as fh:
            json.dump(fixture_doc(c, choices), fh)
        with open(self.b2_path, "w") as fh:
            json.dump(fixture_doc(c, choices), fh)
        self.wl_path = os.path.join(self.dir, "wl.json")
        with open(self.wl_path, "w") as fh:
            json.dump(wl(ALL_CODE_FIELDS), fh)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_and_verify_roundtrip(self):
        out = os.path.join(self.dir, "pack.json")
        rc = merge_cli.main([
            "build", self.contract_path, "--whitelist", self.wl_path,
            "--batch", f"B1={self.b1_path}",
            "--batch", f"B2={self.b2_path}", "--out", out])
        self.assertEqual(rc, 0)
        rc = merge_cli.main(["verify", self.contract_path, out])
        self.assertEqual(rc, 0)

    def test_conflict_returns_one_and_writes_nothing(self):
        with open(self.wl_path, "w") as fh:
            json.dump(wl([]), fh)
        target = os.path.join(self.dir, "never.json")
        err = io.StringIO()
        with redirect_stderr(err):
            rc = merge_cli.main([
                "build", self.contract_path, "--whitelist", self.wl_path,
                "--batch", f"B1={self.b1_path}",
                "--batch", f"B2={self.b2_path}", "--out", target])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(target))
        self.assertIn("U-s", err.getvalue())

    def test_bad_input_returns_two(self):
        err = io.StringIO()
        with redirect_stderr(err):
            rc = merge_cli.main([
                "build", self.contract_path, "--whitelist", self.wl_path,
                "--batch", f"B1={self.b1_path}"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
