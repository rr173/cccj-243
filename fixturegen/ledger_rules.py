"""Incremental rule truth for the transaction ledger.

The ledger may not re-evaluate every rule over every selected row after each
transaction: that would scan slots the transaction never touched.  This module
keeps, per rule, only the aggregates needed to decide that rule's truth, and
updates them from a slot's old and new candidate.

The semantics are exactly those of :mod:`fixturegen.engine`; the tests compare
every incremental truth value against ``evaluate_rules`` on the complete
selection.  Nothing here trusts a fixture's self-reported rule table.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping, Optional, Set, Tuple

from .contract import Candidate, Contract, Rule
from .engine import row_amount, row_key, row_reference

Key = Tuple[object, ...]


class RuleState:
    """Maintains one rule's truth under single-slot changes."""

    def __init__(self, contract: Contract, rule: Rule,
                 selection: Mapping[str, Candidate]) -> None:
        self.contract = contract
        self.rule = rule

    def update(self, slot_id: str, old: Optional[Candidate],
               new: Optional[Candidate]) -> None:
        raise NotImplementedError

    def truth(self) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# uniqueness


class UniqueState(RuleState):
    def __init__(self, contract: Contract, rule: Rule,
                 selection: Mapping[str, Candidate]) -> None:
        super().__init__(contract, rule, selection)
        self.key_fields = rule.params["key"]
        self.slot_ids = {s.id for s in contract.slots_for_table(
            rule.params["table"])}
        self.keys: Dict[str, Key] = {}
        self.counts: Dict[Key, int] = {}
        self.duplicate_keys = 0
        for slot_id in self.slot_ids:
            candidate = selection.get(slot_id)
            if candidate is not None:
                self._add_key(slot_id, row_key(candidate, self.key_fields))

    def _add_key(self, slot_id: str, key: Optional[Key]) -> None:
        if key is None:
            return
        self.keys[slot_id] = key
        count = self.counts.get(key, 0) + 1
        self.counts[key] = count
        if count == 2:
            self.duplicate_keys += 1

    def _remove_key(self, slot_id: str) -> None:
        key = self.keys.pop(slot_id, None)
        if key is None:
            return
        count = self.counts[key] - 1
        if count:
            self.counts[key] = count
            if count == 1:
                self.duplicate_keys -= 1
        else:
            del self.counts[key]

    def update(self, slot_id: str, old: Optional[Candidate],
               new: Optional[Candidate]) -> None:
        if slot_id not in self.slot_ids:
            return
        self._remove_key(slot_id)
        if new is not None:
            self._add_key(slot_id, row_key(new, self.key_fields))

    def truth(self) -> bool:
        return self.duplicate_keys == 0


# ---------------------------------------------------------------------------
# foreign key


