"""Randomized differential tests for the multi-batch merge.

Small random contracts with string key columns are generated; two or three
batches pick candidates independently, a random rule-key-column whitelist is
tried, and every outcome is cross-checked with an independent, naive
implementation that knows nothing about the builder's domain or lock logic:

* when the builder succeeds, the naive final-row scan finds no cross-batch
  equality and each batch's rule truth equals the independently computed truth
  of its original fixture, field-for-field (nulls, missing fields, ints);
* when the builder reports a conflict, every witness is re-derived naively
  from the original fixtures, proving the conflict genuinely cannot be broken
  by whitelist-constrained rewriting.
"""

from __future__ import annotations

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import parse_contract  # noqa: E402
from fixturegen.engine import evaluate_rules, verify_fixture  # noqa: E402
from fixturegen.merge import (  # noqa: E402
    MergeConflictError,
    build_identity_domains,
    merge_fixtures,
    verify_pack,
)
from tests.helpers import contract, slot, table  # noqa: E402
from tests.test_merge import (  # noqa: E402
    naive_cross_batch_violations,
    naive_per_batch_truth,
    wl,
)

LETTERS = "ABCDEFGH"


def random_contract(rng):
    """Build a contract whose first-candidate selection is fully valid.

    Parent slots partition the code alphabet (their first candidates are
    distinct), each code gets one child slot whose first candidate matches
    the parent totals, and extra candidates supply the perturbation space.
    Null-reference and field-omission candidates exercise the absent/null
    semantics without breaking the baseline.
    """
    n_parents = rng.randint(1, 2)
    tables = []
    slots = []
    rules = []
    shared_codes = rng.sample(LETTERS, rng.randint(3, 5))
    code_totals = {code: rng.choice([0, 5, 10]) for code in shared_codes}

    for k in range(n_parents):
        name = f"P{k}"
        tables.append(table(
            name, "parent",
            [("code", "str", False), ("total", "int", True)], ["code"]))
        # as many parent slots as codes; slot si's first candidate is codes[si]
        for si, own in enumerate(shared_codes):
            cands = [("c0", {"code": own, "total": code_totals[own]})]
            for ci, other in enumerate(shared_codes):
                if other != own:
                    cands.append((f"c{ci + 1}",
                                  {"code": other, "total": code_totals[other]}))
            slots.append(slot(f"{name}-s{si}", name, cands))
        rules.append({"id": f"U-{name}", "kind": "unique",
                      "table": name, "key": ["code"]})

    child = "C"
    tables.append(table(
        child, "child",
        [("code", "str", True), ("amt", "int", True),
         ("tag", "str", True)]))
    for si, code in enumerate(shared_codes):
        cands = [("c0", {"code": code, "amt": code_totals[code]})]
        for ci, other in enumerate(shared_codes):
            if other != code:
                cands.append((f"c{ci + 1}",
                              {"code": other, "amt": code_totals[other]}))
        cands.append(("n0", {"code": None, "amt": 0}))
        if rng.random() < 0.4:
            cands.append(("m0", {"amt": 0}))  # reference field omitted
        if rng.random() < 0.5:
            for cid, row in cands:
                row["tag"] = rng.choice(LETTERS)
        slots.append(slot(f"C-s{si}", child, cands))
    for k in range(n_parents):
        name = f"P{k}"
        rules.append({
            "id": f"F-{name}", "kind": "fk", "child": child,
            "child_fields": ["code"], "parent": name,
            "parent_key": ["code"]})
    rules.append({
        "id": "A-P0", "kind": "aggregate", "child": child,
        "child_fields": ["code"], "child_amount": "amt",
        "parent": "P0", "parent_key": ["code"],
        "parent_amount": "total", "reference_rule": "F-P0"})
    return contract(tables, slots, rules, contract_id="random")


def _valid_selection(c, rng):
    """Pick candidates until a selection with at most one false rule is found.

    Aggregate consistency is fragile under independent picks, so a known-good
    base selection (first candidate of every slot, which the generator's
    candidate construction keeps mutually consistent) is perturbed one slot
    at a time and rejected whenever two or more rules break.
    """
    base = {s.id: s.candidates[0] for s in c.slots}
    best = dict(base)
    for _ in range(40):
        candidate = dict(best)
        slot = rng.choice(c.slots)
        candidate[slot.id] = rng.choice(slot.candidates)
        truth = evaluate_rules(c, candidate)
        false_rules = [r for r, v in truth.items() if not v]
        if len(false_rules) <= 1:
            best = candidate
    return best, evaluate_rules(c, best)


