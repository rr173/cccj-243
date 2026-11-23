"""Contract parsing and semantics tests (no generator involved)."""

from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import ContractError, parse_contract  # noqa: E402
from tests.helpers import contract, slot, table  # noqa: E402

T = [
    table("P", "parent",
          [("id", "int", False), ("region", "str", True), ("total", "int", True)],
          ["id"]),
    table("C", "child", [("cid", "int", False), ("pref", "int", True),
                         ("amt", "int", True)]),
]


def base_rules():
    return [
        {"id": "U", "kind": "unique", "table": "P", "key": ["id"]},
        {"id": "F", "kind": "fk", "child": "C", "child_fields": ["pref"],
         "parent": "P", "parent_key": ["id"]},
        {"id": "A", "kind": "aggregate", "child": "C", "child_fields": ["pref"],
         "child_amount": "amt", "parent": "P", "parent_key": ["id"],
         "parent_amount": "total"},
    ]


def base_slots():
    return [
        slot("p", "P", [("a", {"id": 1, "total": 5})]),
        slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 5})]),
    ]


class ParseTests(unittest.TestCase):
    def test_minimal_contract_parses(self):
        c = parse_contract(contract(T, base_slots(), base_rules()))
        self.assertEqual(c.contract_id, "t")
        self.assertEqual(len(c.rules), 3)

    def test_duplicate_rule_id_rejected(self):
        rules = base_rules()
        rules[1]["id"] = "U"
        with self.assertRaisesRegex(ContractError, "duplicate rule id"):
            parse_contract(contract(T, base_slots(), rules))

    def test_undefined_reference_rule_rejected(self):
        rules = base_rules()
        rules[2]["reference_rule"] = "nope"
        with self.assertRaisesRegex(ContractError, "undefined rule"):
            parse_contract(contract(T, base_slots(), rules))

    def test_reference_rule_of_wrong_kind_rejected(self):
        rules = base_rules()
        rules[2]["reference_rule"] = "U"
        with self.assertRaisesRegex(ContractError, "not an fk rule"):
            parse_contract(contract(T, base_slots(), rules))

    def test_mismatched_reference_rule_rejected(self):
        rules = base_rules()
        rules[1]["child_fields"] = ["pref"]
        rules[1]["parent_key"] = ["id"]
        rules[2]["reference_rule"] = "F"
        rules[2]["child_fields"] = ["pref"]
        # make the FK reference a different parent table key
        rules[2]["parent_key"] = ["region"]
        with self.assertRaisesRegex(ContractError, "does not describe"):
            parse_contract(contract(T, base_slots(), rules))

    def test_undefined_candidate_field_rejected(self):
        slots = base_slots()
        slots[0]["candidates"][0]["row"]["ghost"] = 1
        with self.assertRaisesRegex(ContractError, "undefined field"):
            parse_contract(contract(T, slots, base_rules()))

    def test_unknown_table_rejected(self):
        slots = base_slots()
        slots[0]["table"] = "NOPE"
        with self.assertRaisesRegex(ContractError, "undefined table"):
            parse_contract(contract(T, slots, base_rules()))

    def test_unknown_rule_identity_in_slot_table(self):
        rules = base_rules()
        rules[0]["table"] = "NOPE"
        with self.assertRaisesRegex(ContractError, "undefined table"):
            parse_contract(contract(T, base_slots(), rules))

    def test_non_nullable_null_rejected(self):
        slots = base_slots()
        slots[1]["candidates"][0]["row"]["cid"] = None
        with self.assertRaisesRegex(ContractError, "not nullable"):
            parse_contract(contract(T, slots, base_rules()))

    def test_wrong_scalar_type_rejected(self):
        slots = base_slots()
        slots[1]["candidates"][0]["row"]["cid"] = "1"
        with self.assertRaisesRegex(ContractError, "wrong type"):
            parse_contract(contract(T, slots, base_rules()))

    def test_bool_is_not_int(self):
        slots = base_slots()
        slots[1]["candidates"][0]["row"]["amt"] = True
        with self.assertRaisesRegex(ContractError, "wrong type"):
            parse_contract(contract(T, slots, base_rules()))

    def test_duplicate_candidate_id_rejected(self):
        slots = base_slots()
        slots[0]["candidates"].append(copy.deepcopy(slots[0]["candidates"][0]))
        with self.assertRaisesRegex(ContractError, "duplicate candidate_id"):
            parse_contract(contract(T, slots, base_rules()))

    def test_missing_schema_rejected(self):
        doc = contract(T, base_slots(), base_rules())
        doc["schema"] = "nope"
        with self.assertRaisesRegex(ContractError, "schema"):
            parse_contract(doc)

    def test_parent_key_rule_resolves(self):
        rules = base_rules()
        del rules[2]["parent_key"]
        rules[2]["parent_key_rule"] = "U"
        c = parse_contract(contract(T, base_slots(), rules))
        agg = c.rule("A")
        self.assertEqual(agg.params["parent_key"], ("id",))

    def test_parent_key_rule_must_match_parent_key(self):
        rules = base_rules()
        rules[2]["parent_key_rule"] = "U"
        rules[2]["parent_key"] = ["region"]
        with self.assertRaisesRegex(ContractError, "disagrees"):
            parse_contract(contract(T, base_slots(), rules))

    def test_aggregate_needs_a_key_source(self):
        rules = base_rules()
        del rules[2]["parent_key"]
        with self.assertRaisesRegex(ContractError, "parent_key or parent_key_rule"):
            parse_contract(contract(T, base_slots(), rules))

    def test_bad_contract_is_not_reported_unsatisfiable(self):
        # generator must never run on a malformed contract: parsing raises.
        doc = contract(T, base_slots(), base_rules())
        doc["rules"][0]["key"] = ["ghost"]
        with self.assertRaises(ContractError):
            parse_contract(doc)


