"""Historical FK attribution index: build, refresh, query, digest metadata.

The index answers are cross-checked against an independent naive oracle that
replays the recorded transactions over the raw contract JSON and applies the
contract's written ``row_reference``/``row_key`` semantics directly.  Refresh
behaviour (extension, idempotency, refusal cases, interruption, the digest
backfill) is exercised against real ledger databases, and a randomised
differential round compares refreshed and from-scratch indexes with the
oracle for every sequence and every child slot.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.attribution import (
    STATUSES,
    AttributionIndex,
    AttributionError,
    answer_request,
    build_index,
    index_status,
    refresh_index,
    run_requests,
)
from fixturegen.contract import ContractError, load_contract, parse_contract
from fixturegen.engine import generate
from fixturegen.ledger import LedgerError, init_ledger, open_ledger
from tests.helpers import contract, slot, table
from tests.test_ledger_random import random_transactions
from tests.test_random_differential import _random_doc

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")
NORMAL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "out", "billing", "normal.fixture.json")

SEED = 20261125


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# independent naive oracle (shares no code with the package)


def _naive_key(row, fields):
    values = []
    for f in fields:
        if f not in row:
            return None
        values.append(row[f])
    return tuple(values)


def _naive_reference(row, fields):
    values = []
    for f in fields:
        if f not in row:
            return ("missing", ())
        values.append(row[f])
    if all(v is not None for v in values):
        return ("value", tuple(values))
    if all(v is None for v in values):
        return ("null", ())
    return ("partial", ())


def naive_answer(doc, fixture, txs, rule_id, seq, child_slot_id):
    """Replay transactions up to ``seq`` and attribute directly from JSON."""
    rule = next(r for r in doc["rules"] if r["id"] == rule_id)
    rows = {s["id"]: {c["candidate_id"]: c["row"] for c in s["candidates"]}
            for s in doc["slots"]}
    selection = {sid: entry["candidate_id"]
                 for sid, entry in fixture["slots"].items()}
    for tx in sorted(txs, key=lambda t: t["seq"]):
        if tx["seq"] > seq:
            break
        for change in tx["changes"]:
            selection[change["slot_id"]] = change["candidate_id"]
    child_row = rows[child_slot_id][selection[child_slot_id]]
    status, values = _naive_reference(child_row, rule["child_fields"])
    if status != "value":
        return {"status": status}
    carriers = []
    for s in doc["slots"]:
        if s["table"] != rule["parent"]:
            continue
        key = _naive_key(rows[s["id"]][selection[s["id"]]], rule["parent_key"])
        if key is not None and key == values:
            carriers.append(s["id"])
    if not carriers:
        return {"status": "unmatched"}
    if len(carriers) > 1:
        return {"status": "ambiguous", "parent_matches": len(carriers)}
    parent_slot = carriers[0]
    return {
        "status": "matched",
        "parent_slot_id": parent_slot,
        "parent_candidate_id": selection[parent_slot],
        "parent_row": rows[parent_slot][selection[parent_slot]],
    }


def child_slots_of(doc, rule_id):
    rule = next(r for r in doc["rules"] if r["id"] == rule_id)
    return sorted(s["id"] for s in doc["slots"] if s["table"] == rule["child"])


def strip_locating(answer):
    """Drop the echoed locating fields before comparing with the oracle."""
    return {k: v for k, v in answer.items()
            if k not in ("seq", "child_slot_id")}


# ---------------------------------------------------------------------------
# shared fixtures


def composite_doc():
    """Composite FK key with same-key/different-row candidates on one slot."""
    return contract(
        [
            table("P", "parent",
                  [("k1", "int", True), ("k2", "str", True),
                   ("note", "str", True)], ["k1", "k2"]),
            table("C", "child",
                  [("cid", "int", False), ("r1", "int", True),
                   ("r2", "str", True)]),
        ],
        [
            slot("P1", "P", [("a", {"k1": 1, "k2": "x", "note": "first"}),
                             ("b", {"k1": 1, "k2": "x", "note": "second"}),
                             ("c", {"k1": 2, "k2": "y", "note": "other"}),
                             ("d", {"k1": 5})]),
            slot("P2", "P", [("a", {"k1": 1, "k2": "x"}),
                             ("b", {"k1": 3, "k2": "z"})]),
            slot("C1", "C", [("a", {"cid": 1, "r1": 1, "r2": "x"}),
                             ("b", {"cid": 2, "r1": None, "r2": None}),
                             ("c", {"cid": 3, "r1": 1, "r2": None}),
                             ("d", {"cid": 4, "r1": 1}),
                             ("e", {"cid": 5, "r1": 9, "r2": "w"})]),
        ],
        [
            {"id": "F", "kind": "fk", "child": "C",
             "child_fields": ["r1", "r2"], "parent": "P",
             "parent_key": ["k1", "k2"]},
        ],
        contract_id="composite-1",
    )


def composite_fixture():
    """P1=c (2,"y"), P2=b (3,"z"), C1=b (wholly null reference)."""
    return {
        "schema": "fixture/1", "kind": "normal", "target": None,
        "slots": {
            "P1": {"candidate_id": "c",
                   "row": {"k1": 2, "k2": "y", "note": "other"}},
            "P2": {"candidate_id": "b", "row": {"k1": 3, "k2": "z"}},
            "C1": {"candidate_id": "b",
                   "row": {"cid": 2, "r1": None, "r2": None}},
        },
        "rules": {"F": True},
    }


def composite_fixture_alt():
    """A different all-true initial selection for the same contract."""
    return {
        "schema": "fixture/1", "kind": "normal", "target": None,
        "slots": {
            "P1": {"candidate_id": "a",
                   "row": {"k1": 1, "k2": "x", "note": "first"}},
            "P2": {"candidate_id": "b", "row": {"k1": 3, "k2": "z"}},
            "C1": {"candidate_id": "b",
                   "row": {"cid": 2, "r1": None, "r2": None}},
        },
        "rules": {"F": True},
    }


COMPOSITE_TXS = [
    # 1: P1 -> a (key (1,"x"), note "first")
    {"seq": 1, "changes": [{"slot_id": "P1", "candidate_id": "a"}]},
    # 2: C1 -> a (reference (1,"x")): unique match on P1/a
    {"seq": 2, "changes": [{"slot_id": "C1", "candidate_id": "a"}]},
    # 3: P1 -> b: same key (1,"x"), different note — key-preserving move
    {"seq": 3, "changes": [{"slot_id": "P1", "candidate_id": "b"}]},
    # 4: P2 -> a: now two parents carry (1,"x")
    {"seq": 4, "changes": [{"slot_id": "P2", "candidate_id": "a"}]},
    # 5: C1 -> c (partial reference)
    {"seq": 5, "changes": [{"slot_id": "C1", "candidate_id": "c"}]},
    # 6: C1 -> d (missing r2)
    {"seq": 6, "changes": [{"slot_id": "C1", "candidate_id": "d"}]},
    # 7: C1 -> e (reference (9,"w"), no such parent)
    {"seq": 7, "changes": [{"slot_id": "C1", "candidate_id": "e"}]},
    # 8: C1 -> b (wholly null reference)
    {"seq": 8, "changes": [{"slot_id": "C1", "candidate_id": "b"}]},
    # 9: P1 -> d (no key field k2 at all)
    {"seq": 9, "changes": [{"slot_id": "P1", "candidate_id": "d"}]},
]


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._path_counter = 0

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def fresh_path(self, stem):
        """A unique path per call, for loops that create many databases."""
        self._path_counter += 1
        base, ext = os.path.splitext(stem)
        return self.path(f"{base}-{self._path_counter}{ext}")

    def make_ledger(self, name, contract_obj, fixture, txs=()):
        db_path = self.path(name)
        ledger = init_ledger(db_path, contract_obj, fixture)
        for tx in txs:
            ledger.apply_payload(tx)
        ledger.close()
        return db_path


# ---------------------------------------------------------------------------
# billing-based scenario: the same fact re-attributed to another parent


class BillingScenarioTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = load_contract(os.path.join(EXAMPLES, "billing.json"))
        self.fixture = load(NORMAL)
        self.txs = [
            {"seq": 1, "changes": [{"slot_id": "S02-client",
                                    "candidate_id": "d2"}]},
            {"seq": 2, "changes": [{"slot_id": "S01-client",
                                    "candidate_id": "c2"}]},
            {"seq": 3, "changes": [{"slot_id": "S04-invoice",
                                    "candidate_id": "i5"}]},
            {"seq": 4, "changes": [{"slot_id": "S04-invoice",
                                    "candidate_id": "i4"}]},
        ]
        self.db = self.make_ledger("ledger.db", self.contract, self.fixture,
                                   self.txs)
        self.index_path = self.path("index.db")
        build_index(self.index_path, self.contract, self.db,
                    "F-invoice-client")

    def answer(self, seq, slot_id="S04-invoice"):
        with AttributionIndex(self.index_path) as index:
            return index.answer(seq, slot_id)

    def test_same_fact_different_attribution_over_time(self):
        # The S04 invoice references client 1 throughout; only the parent
        # side moves.  At seq 0 the reference lands on S01-client/c1
        # (client 1, region north), at seq 2 on S02-client/d2 (client 1,
        # region east): re-running the report with current values would
        # rewrite history.
        first = self.answer(0)
        self.assertEqual(first["status"], "matched")
        self.assertEqual(first["parent_slot_id"], "S01-client")
        self.assertEqual(first["parent_candidate_id"], "c1")
        self.assertEqual(first["parent_row"],
                         {"client_id": 1, "region": "north",
                          "billed_total": 30})
        second = self.answer(2)
        self.assertEqual(second["status"], "matched")
        self.assertEqual(second["parent_slot_id"], "S02-client")
        self.assertEqual(second["parent_candidate_id"], "d2")
        self.assertEqual(second["parent_row"],
                         {"client_id": 1, "region": "east",
                          "billed_total": 0})

    def test_ambiguous_point_despite_true_fk_rule(self):
        # At seq 1 both parents carry client_id 1: U-client-id and
        # A-client-billed are recorded false, F-invoice-client is recorded
        # *true* — yet the attribution is ambiguous, not a unique match.
        answer = self.answer(1)
        self.assertEqual(answer["status"], "ambiguous")
        self.assertEqual(answer["parent_matches"], 2)
        self.assertNotIn("parent_slot_id", answer)
        self.assertNotIn("parent_row", answer)

    def test_unmatched_and_null_and_back_again(self):
        self.assertEqual(self.answer(3)["status"], "unmatched")  # i5: ref 9
        self.assertEqual(self.answer(4)["status"], "matched")
        self.assertEqual(self.answer(0, "S05-invoice")["status"], "null")

    def test_every_sequence_matches_naive_oracle(self):
        doc = load(os.path.join(EXAMPLES, "billing.json"))
        with AttributionIndex(self.index_path) as index:
            for seq in range(0, 5):
                for slot_id in ("S04-invoice", "S05-invoice"):
                    expected = naive_answer(doc, self.fixture, self.txs,
                                            "F-invoice-client", seq, slot_id)
                    self.assertEqual(
                        strip_locating(index.answer(seq, slot_id)), expected,
                        (seq, slot_id))


class CompositeKeyTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = parse_contract(composite_doc())
        self.fixture = composite_fixture()
        self.db = self.make_ledger("ledger.db", self.contract, self.fixture,
                                   COMPOSITE_TXS)
        self.index_path = self.path("index.db")
        build_index(self.index_path, self.contract, self.db, "F")

    def answer(self, seq, slot_id="C1"):
        with AttributionIndex(self.index_path) as index:
            return index.answer(seq, slot_id)

    def test_reference_classification(self):
        self.assertEqual(self.answer(0)["status"], "null")
        self.assertEqual(self.answer(2)["status"], "matched")
        self.assertEqual(self.answer(4)["status"], "ambiguous")
        self.assertEqual(self.answer(5)["status"], "partial")
        self.assertEqual(self.answer(6)["status"], "missing")
        self.assertEqual(self.answer(7)["status"], "unmatched")
        self.assertEqual(self.answer(8)["status"], "null")

    def test_key_preserving_parent_move_still_returns_that_point_in_time(self):
        # Between seq 2 and 3 the parent key (1,"x") does not move, but the
        # selected candidate on P1 changes (note "first" -> "second").  Each
        # point in time returns its own candidate and row.
        before = self.answer(2)
        self.assertEqual(before["parent_candidate_id"], "a")
        self.assertEqual(before["parent_row"],
                         {"k1": 1, "k2": "x", "note": "first"})
        after = self.answer(3)
        self.assertEqual(after["parent_candidate_id"], "b")
        self.assertEqual(after["parent_row"],
                         {"k1": 1, "k2": "x", "note": "second"})

    def test_parent_without_key_is_no_carrier(self):
        # At seq 9 P1 has no k2 (no key at all); only P2 carries (1,"x").
        self.assertEqual(self.answer(9)["status"], "null")  # C1 wholly null
        with AttributionIndex(self.index_path) as index:
            expected = naive_answer(composite_doc(), self.fixture,
                                    COMPOSITE_TXS, "F", 9, "C1")
            self.assertEqual(strip_locating(index.answer(9, "C1")), expected)

    def test_all_points_match_naive_oracle(self):
        with AttributionIndex(self.index_path) as index:
            for seq in range(0, 10):
                expected = naive_answer(composite_doc(), self.fixture,
                                        COMPOSITE_TXS, "F", seq, "C1")
                self.assertEqual(
                    strip_locating(index.answer(seq, "C1")), expected, seq)


# ---------------------------------------------------------------------------
# request validation


class RequestValidationTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = parse_contract(composite_doc())
        self.db = self.make_ledger("ledger.db", self.contract,
                                   composite_fixture(), COMPOSITE_TXS[:2])
        self.index_path = self.path("index.db")
        build_index(self.index_path, self.contract, self.db, "F")

    def run_lines(self, lines):
        with AttributionIndex(self.index_path) as index:
            return run_requests(index, lines)

    def test_problem_lines_are_named_and_batch_continues(self):
        lines = [
            json.dumps({"seq": 2, "child_slot_id": "C1"}),      # ok
            json.dumps({"seq": 99, "child_slot_id": "C1"}),     # beyond cutoff
            json.dumps({"seq": 0, "child_slot_id": "P1"}),      # not a child
            json.dumps({"seq": 0, "child_slot_id": "ghost"}),   # unknown slot
            json.dumps({"seq": -1, "child_slot_id": "C1"}),     # negative
            json.dumps({"seq": True, "child_slot_id": "C1"}),   # bool seq
            json.dumps({"seq": 0}),                             # missing key
            "not json",                                         # malformed
            "",                                                 # empty
            json.dumps({"seq": 0, "child_slot_id": "C1",
                        "fact_id": "f-9"}),                     # extra key
            json.dumps({"seq": 2, "child_slot_id": "C1"}),      # ok again
        ]
        results, errors = self.run_lines(lines)
        self.assertEqual(errors, 9)
        self.assertEqual([r["line"] for r in results],
                         list(range(1, len(lines) + 1)))
        self.assertEqual(results[0]["status"], "matched")
        self.assertEqual(results[-1]["status"], "matched")
        for bad in results[1:-1]:
            self.assertEqual(bad["status"], "error")
            self.assertIn("error", bad)
        self.assertIn("0..2", results[1]["error"])
        self.assertIn("not a child slot", results[2]["error"])
        self.assertIn("not a child slot", results[3]["error"])
        self.assertEqual(results[3]["child_slot_id"], "ghost")
        self.assertIn("non-negative integer", results[4]["error"])
        self.assertIn("non-negative integer", results[5]["error"])
        self.assertIn("missing 'child_slot_id'", results[6]["error"])
        self.assertIn("invalid JSON", results[7]["error"])
        self.assertIn("empty line", results[8]["error"])
        self.assertIn("unsupported key", results[9]["error"])

    def test_beyond_cutoff_never_falls_back_to_current_values(self):
        # The ledger commits C1 -> a at seq 2 (a "matched" state).  An index
        # built at cutoff 2 must refuse seq 3 outright, not answer with the
        # seq-2 (or any current) values.
        shorter = self.path("shorter.db")
        ledger = init_ledger(shorter, self.contract, composite_fixture())
        for tx in COMPOSITE_TXS[:2]:
            ledger.apply_payload(tx)
        ledger.close()
        index_path = self.path("shorter-index.db")
        build_index(index_path, self.contract, shorter, "F")
        with AttributionIndex(index_path) as index:
            self.assertEqual(index.answer(2, "C1")["status"], "matched")
            with self.assertRaises(AttributionError):
                index.answer(3, "C1")

    def test_duplicates_and_unordered_sequences(self):
        lines = [json.dumps({"seq": s, "child_slot_id": "C1"})
                 for s in (2, 0, 2, 1, 0)]
        results, errors = self.run_lines(lines)
        self.assertEqual(errors, 0)
        self.assertEqual([r["seq"] for r in results], [2, 0, 2, 1, 0])
        self.assertEqual(results[0]["status"], results[2]["status"])

    def test_answer_request_rejects_bad_shapes(self):
        with AttributionIndex(self.index_path) as index:
            for payload in (None, [], "x", {"seq": "1", "child_slot_id": "C1"},
                            {"seq": 0, "child_slot_id": 3}):
                with self.assertRaises(AttributionError):
                    answer_request(index, payload)


# ---------------------------------------------------------------------------
# refresh behaviour


class RefreshTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = parse_contract(composite_doc())
        self.fixture = composite_fixture()
        self.db = self.path("ledger.db")
        ledger = init_ledger(self.db, self.contract, self.fixture)
        for tx in COMPOSITE_TXS[:3]:
            ledger.apply_payload(tx)
        ledger.close()
        self.index_path = self.path("index.db")
        build_index(self.index_path, self.contract, self.db, "F")

    def answers(self, upto):
        with AttributionIndex(self.index_path) as index:
            return [index.answer(seq, "C1") for seq in range(upto + 1)]

    def extend_ledger(self, txs):
        ledger = open_ledger(self.db, self.contract)
        for tx in txs:
            ledger.apply_payload(tx)
        ledger.close()

    def test_refresh_extends_and_preserves_historical_answers(self):
        before = self.answers(3)
        self.extend_ledger(COMPOSITE_TXS[3:6])
        result = refresh_index(self.index_path, self.db)
        self.assertTrue(result["refreshed"])
        self.assertEqual(result["indexed_seq"], 6)
        self.assertEqual(result["transactions_indexed"], 3)
        self.assertEqual(self.answers(3), before)  # history unchanged
        with AttributionIndex(self.index_path) as index:
            self.assertEqual(index.answer(4, "C1")["status"], "ambiguous")
            self.assertEqual(index.answer(5, "C1")["status"], "partial")
            self.assertEqual(index.answer(6, "C1")["status"], "missing")

    def test_refresh_on_unchanged_ledger_is_a_no_op(self):
        with open(self.index_path, "rb") as fh:
            before = fh.read()
        result = refresh_index(self.index_path, self.db)
        self.assertFalse(result["refreshed"])
        self.assertEqual(result["indexed_seq"], 3)
        with open(self.index_path, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_refuses_shorter_ledger(self):
        shorter = self.make_ledger("shorter.db", self.contract, self.fixture,
                                   COMPOSITE_TXS[:1])
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, shorter)
        self.assertIn("shorter", str(ctx.exception))
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)

    def test_refuses_different_contract(self):
        other_doc = composite_doc()
        other_doc["contract_id"] = "composite-2"
        other = parse_contract(other_doc)
        other_db = self.make_ledger("other.db", other, self.fixture,
                                    COMPOSITE_TXS[:4])
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, other_db)
        self.assertIn("contract differs", str(ctx.exception))
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)

    def test_refuses_different_initial_fixture(self):
        other_db = self.make_ledger("other.db", self.contract,
                                    composite_fixture_alt(), COMPOSITE_TXS[:4])
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, other_db)
        self.assertIn("initial fixture differs", str(ctx.exception))
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)

    def test_refuses_diverged_prefix_same_length(self):
        other_db = self.path("other.db")
        ledger = init_ledger(other_db, self.contract, self.fixture)
        ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"}]})   # different tx 1
        ledger.apply_payload(COMPOSITE_TXS[1])
        ledger.apply_payload(COMPOSITE_TXS[2])
        ledger.close()
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, other_db)
        self.assertIn("differs", str(ctx.exception))
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)

    def test_refuses_diverged_prefix_longer_ledger(self):
        other_db = self.path("other.db")
        ledger = init_ledger(other_db, self.contract, self.fixture)
        ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"}]})
        for tx in COMPOSITE_TXS[1:5]:
            ledger.apply_payload(tx)
        ledger.close()
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, other_db)
        self.assertIn("prefix differs", str(ctx.exception))
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)

    def test_interrupted_refresh_keeps_previous_version(self):
        self.extend_ledger(COMPOSITE_TXS[3:6])
        before = self.answers(3)

        def explode():
            raise RuntimeError("simulated crash before commit")

        with self.assertRaises(RuntimeError):
            refresh_index(self.index_path, self.db, before_commit_hook=explode)
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 3)
        self.assertEqual(self.answers(3), before)
        # a later refresh completes normally
        refresh_index(self.index_path, self.db)
        self.assertEqual(index_status(self.index_path)["indexed_seq"], 6)

    def test_ledger_without_digest_metadata_needs_backfill(self):
        self.extend_ledger(COMPOSITE_TXS[3:5])
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE flow_digest")
        conn.commit()
        conn.close()
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, self.db)
        self.assertIn("backfill", str(ctx.exception))
        ledger = open_ledger(self.db, self.contract)
        added = ledger.backfill_flow_digests()
        ledger.close()
        self.assertEqual(added, 5)
        result = refresh_index(self.index_path, self.db)
        self.assertEqual(result["indexed_seq"], 5)

    def test_stale_digest_metadata_is_reported(self):
        self.extend_ledger(COMPOSITE_TXS[3:5])
        conn = sqlite3.connect(self.db)
        conn.execute("DELETE FROM flow_digest WHERE seq=5")
        conn.commit()
        conn.close()
        with self.assertRaises(AttributionError) as ctx:
            refresh_index(self.index_path, self.db)
        self.assertIn("backfill", str(ctx.exception))
        # backfill repairs the stale table and the refresh then succeeds
        ledger = open_ledger(self.db, self.contract)
        self.assertEqual(ledger.backfill_flow_digests(), 1)
        ledger.close()
        self.assertEqual(refresh_index(self.index_path, self.db)
                         ["indexed_seq"], 5)

    def test_no_op_transactions_advance_cutoff_without_new_versions(self):
        ledger = open_ledger(self.db, self.contract)
        ledger.apply_payload({"seq": 4, "changes": [
            {"slot_id": "P1", "candidate_id": "b"}]})   # already b since 3
        ledger.apply_payload({"seq": 5, "changes": [
            {"slot_id": "C1", "candidate_id": "a"}]})   # already a since 2
        ledger.close()
        with AttributionIndex(self.index_path) as index:
            before_versions = index.status()["slot_versions"]
        result = refresh_index(self.index_path, self.db)
        self.assertEqual(result["indexed_seq"], 5)
        with AttributionIndex(self.index_path) as index:
            self.assertEqual(index.status()["slot_versions"], before_versions)
            # a no-op transaction does not change any historical answer
            self.assertEqual(strip_locating(index.answer(5, "C1")),
                             strip_locating(index.answer(3, "C1")))

    def test_open_handles_keep_their_version_across_a_refresh(self):
        # Two report batches hold read handles while a refresh commits:
        # their already-queryable answers are unchanged and sequences beyond
        # their own cutoff stay refused; a new handle sees the new cutoff.
        first = AttributionIndex(self.index_path)
        second = AttributionIndex(self.index_path)
        try:
            before_first = first.answer(2, "C1")
            before_second = second.answer(0, "C1")
            self.extend_ledger(COMPOSITE_TXS[3:5])
            refresh_index(self.index_path, self.db)
            self.assertEqual(first.answer(2, "C1"), before_first)
            self.assertEqual(second.answer(0, "C1"), before_second)
            with self.assertRaises(AttributionError):
                first.answer(4, "C1")
        finally:
            first.close()
            second.close()
        with AttributionIndex(self.index_path) as fresh:
            self.assertEqual(fresh.indexed_seq, 5)
            self.assertEqual(fresh.answer(4, "C1")["status"], "ambiguous")
            self.assertEqual(strip_locating(fresh.answer(2, "C1")),
                             strip_locating(before_first))


# ---------------------------------------------------------------------------
# digest metadata on the ledger itself


class DigestMetadataTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = parse_contract(composite_doc())
        self.fixture = composite_fixture()

    def test_new_ledger_maintains_digests(self):
        db = self.make_ledger("ledger.db", self.contract, self.fixture,
                              COMPOSITE_TXS[:4])
        conn = sqlite3.connect(db)
        rows = dict(conn.execute("SELECT seq, digest FROM flow_digest"))
        conn.close()
        self.assertEqual(sorted(rows), [1, 2, 3, 4])
        ledger = open_ledger(db, self.contract, verify_history=True)
        ledger.close()

    def test_backfill_restores_dropped_table_and_is_idempotent(self):
        db = self.make_ledger("ledger.db", self.contract, self.fixture,
                              COMPOSITE_TXS[:3])
        conn = sqlite3.connect(db)
        original = dict(conn.execute("SELECT seq, digest FROM flow_digest"))
        conn.execute("DROP TABLE flow_digest")
        conn.commit()
        conn.close()
        # the ledger works exactly as before without the table
        ledger = open_ledger(db, self.contract)
        ledger.apply_payload(COMPOSITE_TXS[3])
        self.assertEqual(ledger.backfill_flow_digests(), 4)
        self.assertEqual(ledger.backfill_flow_digests(), 0)
        ledger.close()
        conn = sqlite3.connect(db)
        restored = dict(conn.execute("SELECT seq, digest FROM flow_digest"))
        conn.close()
        # the recomputed chain reproduces the original rows and extends them
        self.assertEqual(len(restored), 4)
        for seq, digest in original.items():
            self.assertEqual(restored[seq], digest)
        # and apply keeps the restored table current
        ledger = open_ledger(db, self.contract, verify_history=True)
        ledger.apply_payload(COMPOSITE_TXS[4])
        ledger.close()
        conn = sqlite3.connect(db)
        self.assertEqual(len(conn.execute(
            "SELECT seq FROM flow_digest").fetchall()), 5)
        conn.close()

    def test_backfill_refuses_to_store_a_chain_that_does_not_match(self):
        db = self.make_ledger("ledger.db", self.contract, self.fixture,
                              COMPOSITE_TXS[:2])
        conn = sqlite3.connect(db)
        conn.execute("DROP TABLE flow_digest")
        conn.execute("UPDATE flow SET changes='[[\"P1\",\"b\"]]' WHERE seq=1")
        conn.commit()
        conn.close()
        ledger = open_ledger(db, self.contract)
        with self.assertRaises(LedgerError):
            ledger.backfill_flow_digests()
        # nothing was stored — not even the table
        conn = sqlite3.connect(db)
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='flow_digest'").fetchone()
        conn.close()
        self.assertIsNone(present)
        ledger.close()

    def test_reopen_tolerates_stale_but_not_inconsistent_digest_table(self):
        db = self.make_ledger("ledger.db", self.contract, self.fixture,
                              COMPOSITE_TXS[:2])
        # a table that lags behind the flow is stale derived metadata: the
        # ledger opens as before and the backfill repairs it
        conn = sqlite3.connect(db)
        conn.execute("DELETE FROM flow_digest WHERE seq=2")
        conn.commit()
        conn.close()
        ledger = open_ledger(db, self.contract)
        self.assertEqual(ledger.backfill_flow_digests(), 1)
        ledger.close()
        # a fully covering table whose head disagrees with the committed
        # state digest is corruption and refuses to open
        conn = sqlite3.connect(db)
        conn.execute("UPDATE flow_digest SET digest='x' WHERE seq=2")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            open_ledger(db, self.contract)
        # and so is a table ahead of the committed sequence
        conn = sqlite3.connect(db)
        conn.execute("UPDATE flow_digest SET digest=(SELECT digest FROM "
                     "flow_digest WHERE seq=1) WHERE seq=2")
        conn.execute("INSERT INTO flow_digest(seq, digest) VALUES (9, 'y')")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            open_ledger(db, self.contract)

    def test_history_audit_cross_checks_digest_rows(self):
        db = self.make_ledger("ledger.db", self.contract, self.fixture,
                              COMPOSITE_TXS[:2])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE flow_digest SET digest='x' WHERE seq=1")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            open_ledger(db, self.contract, verify_history=True)


# ---------------------------------------------------------------------------
# storage shape


class StorageShapeTests(TempDirCase):
    def test_index_stores_changes_not_per_transaction_copies(self):
        doc = contract(
            [table("P", "parent", [("pid", "int", True)], ["pid"]),
             table("C", "child",
                   [("cid", "int", False), ("pref", "int", True)])],
            [slot(f"P{i}", "P", [("a", {"pid": 1}), ("b", {"pid": i + 10})])
             for i in range(6)] +
            [slot("C1", "C", [("a", {"cid": 1, "pref": 1})])],
            [{"id": "F", "kind": "fk", "child": "C", "child_fields": ["pref"],
              "parent": "P", "parent_key": ["pid"]}],
            contract_id="hot-key",
        )
        c = parse_contract(doc)
        fixture = {
            "schema": "fixture/1", "kind": "normal", "target": None,
            "slots": {f"P{i}": {"candidate_id": "a", "row": {"pid": 1}}
                      for i in range(6)},
            "rules": {"F": True},
        }
        fixture["slots"]["C1"] = {"candidate_id": "a",
                                  "row": {"cid": 1, "pref": 1}}
        # move parents away from the hot key one transaction at a time
        txs = [{"seq": i + 1,
                "changes": [{"slot_id": f"P{i}", "candidate_id": "b"}]}
               for i in range(6)]
        db = self.make_ledger("ledger.db", c, fixture, txs)
        index_path = self.path("index.db")
        build_index(index_path, c, db, "F")
        conn = sqlite3.connect(index_path)
        versions = conn.execute("SELECT COUNT(*) FROM slot_version").fetchone()[0]
        key_rows = conn.execute(
            "SELECT seq, parent_count, only_slot_id FROM parent_key_state "
            "WHERE key_json='[1]' ORDER BY seq").fetchall()
        conn.close()
        # 7 involved slots at seq 0 plus one version per real change
        self.assertEqual(versions, 7 + 6)
        # one key-state row per membership change, never per parent row
        self.assertEqual([r[0] for r in key_rows], [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual([r[1] for r in key_rows], [6, 5, 4, 3, 2, 1, 0])
        with AttributionIndex(index_path) as index:
            for seq in range(7):
                answer = index.answer(seq, "C1")
                remaining = 6 - seq
                if remaining > 1:
                    self.assertEqual(answer["status"], "ambiguous")
                    self.assertEqual(answer["parent_matches"], remaining)
                elif remaining == 1:
                    self.assertEqual(answer["status"], "matched")
                    self.assertEqual(answer["parent_slot_id"], "P5")
                else:
                    self.assertEqual(answer["status"], "unmatched")


# ---------------------------------------------------------------------------
# randomised differential round


class RandomDifferentialTests(TempDirCase):
    def test_random_indexes_match_naive_oracle(self):
        rng = random.Random(SEED)
        tested = 0
        attempt = 0
        while tested < 20 and attempt < 400:
            attempt += 1
            doc = _random_doc(rng, attempt)
            try:
                c = parse_contract(doc)
            except ContractError:
                continue
            normal = generate(c).normal
            if not hasattr(normal, "selection"):
                continue
            tested += 1
            with self.subTest(contract=doc["contract_id"]):
                self._run_one(rng, doc, c, normal.to_json())

    def _run_one(self, rng, doc, c, fixture):
        txs = random_transactions(rng, c, doc, rng.randint(6, 14))
        midpoint = len(txs) // 2
        db = self.fresh_path("ledger.db")
        ledger = init_ledger(db, c, fixture)
        for tx in txs[:midpoint]:
            ledger.apply_payload(tx)
        ledger.close()
        refreshed = self.fresh_path("refreshed.db")
        build_index(refreshed, c, db, "F")
        ledger = open_ledger(db, c)
        for tx in txs[midpoint:]:
            ledger.apply_payload(tx)
        ledger.close()
        refresh_index(refreshed, db)
        # an index built from scratch at the final sequence must agree
        from_scratch = self.fresh_path("from-scratch.db")
        build_index(from_scratch, c, db, "F")
        rule = next(r for r in doc["rules"] if r["id"] == "F")
        children = child_slots_of(doc, "F")
        self.assertTrue(children)
        with AttributionIndex(refreshed) as a, \
                AttributionIndex(from_scratch) as b:
            self.assertEqual(a.indexed_seq, len(txs))
            self.assertEqual(a.indexed_digest, b.indexed_digest)
            for seq in range(0, len(txs) + 1):
                for slot_id in children:
                    expected = naive_answer(doc, fixture, txs, "F", seq,
                                            slot_id)
                    answer_a = a.answer(seq, slot_id)
                    answer_b = b.answer(seq, slot_id)
                    self.assertIn(answer_a["status"], STATUSES)
                    self.assertEqual(
                        strip_locating(answer_a), expected,
                        (doc["contract_id"], seq, slot_id))
                    self.assertEqual(
                        strip_locating(answer_b), expected,
                        (doc["contract_id"], seq, slot_id))
        # the second FK rule, when the random contract has one, indexes too
        if any(r["id"] == "F2" for r in doc["rules"]):
            second = self.fresh_path("second.db")
            build_index(second, c, db, "F2")
            with AttributionIndex(second) as index:
                for seq in range(0, len(txs) + 1):
                    for slot_id in children:
                        self.assertEqual(
                            strip_locating(index.answer(seq, slot_id)),
                            naive_answer(doc, fixture, txs, "F2", seq,
                                         slot_id),
                            (doc["contract_id"], "F2", seq, slot_id))


# ---------------------------------------------------------------------------
# CLI


class CliTests(TempDirCase):
    def setUp(self):
        super().setUp()
        self.contract = parse_contract(composite_doc())
        self.contract_path = self.path("contract.json")
        with open(self.contract_path, "w", encoding="utf-8") as fh:
            json.dump(composite_doc(), fh)
        self.db = self.make_ledger("ledger.db", self.contract,
                                   composite_fixture(), COMPOSITE_TXS[:2])
        self.index_path = self.path("index.db")

    def test_build_status_query_refresh_cycle(self):
        from fixturegen import attribution_cli
        import io
        from contextlib import redirect_stdout, redirect_stderr

        out = io.StringIO()
        with redirect_stdout(out):
            rc = attribution_cli.main([
                "build", "--contract", self.contract_path,
                "--ledger", self.db, "--rule", "F",
                "--index", self.index_path])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["indexed_seq"], 2)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = attribution_cli.main(["status", "--index", self.index_path])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["indexed_seq"], 2)

        requests_path = self.path("requests.jsonl")
        with open(requests_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"seq": 2, "child_slot_id": "C1"}) + "\n")
            fh.write(json.dumps({"seq": 0, "child_slot_id": "C1"}) + "\n")
            fh.write(json.dumps({"seq": 7, "child_slot_id": "C1"}) + "\n")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = attribution_cli.main([
                "query", "--index", self.index_path, requests_path])
        self.assertEqual(rc, 1)  # one line beyond the cutoff
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r["status"] for r in lines],
                         ["matched", "null", "error"])
        self.assertEqual(lines[2]["line"], 3)

        # extend the ledger, refresh through the CLI, re-ask
        ledger = open_ledger(self.db, self.contract)
        for tx in COMPOSITE_TXS[2:7]:
            ledger.apply_payload(tx)
        ledger.close()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = attribution_cli.main([
                "refresh", "--ledger", self.db, "--index", self.index_path])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["indexed_seq"], 7)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = attribution_cli.main([
                "query", "--index", self.index_path, requests_path])
        self.assertEqual(rc, 0)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[2]["status"], "unmatched")

    def test_cli_errors(self):
        from fixturegen import attribution_cli
        import io
        from contextlib import redirect_stdout, redirect_stderr

        with redirect_stderr(io.StringIO()):
            rc = attribution_cli.main([
                "build", "--contract", self.contract_path,
                "--ledger", self.db, "--rule", "NOPE",
                "--index", self.index_path])
        self.assertEqual(rc, 2)
        with redirect_stderr(io.StringIO()):
            rc = attribution_cli.main([
                "status", "--index", self.path("missing.db")])
        self.assertEqual(rc, 2)
        # refusing to overwrite an existing index
        with redirect_stdout(io.StringIO()):
            attribution_cli.main([
                "build", "--contract", self.contract_path,
                "--ledger", self.db, "--rule", "F",
                "--index", self.index_path])
        err = io.StringIO()
        with redirect_stderr(err):
            rc = attribution_cli.main([
                "build", "--contract", self.contract_path,
                "--ledger", self.db, "--rule", "F",
                "--index", self.index_path])
        self.assertEqual(rc, 2)
        self.assertIn("overwrite", err.getvalue())

    def test_ledger_cli_backfill(self):
        from fixturegen import ledger_cli
        import io
        from contextlib import redirect_stdout

        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE flow_digest")
        conn.commit()
        conn.close()
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ledger_cli.main([
                "backfill", "--contract", self.contract_path, "--db", self.db])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["digests_added"], 2)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ledger_cli.main([
                "backfill", "--contract", self.contract_path, "--db", self.db])
        self.assertEqual(json.loads(out.getvalue())["digests_added"], 0)


if __name__ == "__main__":
    unittest.main()