def random_batch(c, rng):
    assignment, truth = _valid_selection(c, rng)
    selection = {sid: {"candidate_id": cand.candidate_id,
                       "row": dict(cand.row)}
                 for sid, cand in assignment.items()}
    doc = {"schema": "fixture/1", "kind": "normal", "target": None,
           "slots": selection, "rules": truth}
    false_rules = [r for r, v in truth.items() if not v]
    if false_rules:
        doc["kind"] = "isolated"
        doc["target"] = false_rules[0]
    assert verify_fixture(c, doc)["ok"]
    return doc, assignment


def naive_witness_is_real(doc, batches, whitelist_columns, witness):
    """A builder conflict must correspond to a fully-fixed cross-batch tuple.

    Naively: every value at every compared position of both sides must be a
    non-string (null/int) or a string that the whitelist cannot move -- i.e.
    it occurs in a non-whitelisted rule-key column of the same batch/domain.
    """
    c = parse_contract(doc)
    domains = {frozenset(d.columns): d for d in build_identity_domains(c)}
    column_domain = {}
    for d in build_identity_domains(c):
        for col in d.columns:
            column_domain[col] = d.domain_id

    # collect, per batch/domain, the strings occurring in non-whitelisted cols
    locked = {}
    raw = {bid: fx for bid, fx in batches}
    for bid, fx in raw.items():
        for s in c.slots:
            row = fx["slots"][s.id]["row"]
            for fld, value in row.items():
                col = (s.table, fld)
                if col not in column_domain:
                    continue
                if isinstance(value, str) and col not in whitelist_columns:
                    locked.setdefault((bid, column_domain[col]), set()).add(
                        value)

    rule = next(r for r in doc["rules"] if r["id"] == witness["rule_id"])
    fixed = True
    for side in (witness["a"], witness["b"]):
        bid, sid = side["batch_id"], side["slot_id"]
        slot = next(s for s in c.slots if s.id == sid)
        row = raw[bid]["slots"][sid]["row"]
        fields = (rule["key"] if rule["kind"] == "unique"
                  else (rule["parent_key"] if slot.table == rule.get("parent")
                        else rule["child_fields"]))
        for fld in fields:
            if fld not in row:
                fixed = False
                break
            value = row[fld]
            if isinstance(value, str):
                domain_id = column_domain.get((slot.table, fld))
                if value not in locked.get((bid, domain_id), set()):
                    fixed = False
                    break
        if not fixed:
            break
    return fixed


class RandomMergeDifferentialTests(unittest.TestCase):
    def test_random_contracts(self):
        rng = random.Random(20261123)
        successes = 0
        conflicts = 0
        for trial in range(120):
            doc = random_contract(rng)
            c = parse_contract(doc)
            n_batches = rng.randint(2, 3)
            fixtures = [
                (f"B{i}", random_batch(c, rng)[0])
                for i in range(n_batches)]

            rule_columns = {col for d in build_identity_domains(c)
                            for col in d.columns}
            string_rule_columns = sorted(
                col for col in rule_columns
                if c.table(col[0]).field(col[1]).type == "str")
            subset = [col for col in string_rule_columns
                      if rng.random() < 0.7]
            whitelist_columns = set(subset)
            try:
                pack = merge_fixtures(c, fixtures, wl(subset))
            except MergeConflictError as exc:
                conflicts += 1
                for witness in exc.witnesses:
                    self.assertTrue(
                        naive_witness_is_real(
                            doc, fixtures, whitelist_columns, witness),
                        f"trial {trial}: builder reported an avoidable "
                        f"conflict {witness}")
                continue
            successes += 1
            audit = verify_pack(c, pack)
            self.assertTrue(audit["ok"], f"trial {trial}: {audit['errors']}")
            self.assertEqual(
                naive_cross_batch_violations(pack, c), [],
                f"trial {trial}: naive scan found a cross-batch violation")
            truth = naive_per_batch_truth(pack, doc)
            for bid, fx in fixtures:
                self.assertEqual(truth[bid], fx["rules"],
                                 f"trial {trial}: rule truth changed in {bid}")
            # every packaged row maps back to its source candidate with only
            # whitelisted strings changed
            for table_name, entries in pack["tables"].items():
                for entry in entries:
                    src = entry["source"]
                    original = next(
                        fx for bid, fx in fixtures if bid == src["batch_id"]
                    )["slots"][src["slot_id"]]["row"]
                    for fld, value in entry["row"].items():
                        if isinstance(original[fld], str) and (
                                table_name, fld) in whitelist_columns:
                            continue
                        self.assertEqual(
                            value, original[fld],
                            f"trial {trial}: non-whitelisted value changed")
                    self.assertEqual(
                        set(entry["row"]), set(original),
                        f"trial {trial}: field set changed (null/missing)")
        self.assertGreater(successes, 30)
        self.assertGreater(conflicts, 5)


if __name__ == "__main__":
    unittest.main()
