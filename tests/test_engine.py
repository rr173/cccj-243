"""Generator tests: normal fixture, isolation, no-fixture, tie-breaking,
scaling behaviour, and independent brute-force cross-checks."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import parse_contract  # noqa: E402
from fixturegen.engine import (  # noqa: E402
    Fixture,
    NoFixture,
    build_analysis,
    generate,
    verify_report,
)
from fixturegen.material import replay_material  # noqa: E402
from tests import helpers  # noqa: E402
from tests.helpers import contract, naive_first, slot, table  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "examples")


def billing_doc():
    with open(os.path.join(EXAMPLES, "billing.json"), encoding="utf-8") as fh:
        return json.load(fh)


def small_doc():
    """Two parents that must be reconciled and three independent children."""
    return contract(
        [
            table("P", "parent",
                  [("id", "int", False), ("total", "int", True)], ["id"]),
            table("C", "child",
                  [("cid", "int", False), ("pref", "int", True),
                   ("amt", "int", True)]),
        ],
        [
            slot("p1", "P", [("a", {"id": 1, "total": 5}),
                             ("b", {"id": 2, "total": 7})]),
            slot("p2", "P", [("a", {"id": 3, "total": 0}),
                             ("b", {"id": 1, "total": 5})]),
            slot("c1", "C", [("a", {"cid": 1, "pref": 1, "amt": 5}),
                             ("b", {"cid": 2, "pref": None, "amt": 0})]),
            slot("c2", "C", [("a", {"cid": 3, "pref": None, "amt": 4}),
                             ("b", {"cid": 4, "pref": 1, "amt": 0})]),
        ],
        [
            {"id": "U", "kind": "unique", "table": "P", "key": ["id"]},
            {"id": "F", "kind": "fk", "child": "C", "child_fields": ["pref"],
             "parent": "P", "parent_key": ["id"]},
            {"id": "A", "kind": "aggregate", "child": "C",
             "child_fields": ["pref"], "child_amount": "amt",
             "parent": "P", "parent_key": ["id"],
             "parent_amount": "total"},
        ],
    )


def selection_of(entry) -> dict:
    return {sid: choice["candidate_id"] for sid, choice in entry.selection.items()}


class NormalFixtureTests(unittest.TestCase):
    def test_normal_fixture_all_true(self):
        doc = small_doc()
        report = generate(parse_contract(doc))
        self.assertIsInstance(report.normal, Fixture)
        payload = report.to_json()
        self.assertTrue(verify_report(parse_contract(doc), payload)["ok"])
        self.assertTrue(all(report.normal.rule_results.values()))

    def test_selection_matches_independent_brute_force(self):
        for doc in (small_doc(), billing_doc()):
            with self.subTest(contract=doc["contract_id"]):
                rule_ids = [r["id"] for r in doc["rules"]]
                expected = naive_first(doc, must_true=rule_ids)
                report = generate(parse_contract(doc))
                self.assertIsInstance(report.normal, Fixture)
                self.assertEqual(selection_of(report.normal), expected)

    def test_input_order_does_not_matter(self):
        doc = small_doc()
        shuffled = copy.deepcopy(doc)
        shuffled["slots"].reverse()
        for s in shuffled["slots"]:
            s["candidates"].reverse()
        shuffled["rules"].reverse()
        a = generate(parse_contract(doc))
        b = generate(parse_contract(shuffled))
        self.assertEqual(selection_of(a.normal), selection_of(b.normal))

    def test_lexicographic_minimum_when_several_solutions(self):
        doc = small_doc()
        report = generate(parse_contract(doc))
        sel = selection_of(report.normal)
        # independent brute force over the same identity order
        self.assertEqual(sel, naive_first(doc, [r["id"] for r in doc["rules"]]))


class IsolationTests(unittest.TestCase):
    def test_isolation_falsifies_exactly_one_rule(self):
        doc = small_doc()
        c = parse_contract(doc)
        report = generate(c, isolate=["A"])
        entry = report.isolations["A"]
        self.assertIsInstance(entry, Fixture)
        self.assertFalse(entry.rule_results["A"])
        self.assertTrue(all(v for k, v in entry.rule_results.items() if k != "A"))
        self.assertTrue(verify_report(c, report.to_json())["ok"])
        expected = naive_first(doc, ["U", "F"], ["A"])
        self.assertEqual(selection_of(entry), expected)

    def test_isolation_does_not_disturb_fk_or_amount_when_breaking_uniqueness(self):
        # Breaking U alone: both FK and aggregate remain true.
        doc = billing_doc()
        c = parse_contract(doc)
        report = generate(c, isolate=["U-invoice-id"])
        entry = report.isolations["U-invoice-id"]
        self.assertIsInstance(entry, NoFixture)
        # U-invoice-id is implied by A-client-billed + F-invoice-client here.
        self.assertEqual(entry.status, "proved")
        self.assertFalse(entry.to_json()["material"]["blocks"] == [])

        # Build a contract where uniqueness CAN be broken without touching the
        # other rules: duplicate invoice ids with distinct amounts, both
        # referenced to one client whose total matches.
        breakable = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", True), ("pref", "int", True),
                       ("amt", "int", True)]),
            ],
            [
                slot("p", "P", [("a", {"id": 1, "total": 0})]),
                slot("c1", "C", [("a", {"cid": 1, "pref": 1, "amt": 0})]),
                slot("c2", "C", [("a", {"cid": 1, "pref": 1, "amt": 0})]),
            ],
            [
                {"id": "U", "kind": "unique", "table": "C", "key": ["cid"]},
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        c2 = parse_contract(breakable)
        report2 = generate(c2, isolate=["U"])
        entry2 = report2.isolations["U"]
        self.assertIsInstance(entry2, Fixture)
        self.assertEqual(
            [k for k, v in entry2.rule_results.items() if not v], ["U"])
        self.assertTrue(verify_report(c2, report2.to_json())["ok"])

    def test_rule_implied_by_others_cannot_be_isolated(self):
        # With one parent (total 0), one child (amount 0) referencing it, the
        # aggregate A is entailed by F: every assignment with F true has A
        # true, so no isolation may be fabricated.
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", False), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", False), ("pref", "int", True),
                       ("amt", "int", True)]),
            ],
            [
                slot("p", "P", [("a", {"id": 1, "total": 0})]),
                slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 0})]),
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        self.assertIsNone(naive_first(doc, ["F"], ["A"]))
        c = parse_contract(doc)
        report = generate(c, isolate=["A"])
        entry = report.isolations["A"]
        self.assertIsInstance(entry, NoFixture)
        self.assertEqual(entry.status, "proved")
        self.assertIn("implied", entry.reason)
        material = entry.to_json()["material"]
        self.assertTrue(material["blocks"])
        self.assertTrue(replay_material(material)["ok"])
        claim = material["blocks"][0]["claim"]
        self.assertIn("A", claim["must_false"])
        self.assertIn("F", claim["must_true"])

    def test_invoice_id_uniqueness_implied_by_billing_rules(self):
        # In the billing example no assignment falsifies U-client-id alone;
        # the reason is the interaction with the aggregate totals.
        doc = billing_doc()
        c = parse_contract(doc)
        report = generate(c, isolate=["U-client-id"])
        entry = report.isolations["U-client-id"]
        self.assertIsInstance(entry, NoFixture)
        self.assertEqual(entry.status, "proved")
        self.assertTrue(replay_material(entry.to_json()["material"])["ok"])
        self.assertIsNone(naive_first(doc, [r["id"] for r in doc["rules"]
                                            if r["id"] != "U-client-id"],
                                      ["U-client-id"]))

    def test_isolation_result_matches_brute_force(self):
        for doc in (small_doc(), billing_doc()):
            c = parse_contract(doc)
            for target in [r["id"] for r in doc["rules"]]:
                with self.subTest(contract=doc["contract_id"], target=target):
                    must_true = [r["id"] for r in doc["rules"] if r["id"] != target]
                    expected = naive_first(doc, must_true, [target])
                    entry = generate(c, isolate=[target]).isolations[target]
                    if expected is None:
                        self.assertIsInstance(entry, NoFixture)
                        self.assertEqual(entry.status, "proved")
                    else:
                        self.assertIsInstance(entry, Fixture)
                        self.assertEqual(selection_of(entry), expected)


class NoFixtureTests(unittest.TestCase):
    def test_unsatisfiable_normal_reports_material(self):
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", True), ("pref", "int", True),
                       ("amt", "int", True)]),
            ],
            [
                slot("p", "P", [("a", {"id": 1, "total": 99})]),
                slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 1})]),
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        c = parse_contract(doc)
        report = generate(c)
        self.assertIsInstance(report.normal, NoFixture)
        self.assertEqual(report.normal.status, "proved")
        material = report.normal.to_json()["material"]
        self.assertTrue(replay_material(material)["ok"])
        self.assertIsNone(naive_first(doc, ["F", "A"]))

    def test_unsatisfiable_other_component_blocks_isolation(self):
        # A separate, unsatisfiable component means no isolation is possible
        # for a rule in the healthy component.
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", True), ("pref", "int", True),
                       ("amt", "int", True)]),
                table("Q", "parent", [("qid", "int", False)], ["qid"]),
            ],
            [
                slot("p", "P", [("a", {"id": 1, "total": 2})]),
                slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 2}),
                                ("b", {"cid": 1, "pref": 1, "amt": 3})]),
                slot("q1", "Q", [("a", {"qid": 1})]),
                slot("q2", "Q", [("a", {"qid": 1})]),
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
                {"id": "UQ", "kind": "unique", "table": "Q", "key": ["qid"]},
                # second unique rule on the same single-candidate Q slot:
                # both Q slots must collide, so the pair is unsatisfiable,
                # while A remains falsifiable inside its own component.
                {"id": "UQ2", "kind": "unique", "table": "Q",
                 "key": ["qid"]},
            ],
        )
        c = parse_contract(doc)
        report = generate(c, isolate=["A"])
        entry = report.isolations["A"]
        self.assertIsInstance(entry, NoFixture)
        self.assertEqual(entry.status, "proved")
        self.assertIn("unrelated component", entry.reason)
        material = entry.to_json()["material"]
        self.assertTrue(replay_material(material)["ok"])
        # the A component is satisfiable; the UQ component is not
        self.assertIsNone(naive_first(doc, ["UQ", "UQ2", "F", "A"]))
        self.assertIsNotNone(naive_first(doc, ["F"], ["A"]))


class ScalingTests(unittest.TestCase):
    def test_unfalsifiable_rule_alone_reports_material(self):
        # The only parent slot can never form a key, so uniqueness is always
        # true: no isolation exists, and that must be reported as proved.
        doc = contract(
            [table("P", "parent",
                   [("id", "int", True), ("total", "int", True)], ["id"])],
            [slot("p", "P", [("a", {"total": 1}), ("b", {"total": 2})])],
            [{"id": "U", "kind": "unique", "table": "P", "key": ["id"]}],
        )
        c = parse_contract(doc)
        report = generate(c, isolate=["U"])
        entry = report.isolations["U"]
        self.assertIsInstance(entry, NoFixture)
        self.assertEqual(entry.status, "proved")
        self.assertIn("cannot be falsified at all", entry.reason)
        material = entry.to_json()["material"]
        self.assertTrue(replay_material(material)["ok"])
        self.assertTrue(all(report.normal.rule_results.values()))

    def test_two_disjoint_reference_chains_share_a_table(self):
        # Two FK/aggregate pairs on the same tables but different reference
        # fields; slots are still linked through the shared tables' rules.
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", False), ("pref_a", "int", True),
                       ("pref_b", "int", True), ("amt", "int", True)]),
            ],
            [
                slot("pa", "P", [("a", {"id": 1, "total": 2})]),
                slot("pb", "P", [("a", {"id": 2, "total": 0})]),
                slot("ca", "C", [("a", {"cid": 1, "pref_a": 1,
                                        "pref_b": None, "amt": 2})]),
                slot("cb", "C", [("a", {"cid": 2, "pref_a": None,
                                        "pref_b": 2, "amt": 0})]),
            ],
            [
                {"id": "FA", "kind": "fk", "child": "C",
                 "child_fields": ["pref_a"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "AA", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref_a"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
                {"id": "FB", "kind": "fk", "child": "C",
                 "child_fields": ["pref_b"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "AB", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref_b"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        c = parse_contract(doc)
        report = generate(c, isolate=["AA", "AB", "FA", "FB"])
        self.assertIsNone(naive_first(doc, [r["id"] for r in doc["rules"]]))
        self.assertIsInstance(report.normal, NoFixture)
        # AB can be isolated: making the pref_b sum wrong leaves the pref_a
        # rules untouched.
        self.assertIsInstance(report.isolations["AB"], Fixture)
        self.assertEqual(
            [k for k, v in report.isolations["AB"].rule_results.items() if not v],
            ["AB"])
        # normal is a NoFixture, so verification covers the AB fixture only
        self.assertTrue(verify_report(c, report.to_json())["ok"])

    def test_many_unrelated_slots_do_not_multiply(self):
        # 200 independent free slots with 5 candidates each: product would be
        # 5**200.  The generator must solve this instantly.
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", True), ("pref", "int", True),
                       ("amt", "int", True)]),
                table("Z", "parent", [("zid", "int", True)], ["zid"]),
            ],
            [
                slot("p", "P", [("a", {"id": 1, "total": 0})]),
                slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 0}),
                                ("b", {"cid": 1, "pref": 1, "amt": 1})]),
            ] + [
                slot(f"z{i:03d}", "Z",
                     [(chr(ord("a") + k), {"zid": i * 10 + k}) for k in range(5)])
                for i in range(200)
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        c = parse_contract(doc)
        report = generate(c, isolate=["A"])
        self.assertIsInstance(report.normal, Fixture)
        self.assertIsInstance(report.isolations["A"], Fixture)
        self.assertLess(report.stats["max_component_product"], 10)
        self.assertLess(report.stats["search_nodes"], 5000)
        # free slots independently take their lexicographically smallest id
        for i in range(200):
            self.assertEqual(
                report.normal.selection[f"z{i:03d}"]["candidate_id"], "a")

    def test_analysis_does_not_materialise_cross_products(self):
        doc = billing_doc()
        c = parse_contract(doc)
        analysis = build_analysis(c, [r.id for r in c.rules], [])
        but_for_free = 1
        for comp in analysis.components:
            but_for_free *= comp.product_size
        self.assertLess(but_for_free, 10_000)
        # only 2 of the 7 slots are free
        self.assertEqual(len(analysis.free_slots), 2)

    def test_chain_of_related_slots_stays_component_scoped(self):
        # Two independent chains; component products are 2*2, not 16, and the
        # generator's node count stays small.
        doc = contract(
            [
                table("P", "parent",
                      [("id", "int", True), ("total", "int", True)], ["id"]),
                table("C", "child",
                      [("cid", "int", True), ("pref", "int", True),
                       ("amt", "int", True)]),
            ],
            [
                slot("p1", "P", [("a", {"id": 1, "total": 0}),
                                 ("b", {"id": 2, "total": 0})]),
                slot("c1", "C", [("a", {"cid": 1, "pref": 1, "amt": 0}),
                                 ("b", {"cid": 2, "pref": None, "amt": 0})]),
                slot("p2", "P", [("a", {"id": 1, "total": 0})]),
                slot("c2", "C", [("a", {"cid": 3, "pref": None, "amt": 0})]),
            ],
            [
                {"id": "F", "kind": "fk", "child": "C",
                 "child_fields": ["pref"], "parent": "P",
                 "parent_key": ["id"]},
                {"id": "A", "kind": "aggregate", "child": "C",
                 "child_fields": ["pref"], "child_amount": "amt",
                 "parent": "P", "parent_key": ["id"],
                 "parent_amount": "total"},
            ],
        )
        c = parse_contract(doc)
        report = generate(c)
        # The same rule links all four slots here, so they form one component;
        # the important property is that no global product is built.
        self.assertLess(report.stats["max_component_product"], 100)
        self.assertTrue(verify_report(c, report.to_json())["ok"])


if __name__ == "__main__":
    unittest.main()