class FkState(RuleState):
    def __init__(self, contract: Contract, rule: Rule,
                 selection: Mapping[str, Candidate]) -> None:
        super().__init__(contract, rule, selection)
        self.child_fields = rule.params["child_fields"]
        self.parent_key = rule.params["parent_key"]
        parent_table = rule.params["parent"]
        child_table = rule.params["child"]
        self.parent_slots = {s.id for s in contract.slots_for_table(parent_table)}
        self.child_slots = {s.id for s in contract.slots_for_table(child_table)}

        self.parent_counts: Dict[Key, int] = {}
        self.child_ref: Dict[str, Tuple[str, Key]] = {}
        self.value_counts: Dict[Key, int] = {}
        self.bad_children = 0
        self.unmatched = 0

        for slot_id in self.parent_slots:
            candidate = selection.get(slot_id)
            if candidate is not None:
                key = row_key(candidate, self.parent_key)
                if key is not None:
                    self._add_parent_key(key)
        for slot_id in self.child_slots:
            candidate = selection.get(slot_id)
            self._add_child(slot_id, candidate)

    # -- parent side ------------------------------------------------------
    def _add_parent_key(self, key: Key) -> None:
        if key in self.parent_counts:
            self.parent_counts[key] += 1
            return
        self.parent_counts[key] = 1
        count = self.value_counts.get(key, 0)
        if count:
            self.unmatched -= count

    def _remove_parent_key(self, key: Key) -> None:
        count = self.parent_counts[key] - 1
        if count:
            self.parent_counts[key] = count
            return
        del self.parent_counts[key]
        count = self.value_counts.get(key, 0)
        if count:
            self.unmatched += count

    # -- child side -------------------------------------------------------
    def _add_child(self, slot_id: str,
                   candidate: Optional[Candidate]) -> None:
        if candidate is None:
            return
        status, values = row_reference(candidate, self.child_fields)
        self.child_ref[slot_id] = (status, values)
        if status == "value":
            self.value_counts[values] = self.value_counts.get(values, 0) + 1
            if values not in self.parent_counts:
                self.unmatched += 1
        elif status in ("missing", "partial"):
            self.bad_children += 1

    def _remove_child(self, slot_id: str) -> None:
        entry = self.child_ref.pop(slot_id, None)
        if entry is None:
            return
        status, values = entry
        if status == "value":
            count = self.value_counts[values] - 1
            if count:
                self.value_counts[values] = count
            else:
                del self.value_counts[values]
            if values not in self.parent_counts:
                self.unmatched -= 1
        elif status in ("missing", "partial"):
            self.bad_children -= 1

    def update(self, slot_id: str, old: Optional[Candidate],
               new: Optional[Candidate]) -> None:
        if slot_id in self.parent_slots:
            if old is not None:
                key = row_key(old, self.parent_key)
                if key is not None:
                    self._remove_parent_key(key)
            if new is not None:
                key = row_key(new, self.parent_key)
                if key is not None:
                    self._add_parent_key(key)
        if slot_id in self.child_slots:
            self._remove_child(slot_id)
            self._add_child(slot_id, new)

    def truth(self) -> bool:
        return self.bad_children == 0 and self.unmatched == 0


# ---------------------------------------------------------------------------
# aggregate


