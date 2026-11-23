"""Randomised differential tests against an independent naive enumerator.

For small contracts (at most a few slots with a few candidates) the whole
identity space can be enumerated in the test.  We compare, for many random
contracts:

* the normal fixture selection (existence and lexicographic tie-break),
* every single-rule isolation,
* the "no fixture" conclusion and its replayable material,

against a from-scratch evaluator and enumerator that share no code with the
package.  Input reordering is also checked to have no effect.
"""

from __future__ import annotations

import copy
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import ContractError, parse_contract  # noqa: E402
from fixturegen.engine import Fixture, NoFixture, generate  # noqa: E402
from fixturegen.material import replay_material  # noqa: E402
from tests.helpers import contract, naive_first, slot, table  # noqa: E402

SEED = 20261123


def _random_doc(rng: random.Random, index: int):
    """A tiny two-table contract with random candidates."""
    parent_fields = [("pid", "int", True), ("pcode", "str", True),
                     ("total", "int", True)]
    child_fields = [("cid", "int", False), ("pref", "int", True),
                    ("pcode_ref", "str", True), ("amt", "int", True)]
    tables = [
        table("P", "parent", parent_fields, ["pid"]),
        table("C", "child", child_fields),
    ]

    def maybe_null(rng_, value):
        return None if rng_.random() < 0.2 else value

    def maybe_omit(row, names, rng_):
        row = dict(row)
        for name in names:
            if rng_.random() < 0.1:
                row.pop(name, None)
        return row

    pid_values = [None, 1, 2]
    pcode_values = [None, "x", "y"]
    parent_slots = []
    for i in range(rng.randint(1, 2)):
        cands = []
        for k in range(rng.randint(1, 3)):
            cid = chr(ord("a") + k)
            row = {
                "pid": maybe_null(rng, rng.choice(pid_values)),
                "pcode": maybe_null(rng, rng.choice(pcode_values)),
                "total": maybe_null(rng, rng.choice([0, 0, 1, 5, -1])),
            }
            row = maybe_omit(row, ["pid", "pcode", "total"], rng)
            cands.append((cid, row))
        parent_slots.append(slot(f"P{i}", "P", cands))

    child_slots = []
    for i in range(rng.randint(1, 2)):
        cands = []
        for k in range(rng.randint(1, 3)):
            cid = chr(ord("a") + k)
            row = {
                "cid": i * 10 + k,
                "pref": maybe_null(rng, rng.choice(pid_values)),
                "pcode_ref": maybe_null(rng, rng.choice(pcode_values)),
                "amt": maybe_null(rng, rng.choice([0, 1, 2, -1])),
            }
            row = maybe_omit(row, ["pref", "pcode_ref", "amt"], rng)
            cands.append((cid, row))
        child_slots.append(slot(f"C{i}", "C", cands))

    rules = []
    # always an FK and aggregate on the int reference
    rules.append({"id": "F", "kind": "fk", "child": "C",
                  "child_fields": ["pref"], "parent": "P",
                  "parent_key": ["pid"]})
    rules.append({"id": "A", "kind": "aggregate", "child": "C",
                  "child_fields": ["pref"], "child_amount": "amt",
                  "parent": "P", "parent_key": ["pid"],
                  "parent_amount": "total", "reference_rule": "F"})
    # a second key group sometimes, plus a uniqueness rule
    if rng.random() < 0.5:
        rules.append({"id": "F2", "kind": "fk", "child": "C",
                      "child_fields": ["pcode_ref"], "parent": "P",
                      "parent_key": ["pcode"]})
    if rng.random() < 0.8:
        rules.append({"id": "U", "kind": "unique", "table": "P",
                      "key": ["pid"]})
    if rng.random() < 0.4:
        rules.append({"id": "UP", "kind": "unique", "table": "P",
                      "key": ["pcode"]})
    if rng.random() < 0.3:
        rules.append({"id": "UC", "kind": "unique", "table": "C",
                      "key": ["cid"]})
    rng.shuffle(rules)
    return contract(tables, parent_slots + child_slots, rules,
                    contract_id=f"random-{index}")


class RandomDifferentialTests(unittest.TestCase):
    def test_random_contracts_match_naive_enumerator(self):
        rng = random.Random(SEED)
        tested = 0
        attempt = 0
        while tested < 60 and attempt < 400:
            attempt += 1
            doc = _random_doc(rng, attempt)
            try:
                c = parse_contract(doc)
            except ContractError:
                continue
            tested += 1
            with self.subTest(contract=doc["contract_id"]):
                self._check_contract(doc, c)

    def _check_contract(self, doc, c):
        rule_ids = [r["id"] for r in doc["rules"]]
        expected = naive_first(doc, rule_ids)
        report = generate(c, isolate=rule_ids)
        payload = report.to_json()

        if expected is None:
            self.assertIsInstance(report.normal, NoFixture)
            self.assertEqual(report.normal.status, "proved")
            self.assertTrue(replay_material(
                report.normal.to_json()["material"], max_assignments=10_000)["ok"])
        else:
            self.assertIsInstance(report.normal, Fixture)
            self.assertEqual(
                {sid: v["candidate_id"] for sid, v in report.normal.selection.items()},
                expected)

        for target in rule_ids:
            with self.subTest(target=target):
                others = [rid for rid in rule_ids if rid != target]
                want = naive_first(doc, others, [target])
                entry = report.isolations[target]
                if want is None:
                    self.assertIsInstance(entry, NoFixture, entry)
                    self.assertEqual(entry.status, "proved")
                    material = entry.to_json()["material"]
                    self.assertTrue(replay_material(
                        material, max_assignments=10_000)["ok"])
                else:
                    self.assertIsInstance(entry, Fixture, entry)
                    self.assertEqual(
                        {sid: v["candidate_id"]
                         for sid, v in entry.selection.items()}, want)
                    self.assertFalse(entry.rule_results[target])
                    self.assertTrue(all(v for k, v in entry.rule_results.items()
                                        if k != target))
        return payload

    def test_random_contracts_order_invariant(self):
        rng = random.Random(SEED + 1)
        tested = 0
        attempt = 0
        while tested < 30 and attempt < 200:
            attempt += 1
            doc = _random_doc(rng, attempt)
            try:
                c = parse_contract(doc)
            except ContractError:
                continue
            tested += 1
            shuffled = copy.deepcopy(doc)
            shuffled["slots"].reverse()
            for s in shuffled["slots"]:
                s["candidates"].reverse()
            shuffled["rules"].reverse()
            rule_ids = [r["id"] for r in doc["rules"]]
            a = generate(c, isolate=rule_ids)
            b = generate(parse_contract(shuffled), isolate=list(reversed(rule_ids)))
            self.assertEqual(a.to_json()["normal"], b.to_json()["normal"])
            for rid in rule_ids:
                self.assertEqual(a.isolations[rid].to_json()["target"],
                                 b.isolations[rid].to_json()["target"])
                if isinstance(a.isolations[rid], Fixture):
                    self.assertEqual(
                        {s: v["candidate_id"]
                         for s, v in a.isolations[rid].selection.items()},
                        {s: v["candidate_id"]
                         for s, v in b.isolations[rid].selection.items()})


if __name__ == "__main__":
    unittest.main()