class SemanticsTests(unittest.TestCase):
    """Direct rule evaluation on hand-built assignments."""

    def _eval(self, slots, rules, selection):
        from fixturegen.engine import evaluate_rules
        c = parse_contract(contract(T, slots, rules))
        by_slot = {s.id: {cand.candidate_id: cand for cand in s.candidates}
                   for s in c.slots}
        assignment = {sid: by_slot[sid][cid] for sid, cid in selection.items()}
        return evaluate_rules(c, assignment)

    def test_null_reference_matches_nothing(self):
        slots = [
            slot("p", "P", [("a", {"id": 1, "total": 0})]),
            slot("c", "C", [("a", {"cid": 1, "pref": None, "amt": 99})]),
        ]
        truth = self._eval(slots, base_rules(), {"p": "a", "c": "a"})
        self.assertTrue(truth["F"])
        self.assertTrue(truth["A"])

    def test_partial_reference_fails_fk_and_contributes_nothing(self):
        rules = [
            {"id": "F", "kind": "fk", "child": "C",
             "child_fields": ["pref", "cid"],
             "parent": "P", "parent_key": ["id", "region"]},
            {"id": "A", "kind": "aggregate", "child": "C",
             "child_fields": ["pref", "cid"], "child_amount": "amt",
             "parent": "P", "parent_key": ["id", "region"],
             "parent_amount": "total"},
        ]
        slots = [
            slot("p", "P", [("a", {"id": 1, "region": None, "total": 0})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 50})]),
        ]
        # reference is (1, 1) but parent key is (1, None): FK fails.
        truth = self._eval(slots, rules, {"p": "a", "c": "a"})
        self.assertFalse(truth["F"])
        self.assertTrue(truth["A"])

    def test_fk_missing_field_fails(self):
        slots = [
            slot("p", "P", [("a", {"id": 1, "total": 0})]),
            slot("c", "C", [("a", {"cid": 1})]),  # pref absent
        ]
        truth = self._eval(slots, base_rules(), {"p": "a", "c": "a"})
        self.assertFalse(truth["F"])
        self.assertTrue(truth["A"])  # contributes nowhere; parent total 0

    def test_duplicate_null_key_collides(self):
        nullable_p = table("P", "parent",
                           [("id", "int", True), ("total", "int", True)], ["id"])
        slots = [
            slot("p1", "P", [("a", {"id": None, "total": 0})]),
            slot("p2", "P", [("a", {"id": None, "total": 0})]),
        ]
        c = parse_contract(contract([nullable_p], slots, [
            {"id": "U", "kind": "unique", "table": "P", "key": ["id"]}]))
        from fixturegen.engine import evaluate_rule
        assignment = {s.id: s.candidates[0] for s in c.slots}
        self.assertFalse(evaluate_rule(c, c.rule("U"), assignment))

    def test_missing_key_field_is_not_a_key(self):
        nullable_p = table("P", "parent",
                           [("id", "int", True), ("total", "int", True)], ["id"])
        slots = [
            slot("p1", "P", [("a", {"id": None, "total": 0})]),
            slot("p2", "P", [("a", {"total": 0})]),  # id absent
        ]
        c = parse_contract(contract([nullable_p], slots, [
            {"id": "U", "kind": "unique", "table": "P", "key": ["id"]}]))
        from fixturegen.engine import evaluate_rule
        assignment = {s.id: s.candidates[0] for s in c.slots}
        self.assertTrue(evaluate_rule(c, c.rule("U"), assignment))

    def test_unmatched_child_amount_is_not_summed(self):
        # A dangling child with a null amount must not fail the aggregate.
        slots = [
            slot("p", "P", [("a", {"id": 1, "total": 3})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 3})]),
            slot("d", "C", [("a", {"cid": 2, "pref": 7, "amt": None})]),
        ]
        truth = self._eval(slots, base_rules(), {"p": "a", "c": "a", "d": "a"})
        self.assertTrue(truth["A"])
        self.assertFalse(truth["F"])  # dangling reference

    def test_matched_child_invalid_amount_fails_aggregate(self):
        slots = [
            slot("p", "P", [("a", {"id": 1, "total": 3})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": None})]),
        ]
        truth = self._eval(slots, base_rules(), {"p": "a", "c": "a"})
        self.assertTrue(truth["F"])
        self.assertFalse(truth["A"])

    def test_child_matching_two_parents_contributes_to_each(self):
        # Uniqueness relaxed: two parents share key 1; total must each see 3.
        rules = [
            {"id": "F", "kind": "fk", "child": "C", "child_fields": ["pref"],
             "parent": "P", "parent_key": ["id"]},
            {"id": "A", "kind": "aggregate", "child": "C",
             "child_fields": ["pref"], "child_amount": "amt",
             "parent": "P", "parent_key": ["id"], "parent_amount": "total"},
        ]
        slots = [
            slot("p1", "P", [("a", {"id": 1, "total": 3})]),
            slot("p2", "P", [("a", {"id": 1, "total": 3})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 1, "amt": 3})]),
        ]
        truth = self._eval(slots, rules, {"p1": "a", "p2": "a", "c": "a"})
        self.assertTrue(truth["A"])

    def test_rules_evaluated_independently(self):
        # Fails both F and A; both must still be reported, none skipped.
        slots = [
            slot("p", "P", [("a", {"id": 1, "total": 100})]),
            slot("c", "C", [("a", {"cid": 1, "pref": 7, "amt": 1})]),
        ]
        truth = self._eval(slots, base_rules(), {"p": "a", "c": "a"})
        self.assertEqual(set(truth), {"U", "F", "A"})
        self.assertFalse(truth["F"])
        self.assertFalse(truth["A"])


if __name__ == "__main__":
    unittest.main()