class AggregateState(RuleState):
    """Sums per parent key plus the mismatch bookkeeping of the engine.

    ``sums`` has an entry exactly for keys that at least one selected parent
    currently carries.  Per child reference value we keep the sum of valid
    amounts together with an explicit *count* of the children that produced
    it: the sum alone cannot tell "no child" from "children whose amounts
    cancel to zero".  ``child_value_bad`` counts matching children with an
    invalid amount.  These are maintained regardless of whether a matching
    parent exists, so activating a key (or adding a child to an active key)
    is O(1) apart from rechecking the parent rows that carry that key.
    """

    def __init__(self, contract: Contract, rule: Rule,
                 selection: Mapping[str, Candidate]) -> None:
        super().__init__(contract, rule, selection)
        self.child_fields = rule.params["child_fields"]
        self.child_amount = rule.params["child_amount"]
        self.parent_key = rule.params["parent_key"]
        self.parent_amount = rule.params["parent_amount"]
        parent_table = rule.params["parent"]
        child_table = rule.params["child"]
        self.parent_slots = {s.id for s in contract.slots_for_table(parent_table)}
        self.child_slots = {s.id for s in contract.slots_for_table(child_table)}

        self.parent_info: Dict[str, Tuple[Optional[Key], Optional[int]]] = {}
        self.parent_key_counts: Dict[Key, int] = {}
        self.parent_key_slots: Dict[Key, Set[str]] = {}
        self.parents_bad = 0
        self.mismatched: Set[str] = set()

        self.child_info: Dict[str, Tuple[str, Key, Optional[int]]] = {}
        self.child_value_sum: Dict[Key, int] = {}
        self.child_value_count: Dict[Key, int] = {}
        self.child_value_bad: Dict[Key, int] = {}
        self.sums: Dict[Key, int] = {}
        self.matched_bad = 0

        for slot_id in self.parent_slots:
            candidate = selection.get(slot_id)
            if candidate is not None:
                self._store_parent(slot_id, candidate)
        for slot_id in self.child_slots:
            candidate = selection.get(slot_id)
            if candidate is not None:
                self._store_child(slot_id, candidate)
        for key, count in self.parent_key_counts.items():
            self.sums[key] = self.child_value_sum.get(key, 0)
        for key in self.sums:
            self.matched_bad += self.child_value_bad.get(key, 0)
        for slot_id, (key, amount) in self.parent_info.items():
            self._check_parent(slot_id, key, amount)

    # -- construction helpers --------------------------------------------
    def _store_parent(self, slot_id: str, candidate: Candidate) -> None:
        key = row_key(candidate, self.parent_key)
        amount = row_amount(candidate, self.parent_amount)
        self.parent_info[slot_id] = (key, amount)
        if amount is None:
            self.parents_bad += 1
        if key is not None:
            self.parent_key_counts[key] = self.parent_key_counts.get(key, 0) + 1
            self.parent_key_slots.setdefault(key, set()).add(slot_id)

    def _store_child(self, slot_id: str, candidate: Candidate) -> None:
        status, values = row_reference(candidate, self.child_fields)
        amount = row_amount(candidate, self.child_amount) if status == "value" else None
        self.child_info[slot_id] = (status, values, amount)
        if status != "value":
            return
        if amount is None:
            self.child_value_bad[values] = self.child_value_bad.get(values, 0) + 1
        else:
            self.child_value_sum[values] = (
                self.child_value_sum.get(values, 0) + amount)
            self.child_value_count[values] = (
                self.child_value_count.get(values, 0) + 1)

    def _check_parent(self, slot_id: str, key: Optional[Key],
                      amount: Optional[int]) -> None:
        self.mismatched.discard(slot_id)
        if amount is None:
            return
        expected = 0 if key is None else self.sums.get(key, 0)
        if amount != expected:
            self.mismatched.add(slot_id)

    def _check_key(self, key: Key) -> None:
        for slot_id in self.parent_key_slots.get(key, ()):
            info = self.parent_info[slot_id]
            self._check_parent(slot_id, info[0], info[1])

    # -- parent side ------------------------------------------------------
    def _remove_parent(self, slot_id: str) -> None:
        info = self.parent_info.pop(slot_id, None)
        if info is None:
            return
        key, amount = info
        self.mismatched.discard(slot_id)
        if amount is None:
            self.parents_bad -= 1
        if key is None:
            return
        slots = self.parent_key_slots[key]
        slots.discard(slot_id)
        if slots:
            self.parent_key_counts[key] -= 1
            return
        del self.parent_key_slots[key]
        del self.parent_key_counts[key]
        self.matched_bad -= self.child_value_bad.get(key, 0)
        self.sums.pop(key, None)

    def _add_parent(self, slot_id: str, candidate: Candidate) -> None:
        key = row_key(candidate, self.parent_key)
        amount = row_amount(candidate, self.parent_amount)
        self.parent_info[slot_id] = (key, amount)
        if amount is None:
            self.parents_bad += 1
        if key is not None:
            if key in self.parent_key_counts:
                self.parent_key_counts[key] += 1
                self.parent_key_slots[key].add(slot_id)
            else:
                self.parent_key_counts[key] = 1
                self.parent_key_slots[key] = {slot_id}
                self.sums[key] = self.child_value_sum.get(key, 0)
                self.matched_bad += self.child_value_bad.get(key, 0)
        self._check_parent(slot_id, key, amount)

    # -- child side -------------------------------------------------------
    def _remove_child(self, slot_id: str) -> None:
        info = self.child_info.pop(slot_id, None)
        if info is None:
            return
        status, values, amount = info
        if status != "value":
            return
        if amount is None:
            count = self.child_value_bad[values] - 1
            if count:
                self.child_value_bad[values] = count
            else:
                del self.child_value_bad[values]
            if values in self.sums:
                self.matched_bad -= 1
            return
        self.child_value_sum[values] -= amount
        remaining = self.child_value_count[values] - 1
        if remaining:
            self.child_value_count[values] = remaining
        else:
            del self.child_value_count[values]
            del self.child_value_sum[values]
        if values in self.sums:
            self.sums[values] -= amount
            self._check_key(values)

    def _add_child(self, slot_id: str, candidate: Candidate) -> None:
        status, values = row_reference(candidate, self.child_fields)
        amount = row_amount(candidate, self.child_amount) if status == "value" else None
        self.child_info[slot_id] = (status, values, amount)
        if status != "value":
            return
        if amount is None:
            self.child_value_bad[values] = self.child_value_bad.get(values, 0) + 1
            if values in self.sums:
                self.matched_bad += 1
            return
        self.child_value_sum[values] = self.child_value_sum.get(values, 0) + amount
        self.child_value_count[values] = self.child_value_count.get(values, 0) + 1
        if values in self.sums:
            self.sums[values] += amount
            self._check_key(values)

    # -- external ---------------------------------------------------------
    def update(self, slot_id: str, old: Optional[Candidate],
               new: Optional[Candidate]) -> None:
        if slot_id in self.parent_slots:
            self._remove_parent(slot_id)
            if new is not None:
                self._add_parent(slot_id, new)
        if slot_id in self.child_slots:
            self._remove_child(slot_id)
            if new is not None:
                self._add_child(slot_id, new)

    def truth(self) -> bool:
        return (self.parents_bad == 0 and self.matched_bad == 0
                and not self.mismatched)


