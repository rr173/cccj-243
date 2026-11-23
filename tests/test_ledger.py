"""Ledger behaviour: incremental truth, flow diffs, crash safety, reopening.

The incremental rule indexes are checked against the reference
``evaluate_rules`` after every transaction, and the flow's newly-false and
recovered lists are checked against successive reference evaluations.  Input
acceptance is exercised line by line (invalid values, unknown slot/candidate,
duplicate slot, gap, conflicting history, idempotent replay), together with a
simulated crash before commit and reopen with and without the history audit.
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import load_contract, parse_contract
from fixturegen.engine import evaluate_rules
from fixturegen.ledger import (
    InputError,
    LedgerError,
    init_ledger,
    open_ledger,
)
from tests.helpers import contract, slot, table

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "examples")
NORMAL = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "out", "billing", "normal.fixture.json")


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def two_parent_doc():
    """Two independent parent slots whose candidate keys overlap.

    This is the shape in which two parent rows can exchange identities inside
    one transaction; the billing example's parent key spaces do not overlap.
    """
    return contract(
        [
            table("P", "parent",
                  [("pid", "int", False), ("total", "int", True)], ["pid"]),
            table("C", "child",
                  [("cid", "int", False), ("pref", "int", True),
                   ("amt", "int", True)]),
        ],
        [
            slot("P1", "P", [("a", {"pid": 1, "total": 10}),
                             ("b", {"pid": 2, "total": 10})]),
            slot("P2", "P", [("a", {"pid": 2, "total": 0}),
                             ("b", {"pid": 1, "total": 0})]),
            slot("C1", "C", [("x", {"cid": 1, "pref": 1, "amt": 10}),
                             ("y", {"cid": 1, "pref": 2, "amt": 10})]),
        ],
        [
            {"id": "U", "kind": "unique", "table": "P", "key": ["pid"]},
            {"id": "F", "kind": "fk", "child": "C", "child_fields": ["pref"],
             "parent": "P", "parent_key": ["pid"]},
            {"id": "A", "kind": "aggregate", "child": "C",
             "child_fields": ["pref"], "child_amount": "amt",
             "parent": "P", "parent_key": ["pid"], "parent_amount": "total"},
        ],
    )


def two_parent_fixture():
    """P1=a (pid 1, 10), P2=a (pid 2, 0), child C1 references 1 with 10."""
    return {
        "schema": "fixture/1", "kind": "normal", "target": None,
        "slots": {
            "P1": {"candidate_id": "a", "row": {"pid": 1, "total": 10}},
            "P2": {"candidate_id": "a", "row": {"pid": 2, "total": 0}},
            "C1": {"candidate_id": "x",
                   "row": {"cid": 1, "pref": 1, "amt": 10}},
        },
        "rules": {"U": True, "F": True, "A": True},
    }


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.contract = load_contract(os.path.join(EXAMPLES, "billing.json"))
        self.fixture = load(NORMAL)
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "ledger.db")
        self.ledger = init_ledger(self.db, self.contract, self.fixture)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def apply(self, seq, changes):
        return self.ledger.apply_payload({"seq": seq, "changes": changes})

    def reference_truth(self):
        return evaluate_rules(self.contract, self.ledger.assignment)


class InitializationTests(LedgerCase):
    def test_sequence_starts_at_zero_and_selection_is_fixture(self):
        self.assertEqual(self.ledger.current_seq, 0)
        self.assertEqual(self.ledger.selection(), {
            "S01-client": "c1", "S02-client": "d3", "S03-branch": "b2",
            "S04-invoice": "i4", "S05-invoice": "j2",
            "S10-free-audit": "a1", "S11-free-audit": "a1",
        })
        self.assertEqual(self.ledger.flow(), [])

    def test_truth_is_recomputed_not_trusted(self):
        self.assertEqual(self.reference_truth(), self.ledger.rules())
        tampered = copy.deepcopy(self.fixture)
        tampered["rules"]["U-client-id"] = False
        other_db = os.path.join(self.tmp.name, "tampered.db")
        with self.assertRaises(LedgerError):
            init_ledger(other_db, self.contract, tampered)

    def test_selected_row_not_matching_candidate_is_rejected(self):
        tampered = copy.deepcopy(self.fixture)
        tampered["slots"]["S01-client"]["row"]["billed_total"] = 999
        with self.assertRaises(LedgerError):
            init_ledger(os.path.join(self.tmp.name, "badrow.db"),
                        self.contract, tampered)

    def test_refuses_to_overwrite_existing_database(self):
        with self.assertRaises(LedgerError):
            init_ledger(self.db, self.contract, self.fixture)


class IncrementalTruthTests(LedgerCase):
    def test_every_step_matches_reference_and_flow_diffs(self):
        steps = [
            [{"slot_id": "S01-client", "candidate_id": "c2"}],
            [{"slot_id": "S04-invoice", "candidate_id": "i5"}],
            [{"slot_id": "S02-client", "candidate_id": "d2"}],
            [{"slot_id": "S01-client", "candidate_id": "c1"}],
            [{"slot_id": "S02-client", "candidate_id": "d3"},
             {"slot_id": "S05-invoice", "candidate_id": "j3"}],
        ]
        previous = self.reference_truth()
        for seq, changes in enumerate(steps, 1):
            self.apply(seq, changes)
            now = self.reference_truth()
            expected_false = sorted(r for r in now
                                    if previous[r] and not now[r])
            expected_recovered = sorted(r for r in now
                                        if not previous[r] and now[r])
            entry = self.ledger.flow()[-1]
            self.assertEqual(list(entry.newly_false), expected_false)
            self.assertEqual(list(entry.recovered), expected_recovered)
            self.assertEqual(self.ledger.rules(), now)
            previous = now

    def test_false_rule_does_not_reject_next_transaction(self):
        result = self.apply(1, [{"slot_id": "S01-client",
                                 "candidate_id": "c2"}])
        self.assertEqual(list(result.newly_false), ["F-invoice-client"])
        self.assertEqual(self.ledger.false_rules(), ["F-invoice-client"])
        self.apply(2, [{"slot_id": "S01-client", "candidate_id": "c1"}])
        self.assertEqual(self.ledger.false_rules(), [])
        self.assertEqual(list(self.ledger.flow()[-1].recovered),
                         ["F-invoice-client"])

    def test_swap_is_not_a_unique_failure(self):
        # c1 (client 1) -> c2 (client 2); d3 (client 5) -> d2 (client 1).
        # Within the one transaction the two parent rows exchange identity;
        # no duplicate exists after the transaction, so U-client-id is not
        # reported even though a row-by-row intermediate state would collide.
        result = self.apply(1, [
            {"slot_id": "S01-client", "candidate_id": "c2"},
            {"slot_id": "S02-client", "candidate_id": "d2"},
        ])
        self.assertNotIn("U-client-id", result.newly_false)
        # The unchanged S04 invoice still references client 1, whose row has
        # moved to S02 and now claims 0: the aggregate is the rule that fails.
        self.assertEqual(list(result.newly_false), ["A-client-billed"])
        self.assertEqual(self.ledger.selection()["S02-client"], "d2")

    def test_swap_the_other_way(self):
        # Starting selection is c1/d3.  Swap to d2 (client 1) on S02 first and
        # then away from client 1 on S01 within one transaction.
        self.apply(1, [{"slot_id": "S02-client", "candidate_id": "d2"}])
        # This one really is a duplicate: S01 is still client 1.  The two
        # client-1 parent rows now disagree with the summed children.
        self.assertEqual(self.ledger.false_rules(),
                         ["A-client-billed", "U-client-id"])
        result = self.apply(2, [
            {"slot_id": "S01-client", "candidate_id": "c3"},
            {"slot_id": "S02-client", "candidate_id": "d1"},
        ])
        self.assertEqual(list(result.recovered), ["U-client-id"])

    def test_unchanged_child_reacts_to_parent_key_change(self):
        # S05 j2 currently has a wholly-null client reference, so moving the
        # parent key alone must not fail the FK.  S04 i4 points at client 1.
        result = self.apply(1, [{"slot_id": "S01-client",
                                 "candidate_id": "c3"}])
        self.assertIn("F-invoice-client", result.newly_false)
        # The unchanged S04 invoice now dangles, and the unchanged S05
        # invoice (wholly null) contributes nowhere, so the aggregate fails.
        self.assertIn("A-client-billed", result.newly_false)
        # Moving the free audit keys changes nothing anywhere.
        result = self.apply(2, [{"slot_id": "S10-free-audit",
                                 "candidate_id": "a3"}])
        self.assertEqual(list(result.newly_false), [])
        self.assertEqual(list(result.recovered), [])

    def test_unchanged_child_reacts_to_new_parent_amount(self):
        # S04 i4 already references client 1; its amount is 30 and the client
        # claims 30.  Rewriting S04 with i2 (client 1, amount 20) breaks the
        # aggregate while every FK stays true.
        result = self.apply(1, [{"slot_id": "S04-invoice",
                                 "candidate_id": "i2"}])
        self.assertEqual(list(result.newly_false), ["A-client-billed"])
        self.assertEqual(list(result.recovered), [])

    def test_aggregate_failure_through_unchanged_parent(self):
        # S04 i4 contributes 30 to client 1.  i2 also references client 1 but
        # contributes 20, so the aggregate fails while the unchanged parent
        # row S01 still claims 30.  FK stays true.
        result = self.apply(1, [{"slot_id": "S04-invoice",
                                 "candidate_id": "i2"}])
        self.assertEqual(list(result.newly_false), ["A-client-billed"])
        self.assertNotIn("F-invoice-client", result.newly_false)
        result = self.apply(2, [{"slot_id": "S04-invoice",
                                 "candidate_id": "i4"}])
        self.assertEqual(list(result.recovered), ["A-client-billed"])
        # Moving the parent key now strands the unchanged invoice i4.
        result = self.apply(3, [{"slot_id": "S01-client",
                                 "candidate_id": "c3"}])
        self.assertEqual(list(result.newly_false),
                         ["A-client-billed", "F-invoice-client"])

    def test_no_op_change_still_records_a_flow_entry(self):
        result = self.apply(1, [{"slot_id": "S01-client",
                                 "candidate_id": "c1"}])
        self.assertEqual(result.status, "applied")
        self.assertEqual(list(result.newly_false), [])
        self.assertEqual(list(result.recovered), [])
        self.assertEqual(len(self.ledger.flow()), 1)


class IdentitySwapTests(unittest.TestCase):
    """Two parent rows exchanging their primary keys in one transaction."""

    def setUp(self):
        self.contract = parse_contract(two_parent_doc())
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "swap.db")
        self.ledger = init_ledger(self.db, self.contract, two_parent_fixture())

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_identity_swap_does_not_report_uniqueness_failure(self):
        result = self.ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"},
            {"slot_id": "P2", "candidate_id": "b"},
        ]})
        # P1 becomes pid 2, P2 becomes pid 1: no duplicate before or after,
        # so U must not be recorded.  The unchanged child still references
        # pid 1, which is still present (F holds), but the row that now owns
        # pid 1 carries total 0 while the child sums to 10, so A fails.
        self.assertNotIn("U", result.newly_false)
        self.assertEqual(list(result.newly_false), ["A"])
        self.assertTrue(self.ledger.rules()["F"])
        self.assertFalse(self.ledger.rules()["A"])

    def test_one_sided_move_is_a_real_duplicate(self):
        result = self.ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"}]})
        # pid 2 now appears twice (U), pid 1 disappeared (child F fails), and
        # both pid-2 parents disagree with the sums (A).
        self.assertIn("U", result.newly_false)
        self.assertEqual(self.ledger.false_rules(), ["A", "F", "U"])

    def test_swap_back_recovers_both(self):
        self.ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"},
            {"slot_id": "P2", "candidate_id": "b"}]})
        result = self.ledger.apply_payload({"seq": 2, "changes": [
            {"slot_id": "P1", "candidate_id": "a"},
            {"slot_id": "P2", "candidate_id": "a"}]})
        self.assertEqual(list(result.recovered), ["A"])
        self.assertEqual(self.ledger.false_rules(), [])

    def test_swap_with_child_move_in_same_transaction(self):
        # Swap the parent identities and move the child to pid 2 in the same
        # transaction: every rule is true afterwards, so nothing is recorded
        # even though a row-by-row update would see a temporary duplicate.
        result = self.ledger.apply_payload({"seq": 1, "changes": [
            {"slot_id": "P1", "candidate_id": "b"},
            {"slot_id": "P2", "candidate_id": "b"},
            {"slot_id": "C1", "candidate_id": "y"},
        ]})
        self.assertNotIn("U", result.newly_false)
        self.assertEqual(list(result.newly_false), [])
        self.assertEqual(list(result.recovered), [])
        self.assertEqual(self.ledger.false_rules(), [])


class AggregateEdgeCaseTests(unittest.TestCase):
    """Aggregate bookkeeping must distinguish zero sums from absent keys."""

    def test_cancelling_child_amounts_do_not_drop_a_key(self):
        doc = contract(
            [table("P", "parent",
                   [("pid", "int", False), ("total", "int", True)], ["pid"]),
             table("C", "child",
                   [("cid", "int", False), ("pref", "int", True),
                    ("amt", "int", True)])],
            [slot("P1", "P", [("p", {"pid": 1, "total": 0})]),
             slot("C1", "C", [("a", {"cid": 1, "pref": 1, "amt": 5}),
                              ("b", {"cid": 2, "pref": 1, "amt": -5}),
                              ("c", {"cid": 3, "pref": 1, "amt": 7})]),
             slot("C2", "C", [("a", {"cid": 4, "pref": 1, "amt": -5})])],
            [{"id": "A", "kind": "aggregate", "child": "C",
              "child_fields": ["pref"], "child_amount": "amt",
              "parent": "P", "parent_key": ["pid"],
              "parent_amount": "total"}],
        )
        c = parse_contract(doc)
        fixture = {
            "schema": "fixture/1", "kind": "normal", "target": None,
            "slots": {"P1": {"candidate_id": "p",
                             "row": {"pid": 1, "total": 0}},
                      "C1": {"candidate_id": "a",
                             "row": {"cid": 1, "pref": 1, "amt": 5}},
                      "C2": {"candidate_id": "a",
                             "row": {"cid": 4, "pref": 1, "amt": -5}}},
            "rules": {"A": True},
        }
        tmp = tempfile.TemporaryDirectory()
        try:
            ledger = init_ledger(os.path.join(tmp.name, "l.db"), c, fixture)
            try:
                # Replace +5 with -5: the key sum becomes 0 but a child still
                # matches, so the aggregate stays true; it must not crash and
                # must not lose the key.
                result = ledger.apply_payload({"seq": 1, "changes": [
                    {"slot_id": "C1", "candidate_id": "b"}]})
                self.assertEqual(list(result.newly_false), ["A"])
                self.assertEqual(ledger.rules(), {"A": False})
                result = ledger.apply_payload({"seq": 2, "changes": [
                    {"slot_id": "C1", "candidate_id": "a"}]})
                self.assertEqual(list(result.recovered), ["A"])
                self.assertEqual(ledger.rules(), {"A": True})
            finally:
                ledger.close()
        finally:
            tmp.cleanup()


class ValidationTests(LedgerCase):
    def bad(self, payload):
        with self.assertRaises((InputError, LedgerError)):
            self.ledger.apply_payload(payload)

    def test_rejects_unknown_slot_and_candidate_and_duplicates(self):
        self.bad({"seq": 1, "changes": [{"slot_id": "S99",
                                          "candidate_id": "x"}]})
        self.bad({"seq": 1, "changes": [{"slot_id": "S01-client",
                                          "candidate_id": "zzz"}]})
        self.bad({"seq": 1, "changes": [
            {"slot_id": "S01-client", "candidate_id": "c1"},
            {"slot_id": "S01-client", "candidate_id": "c2"},
        ]})
        self.bad({"seq": 1, "changes": []})
        self.bad({"seq": 1, "changes": [{"slot_id": "S01-client"}]})
        self.bad({"seq": 0, "changes": [{"slot_id": "S01-client",
                                         "candidate_id": "c1"}]})
        self.bad({"seq": True, "changes": [{"slot_id": "S01-client",
                                            "candidate_id": "c1"}]})
        self.bad({"seq": 1, "changes": [{"slot_id": "S01-client",
                                          "candidate_id": "c1"}],
                  "add_slot": {}})
        self.assertEqual(self.ledger.current_seq, 0)

    def test_stop_message_reports_reason_and_line(self):
        lines = [
            json.dumps({"seq": 1, "changes": [
                {"slot_id": "S01-client", "candidate_id": "c2"}]}),
            json.dumps({"seq": 2, "changes": [
                {"slot_id": "S01-client", "candidate_id": "c1"},
                {"slot_id": "S01-client", "candidate_id": "c3"}]}),
        ]
        result = self.ledger.replay(lines, source="bad.jsonl")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.line, 2)
        self.assertEqual(result.error.source, "bad.jsonl")
        self.assertIn("more than once", result.error.reason)
        self.assertIn("bad.jsonl:2:", str(result.error))

    def test_rejects_gap(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        with self.assertRaises(InputError) as ctx:
            self.apply(3, [{"slot_id": "S01-client", "candidate_id": "c1"}])
        self.assertIn("expected 2", str(ctx.exception))
        self.assertEqual(self.ledger.current_seq, 1)

    def test_duplicate_sequence_is_idempotent(self):
        first = self.apply(1, [{"slot_id": "S01-client",
                                "candidate_id": "c2"},
                               {"slot_id": "S05-invoice",
                                "candidate_id": "j3"}])
        again = self.apply(1, [{"slot_id": "S05-invoice", "candidate_id": "j3"},
                               {"slot_id": "S01-client", "candidate_id": "c2"}])
        self.assertEqual(again.status, "skipped")
        self.assertEqual(len(self.ledger.flow()), 1)
        self.assertEqual(again.newly_false, first.newly_false)
        self.assertEqual(self.ledger.current_seq, 1)

    def test_conflicting_historical_content_rejected(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        with self.assertRaises(InputError) as ctx:
            self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c3"}])
        self.assertIn("different content", str(ctx.exception))
        with self.assertRaises(InputError):
            self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"},
                           {"slot_id": "S02-client", "candidate_id": "d1"}])

    def test_duplicate_resubmission_ignores_changes_order(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"},
                       {"slot_id": "S05-invoice", "candidate_id": "j3"}])
        flow = [e.to_dict() for e in self.ledger.flow()]
        result = self.apply(1, [{"slot_id": "S05-invoice", "candidate_id": "j3"},
                               {"slot_id": "S01-client", "candidate_id": "c2"}])
        self.assertEqual(result.status, "skipped")
        self.assertEqual([e.to_dict() for e in self.ledger.flow()], flow)

    def test_replay_stops_at_error_line_and_keeps_prefix(self):
        lines = [
            json.dumps({"seq": 1, "changes": [{"slot_id": "S01-client",
                                               "candidate_id": "c2"}]}),
            json.dumps({"seq": 2, "changes": [{"slot_id": "S05-invoice",
                                               "candidate_id": "j3"}]}),
            json.dumps({"seq": 3, "changes": [{"slot_id": "S01-client",
                                               "candidate_id": "nope"}]}),
            json.dumps({"seq": 4, "changes": [{"slot_id": "S01-client",
                                               "candidate_id": "c1"}]}),
        ]
        result = self.ledger.replay(lines, source="batch.jsonl")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.line, 3)
        self.assertEqual(result.error.source, "batch.jsonl")
        self.assertEqual(result.applied, 2)
        self.assertEqual(self.ledger.current_seq, 2)
        self.assertEqual(len(self.ledger.flow()), 2)

    def test_replay_of_committed_prefix_resumes(self):
        lines = [
            json.dumps({"seq": 1, "changes": [{"slot_id": "S01-client",
                                               "candidate_id": "c2"}]}),
            json.dumps({"seq": 2, "changes": [{"slot_id": "S02-client",
                                               "candidate_id": "d1"}]}),
            json.dumps({"seq": 3, "changes": [{"slot_id": "S03-branch",
                                               "candidate_id": "b1"}]}),
        ]
        first = self.ledger.replay(lines[:2])
        self.assertEqual(first.applied, 2)
        second = self.ledger.replay(lines)
        self.assertTrue(second.ok)
        self.assertEqual(second.skipped, 2)
        self.assertEqual(second.applied, 1)
        self.assertEqual(self.ledger.current_seq, 3)

    def test_invalid_json_and_blank_lines_are_reported(self):
        result = self.ledger.replay(['not json\n'])
        self.assertEqual(result.error.line, 1)
        result = self.ledger.replay(['\n'])
        self.assertEqual(result.error.line, 1)


class ReopenTests(LedgerCase):
    def test_reopen_preserves_prefix_and_continues(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        self.apply(2, [{"slot_id": "S04-invoice", "candidate_id": "i1"}])
        selection = self.ledger.selection()
        rules = self.ledger.rules()
        flow = [e.to_dict() for e in self.ledger.flow()]
        self.ledger.close()

        reopened = open_ledger(self.db, self.contract)
        try:
            self.assertEqual(reopened.current_seq, 2)
            self.assertEqual(reopened.selection(), selection)
            self.assertEqual(reopened.rules(), rules)
            self.assertEqual([e.to_dict() for e in reopened.flow()], flow)
            reopened.apply_payload({"seq": 3, "changes": [
                {"slot_id": "S01-client", "candidate_id": "c1"}]})
            self.assertEqual(reopened.current_seq, 3)
        finally:
            reopened.close()
        self.ledger = open_ledger(self.db, self.contract)

    def test_history_audit_reopen(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        self.apply(2, [{"slot_id": "S02-client", "candidate_id": "d2"}])
        self.ledger.close()
        audited = open_ledger(self.db, self.contract, verify_history=True)
        try:
            self.assertEqual(audited.current_seq, 2)
        finally:
            audited.close()
        self.ledger = open_ledger(self.db, self.contract)

    def test_interrupted_commit_leaves_no_half_record(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        selection = self.ledger.selection()

        def explode(ledger, transaction):
            raise RuntimeError("simulated crash after the row writes")

        crash_db = os.path.join(self.tmp.name, "crash.db")
        crashing = init_ledger(crash_db, self.contract, self.fixture,
                               before_commit_hook=explode)
        with self.assertRaises(RuntimeError):
            crashing.apply_payload({"seq": 1, "changes": [
                {"slot_id": "S02-client", "candidate_id": "d2"}]})
        # the failed writer rolls back and reloads the committed prefix
        self.assertEqual(crashing.current_seq, 0)
        self.assertEqual(crashing.selection(),
                         {s: c.candidate_id for s, c in
                          crashing.assignment.items()})
        crashing.close()
        reopened = open_ledger(crash_db, self.contract)
        try:
            self.assertEqual(reopened.current_seq, 0)
            self.assertEqual(reopened.flow(), [])
            self.assertEqual(
                reopened.selection(),
                {"S01-client": "c1", "S02-client": "d3", "S03-branch": "b2",
                 "S04-invoice": "i4", "S05-invoice": "j2",
                 "S10-free-audit": "a1", "S11-free-audit": "a1"})
        finally:
            reopened.close()
        self.assertEqual(selection["S01-client"], "c2")

    def test_crash_mid_batch_then_resubmit_full_batch(self):
        # The batch includes the already committed prefix; only new sequences
        # are applied and the flow stays contiguous with no duplicates.
        lines = [
            json.dumps({"seq": seq, "changes": [
                {"slot_id": slot, "candidate_id": candidate}]})
            for seq, slot, candidate in [
                (1, "S03-branch", "b1"), (2, "S01-client", "c2"),
                (3, "S01-client", "c1"), (4, "S03-branch", "b2"),
                (5, "S04-invoice", "i2"), (6, "S04-invoice", "i4"),
            ]
        ]
        self.apply(1, [{"slot_id": "S03-branch", "candidate_id": "b1"}])
        self.apply(2, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        self.ledger.close()
        self.ledger = open_ledger(self.db, self.contract)
        result = self.ledger.replay(lines, source="batch")
        self.assertTrue(result.ok)
        self.assertEqual(result.skipped, 2)
        self.assertEqual(result.applied, 4)
        self.assertEqual([e.seq for e in self.ledger.flow()],
                         [1, 2, 3, 4, 5, 6])
        self.assertEqual(self.ledger.false_rules(), [])

    def test_tampered_selection_detected_on_reopen(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        self.ledger.close()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE slot_state SET candidate_id='c3' "
                     "WHERE slot_id='S01-client'")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            open_ledger(self.db, self.contract)
        # reopen the untampered database for tearDown
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE slot_state SET candidate_id='c2' "
                     "WHERE slot_id='S01-client'")
        conn.commit()
        conn.close()
        self.ledger = open_ledger(self.db, self.contract)

    def test_tampered_flow_detected_only_with_history_audit(self):
        self.apply(1, [{"slot_id": "S01-client", "candidate_id": "c2"}])
        self.ledger.close()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE flow SET changes='[[\"S01-client\",\"c1\"]]' "
                     "WHERE seq=1")
        conn.commit()
        conn.close()
        with self.assertRaises(LedgerError):
            open_ledger(self.db, self.contract, verify_history=True)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE flow SET changes='[[\"S01-client\",\"c2\"]]' "
                     "WHERE seq=1")
        conn.commit()
        conn.close()
        self.ledger = open_ledger(self.db, self.contract)


class ContractAndScalingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.contract = load_contract(os.path.join(EXAMPLES, "billing.json"))
        self.fixture = load(NORMAL)
        self.db = os.path.join(self.tmp.name, "ledger.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_contract_change_is_refused(self):
        ledger = init_ledger(self.db, self.contract, self.fixture)
        ledger.close()
        changed = self.contract.raw
        changed["contract_id"] = "other-contract"
        from fixturegen.contract import parse_contract
        other = parse_contract(changed)
        with self.assertRaises(LedgerError):
            open_ledger(self.db, other)

    def test_transaction_only_updates_changed_slots(self):
        # Only rule states on the changed slot's table may be touched, and
        # always with exactly the changed slot.
        import fixturegen.ledger_rules as rules_module

        calls = []
        original_update = rules_module.UniqueState.update
        original_fk = rules_module.FkState.update
        original_agg = rules_module.AggregateState.update

        def wrap(original):
            def inner(self, slot_id, old, new):
                calls.append((self.rule.id, slot_id))
                return original(self, slot_id, old, new)
            return inner

        rules_module.UniqueState.update = wrap(original_update)
        rules_module.FkState.update = wrap(original_fk)
        rules_module.AggregateState.update = wrap(original_agg)
        try:
            ledger = init_ledger(self.db, self.contract, self.fixture)
            try:
                ledger.apply_payload({"seq": 1, "changes": [
                    {"slot_id": "S10-free-audit", "candidate_id": "a2"}]})
                self.assertEqual(calls, [])  # no rule mentions AUDIT_TRAIL
                calls.clear()
                ledger.apply_payload({"seq": 2, "changes": [
                    {"slot_id": "S04-invoice", "candidate_id": "i2"}]})
                self.assertEqual({slot for _rule, slot in calls},
                                 {"S04-invoice"})
                # U-invoice-id, F-invoice-client, F-invoice-branch and
                # A-client-billed all mention INVOICE; the two CLIENT-only
                # uniqueness rules must not be touched.
                self.assertEqual({rule for rule, _slot in calls}, {
                    "U-invoice-id", "F-invoice-client", "F-invoice-branch",
                    "A-client-billed"})
            finally:
                ledger.close()
        finally:
            rules_module.UniqueState.update = original_update
            rules_module.FkState.update = original_fk
            rules_module.AggregateState.update = original_agg

    def test_flow_lists_sorted_rule_ids(self):
        ledger = init_ledger(self.db, self.contract, self.fixture)
        try:
            ledger.apply_payload({"seq": 1, "changes": [
                {"slot_id": "S01-client", "candidate_id": "c2"},
                {"slot_id": "S04-invoice", "candidate_id": "i5"}]})
            entry = ledger.flow()[0]
            self.assertEqual(list(entry.newly_false),
                             sorted(entry.newly_false))
            self.assertTrue(list(entry.newly_false))
        finally:
            ledger.close()


if __name__ == "__main__":
    unittest.main()
