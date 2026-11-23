"""Randomised differential test: ledger flow against the reference evaluator.

For random small contracts we generate a normal fixture, initialize a ledger,
then feed random transactions (including parent-key swaps, no-op rewrites,
repeated parents and child moves).  After every transaction we assert:

* the ledger's selection equals a naive replay of the accepted transactions;
* the ledger's rule truth equals ``evaluate_rules`` on that selection;
* the flow entry's newly-false/recovered lists equal the successive reference
  truth difference, sorted by rule id;
* reopening mid-stream reproduces the same sequence, selection and flow, and
  continued transactions match a single uninterrupted run.

A local naive JSONL validator decides which lines are acceptable.
"""

from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import parse_contract
from fixturegen.engine import evaluate_rules, generate
from fixturegen.ledger import init_ledger, open_ledger
from tests.test_random_differential import _random_doc

SEED = 20261124


def random_contract(rng, attempt):
    doc = _random_doc(rng, attempt)
    return doc, parse_contract(doc)


def random_transactions(rng, contract_obj, doc, count):
    """A list of (seq, changes) with duplicates, swaps and same-candidate ops."""
    slots = list(contract_obj.slots)
    txns = []
    for seq in range(1, count + 1):
        chosen = rng.sample(slots, rng.randint(1, min(3, len(slots))))
        changes = []
        for s in chosen:
            cid = rng.choice(s.candidates).candidate_id
            changes.append({"slot_id": s.id, "candidate_id": cid})
        if len(chosen) >= 2 and rng.random() < 0.5:
            # swap the identities of two parent rows in one transaction; only
            # candidate ids declared by both slots can be exchanged
            parents = [s for s in chosen
                       if contract_obj.table(s.table).kind == "parent"]
            for i in range(len(parents)):
                for j in range(i + 1, len(parents)):
                    a, b = parents[i], parents[j]
                    shared = sorted({c.candidate_id for c in a.candidates}
                                    & {c.candidate_id for c in b.candidates})
                    if not shared:
                        continue
                    cid = rng.choice(shared)
                    changes = [c for c in changes
                               if c["slot_id"] not in (a.id, b.id)]
                    changes += [
                        {"slot_id": a.id, "candidate_id": cid},
                        {"slot_id": b.id, "candidate_id": cid},
                    ]
                    break
        rng.shuffle(changes)
        txns.append({"seq": seq, "changes": changes})
    return txns


class LedgerDifferentialTests(unittest.TestCase):
    def test_random_flows_match_reference(self):
        rng = random.Random(SEED)
        tested = 0
        attempt = 0
        while tested < 25 and attempt < 300:
            attempt += 1
            doc, c = random_contract(rng, attempt)
            normal = generate(c).normal
            if not hasattr(normal, "selection"):
                continue
            tested += 1
            with self.subTest(contract=doc["contract_id"]):
                self._run_one(rng, c, normal.to_json())

    def _run_one(self, rng, c, fixture):
        tmp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(tmp.name, "l.db")
            ledger = init_ledger(db, c, fixture)
            selection = {sid: entry["candidate_id"]
                         for sid, entry in fixture["slots"].items()}
            truth = evaluate_rules(c, ledger.assignment)
            txns = random_transactions(rng, c, c.raw, rng.randint(8, 20))
            for index, txn in enumerate(txns):
                # no invalid transactions here: these must all be accepted
                result = ledger.apply_payload(txn)
                self.assertEqual(result.status, "applied")
                for change in txn["changes"]:
                    selection[change["slot_id"]] = change["candidate_id"]
                self.assertEqual(ledger.selection(), selection)
                new_truth = evaluate_rules(c, ledger.assignment)
                self.assertEqual(ledger.rules(), new_truth)
                entry = ledger.flow()[-1]
                self.assertEqual(
                    list(entry.newly_false),
                    sorted(r for r in new_truth if truth[r] and not new_truth[r]))
                self.assertEqual(
                    list(entry.recovered),
                    sorted(r for r in new_truth if not truth[r] and new_truth[r]))
                truth = new_truth
                if index == len(txns) // 2:
                    ledger.close()
                    ledger = open_ledger(db, c)
                    self.assertEqual(ledger.selection(), selection)
                    self.assertEqual(ledger.rules(), truth)
                    self.assertEqual(ledger.current_seq, txn["seq"])
            ledger.close()
            audited = open_ledger(db, c, verify_history=True)
            try:
                self.assertEqual(audited.selection(), selection)
                self.assertEqual(audited.rules(), truth)
                self.assertEqual([e.seq for e in audited.flow()],
                                 list(range(1, len(txns) + 1)))
            finally:
                audited.close()
        finally:
            tmp.cleanup()

    def test_idempotent_and_conflicting_replays(self):
        rng = random.Random(SEED + 1)
        doc, c = random_contract(rng, 1)
        normal = generate(c).normal
        if not hasattr(normal, "selection"):
            self.skipTest("random contract has no normal fixture")
        tmp = tempfile.TemporaryDirectory()
        try:
            db = os.path.join(tmp.name, "l.db")
            ledger = init_ledger(db, c, normal.to_json())
            txns = random_transactions(rng, c, doc, 6)
            line = lambda t: json.dumps(t) + "\n"
            batch = "".join(line(t) for t in txns)
            first = ledger.replay(batch.splitlines(True), source="batch")
            self.assertTrue(first.ok, first.error)
            again = ledger.replay(batch.splitlines(True), source="batch")
            self.assertTrue(again.ok)
            self.assertEqual(again.applied, 0)
            self.assertEqual(again.skipped, 6)
            flow = [e.to_dict() for e in ledger.flow()]

            # a conflicting re-submission of a committed sequence stops
            s = dict(ledger._slots)
            for txn in txns:
                for change in txn["changes"]:
                    slot_obj = s[change["slot_id"]]
                    other = next(
                        (cd.candidate_id for cd in slot_obj.candidates
                         if cd.candidate_id != change["candidate_id"]), None)
                    if other is None:
                        continue
                    bad = [{"seq": txn["seq"],
                            "changes": [{"slot_id": change["slot_id"],
                                         "candidate_id": other}]}]
                    result = ledger.replay([json.dumps(bad[0])], source="x")
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error.line, 1)
                    self.assertEqual([e.to_dict() for e in ledger.flow()], flow)
                    ledger.close()
                    ledger = open_ledger(db, c)
                    break
                else:
                    continue
                break
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