_STATES = {
    "unique": UniqueState,
    "fk": FkState,
    "aggregate": AggregateState,
}


class RuleTracker:
    """All rule states plus O(1) truth access and before/after differences.

    Rule states are grouped by the table they mention, so a transaction only
    dispatches each changed slot to the rules on that slot's table.  Rules on
    other tables are untouched and their truth is carried over unchanged.
    """

    def __init__(self, contract: Contract,
                 selection: Mapping[str, Candidate]) -> None:
        self.contract = contract
        self.states = {
            rule.id: _STATES[rule.kind](contract, rule, selection)
            for rule in contract.rules
        }
        self.current = {rid: state.truth() for rid, state in self.states.items()}
        grouped: Dict[str, list] = {}
        for rule in contract.rules:
            state = self.states[rule.id]
            for table in _rule_tables(rule):
                grouped.setdefault(table, []).append(state)
        self._by_table: Dict[str, Tuple[RuleState, ...]] = {
            table: tuple(states) for table, states in grouped.items()}
        self._slot_table = {slot.id: slot.table for slot in contract.slots}

    def apply(self, changes: Iterable[Tuple[str, Optional[Candidate],
                                            Optional[Candidate]]]) -> Tuple[list, list]:
        """Apply all changes, then return (newly_false, recovered), sorted."""
        old_truth = dict(self.current)
        changed_rules: Set[str] = set()
        for slot_id, old, new in changes:
            table = self._slot_table[slot_id]
            for state in self._by_table.get(table, ()):
                state.update(slot_id, old, new)
                changed_rules.add(state.rule.id)
        for rule_id in changed_rules:
            self.current[rule_id] = self.states[rule_id].truth()
        newly_false = sorted(rid for rid, value in self.current.items()
                             if old_truth.get(rid) is True and value is False)
        recovered = sorted(rid for rid, value in self.current.items()
                           if old_truth.get(rid) is False and value is True)
        return newly_false, recovered

    def truths(self) -> Dict[str, bool]:
        return dict(self.current)


def _rule_tables(rule: Rule) -> Tuple[str, ...]:
    if rule.kind == "unique":
        return (rule.params["table"],)
    return (rule.params["child"], rule.params["parent"])
