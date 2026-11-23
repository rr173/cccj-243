"""Evaluation and solving for the narrow fixture contract.

Semantics implemented here (also documented in :mod:`fixturegen.contract`):

* UNIQUE.  Rows whose key fields are all present form keys.  JSON ``null`` is
  an ordinary key value, so duplicate null keys collide.  A row that omits a
  key field has no key and cannot collide.
* FK.  A reference is either wholly null (no reference) or wholly present and
  non-null (must equal a selected parent key).  Partial nulls and missing
  fields fail the rule.
* AGGREGATE.  Every selected child and parent amount must be a present JSON
  integer, otherwise the rule fails.  For each parent row the matching child
  amounts are summed (no match means 0) and compared with the parent amount.
  Wholly-null references match nothing; partial/missing references contribute
  nowhere (the FK rule is responsible for rejecting them).  A child whose
  reference matches several parent rows contributes once to each, which can
  happen only when a uniqueness rule has been relaxed.
* Rules are evaluated on the complete fixture; every rule gets a truth value
  independently of the others.

Search strategy
---------------
The generator never materialises the cross-slot product.  For a given truth
requirement it first computes, per rule, the *influence slots*: slots whose
chosen row can change that rule's truth.  Two slots are linked when they share
a new rule's influence set, and connected components of that slot graph are
solved independently.  Slots in no influence set are free and take their
lexicographically smallest admissible candidate on their own.  Therefore many
mutually unrelated slots cost linear, not multiplicative, work.

Within a component, candidates are filtered for must-true rules (a candidate
that unarily falsifies such a rule can never appear in a valid fixture), and
depth-first search visits slots in slot-id order and candidates in
candidate-id order.  The first leaf is thus the component's lexicographic
minimum; constraints decompose across components, so combining per-component
minima gives the global lexicographic minimum.  Input file order is irrelevant.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from .contract import Candidate, Contract, MISSING, Rule, Slot


class SearchLimitExceeded(RuntimeError):
    """A bounded search gave up; the conclusion is undetermined."""


class FixtureRequestError(ValueError):
    """The caller asked for something the contract cannot express."""


# ---------------------------------------------------------------------------
# primitives


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def row_key(candidate: Candidate, fields: Sequence[str]) -> Optional[Tuple[Any, ...]]:
    """Return the key tuple, or None when any key field is missing."""
    values: List[Any] = []
    for name in fields:
        value = candidate.get(name)
        if value is MISSING:
            return None
        values.append(value)
    return tuple(values)


def row_reference(candidate: Candidate,
                  fields: Sequence[str]) -> Tuple[str, Tuple[Any, ...]]:
    """Classify a child reference.

    Returns one of:
      ("missing", ())  at least one reference field absent
      ("partial", ())  present but a mix of null and non-null
      ("null", ())     present and wholly null
      ("value", tuple) present and wholly non-null
    """
    values: List[Any] = []
    for name in fields:
        value = candidate.get(name)
        if value is MISSING:
            return ("missing", ())
        values.append(value)
    non_null = [v is not None for v in values]
    if all(non_null):
        return ("value", tuple(values))
    if not any(non_null):
        return ("null", ())
    return ("partial", ())


def row_amount(candidate: Candidate, field_name: str) -> Optional[int]:
    value = candidate.get(field_name)
    if value is MISSING or value is None or not is_int(value):
        return None
    return value


# ---------------------------------------------------------------------------
# rule evaluation


def evaluate_unique(contract: Contract, rule: Rule,
                    assignment: Mapping[str, Candidate]) -> bool:
    key_fields = rule.params["key"]
    seen = set()
    for slot in contract.slots_for_table(rule.params["table"]):
        candidate = assignment.get(slot.id)
        if candidate is None:
            continue
        key = row_key(candidate, key_fields)
        if key is None:
            continue
        if key in seen:
            return False
        seen.add(key)
    return True


def _parent_keys(contract: Contract, rule: Rule,
                 assignment: Mapping[str, Candidate]) -> Set[Tuple[Any, ...]]:
    keys = set()
    for slot in contract.slots_for_table(rule.params["parent"]):
        candidate = assignment.get(slot.id)
        if candidate is None:
            continue
        key = row_key(candidate, rule.params["parent_key"])
        if key is not None:
            keys.add(key)
    return keys


def evaluate_fk(contract: Contract, rule: Rule,
                assignment: Mapping[str, Candidate]) -> bool:
    keys = _parent_keys(contract, rule, assignment)
    for slot in contract.slots_for_table(rule.params["child"]):
        candidate = assignment.get(slot.id)
        if candidate is None:
            continue
        status, values = row_reference(candidate, rule.params["child_fields"])
        if status in ("missing", "partial"):
            return False
        if status == "null":
            continue
        if values not in keys:
            return False
    return True


def evaluate_aggregate(contract: Contract, rule: Rule,
                       assignment: Mapping[str, Candidate]) -> bool:
    amount_field = rule.params["child_amount"]
    parent_amount_field = rule.params["parent_amount"]
    child_fields = rule.params["child_fields"]
    parent_key = rule.params["parent_key"]

    # Parents: each selected parent amount must be a present JSON integer;
    # the net of a parent is compared even when its own key is incomplete.
    parents: List[Tuple[Optional[Tuple[Any, ...]], int]] = []
    for slot in contract.slots_for_table(rule.params["parent"]):
        candidate = assignment.get(slot.id)
        if candidate is None:
            continue
        parent_amount = row_amount(candidate, parent_amount_field)
        if parent_amount is None:
            return False
        parents.append((row_key(candidate, parent_key), parent_amount))
    parent_keys = {key for key, _ in parents if key is not None}

    # Children contribute only when their (complete, non-null) reference
    # matches at least one selected parent; a row that matches nothing is
    # simply not summed, so an invalid amount there is not a summed amount.
    sums: Dict[Tuple[Any, ...], int] = {}
    for slot in contract.slots_for_table(rule.params["child"]):
        candidate = assignment.get(slot.id)
        if candidate is None:
            continue
        status, values = row_reference(candidate, child_fields)
        if status != "value" or values not in parent_keys:
            continue
        amount = row_amount(candidate, amount_field)
        if amount is None:
            return False
        sums[values] = sums.get(values, 0) + amount

    for key, parent_amount in parents:
        total = 0 if key is None else sums.get(key, 0)
        if total != parent_amount:
            return False
    return True


_EVALUATORS = {
    "unique": evaluate_unique,
    "fk": evaluate_fk,
    "aggregate": evaluate_aggregate,
}


def evaluate_rule(contract: Contract, rule: Rule,
                  assignment: Mapping[str, Candidate]) -> bool:
    return _EVALUATORS[rule.kind](contract, rule, assignment)


def evaluate_rules(contract: Contract,
                   assignment: Mapping[str, Candidate]) -> Dict[str, bool]:
    """Truth value of every rule on a complete assignment (no short-circuit)."""
    return {rule.id: evaluate_rule(contract, rule, assignment) for rule in contract.rules}


def rule_tables(rule: Rule) -> Tuple[str, ...]:
    if rule.kind == "unique":
        return (rule.params["table"],)
    return (rule.params["child"], rule.params["parent"])


# ---------------------------------------------------------------------------
# analysis: influence sets, candidate filtering, components


def _slots_by_table(contract: Contract) -> Dict[str, Tuple[Slot, ...]]:
    out: Dict[str, List[Slot]] = {}
    for slot in contract.slots:
        out.setdefault(slot.table, []).append(slot)
    return {name: tuple(slots) for name, slots in out.items()}


def rule_influence(rule: Rule, by_table: Mapping[str, Sequence[Slot]]) -> Tuple[str, ...]:
    """Slots whose selected row can change this rule's truth value.

    The computation is conservative: a slot is included whenever any of its
    candidates might participate in the rule.  Slots left out provably cannot
    affect the rule (e.g. a child whose every candidate has a wholly null
    reference for an FK rule, or a slot without a single complete key for a
    uniqueness rule).
    """
    inf: List[str] = []
    if rule.kind == "unique":
        key = rule.params["key"]
        for slot in by_table.get(rule.params["table"], ()):
            if any(row_key(c, key) is not None for c in slot.candidates):
                inf.append(slot.id)
    elif rule.kind == "fk":
        child_fields = rule.params["child_fields"]
        for slot in by_table.get(rule.params["child"], ()):
            if any(row_reference(c, child_fields)[0] != "null" for c in slot.candidates):
                inf.append(slot.id)
        for slot in by_table.get(rule.params["parent"], ()):
            if any(row_key(c, rule.params["parent_key"]) is not None
                   for c in slot.candidates):
                inf.append(slot.id)
    else:  # aggregate
        child_fields = rule.params["child_fields"]
        # A child can affect the sums only if it can carry a value reference
        # (it also can affect the truth through an invalid amount there).
        for slot in by_table.get(rule.params["child"], ()):
            if any(row_reference(c, child_fields)[0] == "value"
                   for c in slot.candidates):
                inf.append(slot.id)
        # Every parent slot can change the rule: its key selects which sum it
        # is compared against and its amount changes the comparison.
        for slot in by_table.get(rule.params["parent"], ()):
            inf.append(slot.id)
    return tuple(sorted(set(inf)))


def candidate_unarily_falsifies(rule: Rule, table: str, candidate: Candidate) -> bool:
    """True when one slot's row alone makes the rule false, whatever else holds.

    This is used only to shrink the search domain for rules required true.  It
    must be sound: returning True for a candidate that could still participate
    in a true rule would drop valid fixtures.
    """
    if rule.kind == "fk" and table == rule.params["child"]:
        return row_reference(candidate, rule.params["child_fields"])[0] in (
            "missing", "partial")
    if rule.kind == "aggregate":
        # Parent amounts are always compared, so an invalid one is fatal.
        if table == rule.params["parent"]:
            return row_amount(candidate, rule.params["parent_amount"]) is None
    return False


class _DSU:
    def __init__(self, items: Iterable[str] = ()) -> None:
        self.parent: Dict[str, str] = {item: item for item in items}

    def add(self, item: str) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: str) -> str:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a: str, b: str) -> None:
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass(frozen=True)
class Component:
    key: str
    tables: Tuple[str, ...]              # tables of the rules in this component
    slots: Tuple[Slot, ...]              # all contract slots on those tables
    search_slots: Tuple[Slot, ...]       # influence slots, sorted by id
    rules: Tuple[Rule, ...]              # all contract rules on those tables
    must_true: Tuple[str, ...]           # search requirement (subset of rules)
    must_false: Tuple[str, ...]

    @property
    def product_size(self) -> int:
        """Product over *search* slots only (never the global product)."""
        size = 1
        for slot in self.search_slots:
            size *= len(slot.candidates)
        return size


@dataclass
class Analysis:
    domains: Dict[str, Tuple[Candidate, ...]]
    components: Tuple[Component, ...]
    free_slots: Tuple[Slot, ...]
    rule_component: Dict[str, str]       # rule id -> component key
    unfalsifiable: Tuple[str, ...]       # must_false rules with no influence
    trivial_rules: Tuple[str, ...]       # must_true rules with no influence

    def component_for_rule(self, rule_id: str) -> Optional[Component]:
        key = self.rule_component.get(rule_id)
        if key is None:
            return None
        for comp in self.components:
            if comp.key == key:
                return comp
        raise AssertionError(f"internal error: dangling component key {key!r}")


def build_analysis(contract: Contract, must_true: Sequence[str],
                   must_false: Sequence[str]) -> Analysis:
    """Analyse one truth requirement.

    Each component embeds, for material purposes, every rule whose tables are
    a subset of the component's tables (all slots on those tables are kept as
    well), so no minimality is claimed and replay needs no generator logic.
    """
    by_table = _slots_by_table(contract)
    rules_by_id = {rule.id: rule for rule in contract.rules}

    # --- filter candidates that unarily falsify a must-true rule ---------
    domains: Dict[str, Tuple[Candidate, ...]] = {slot.id: slot.candidates
                                                for slot in contract.slots}
    for slot in contract.slots:
        kept = []
        for candidate in slot.candidates:
            bad = False
            for rid in must_true:
                if candidate_unarily_falsifies(rules_by_id[rid], slot.table, candidate):
                    bad = True
                    break
            if not bad:
                kept.append(candidate)
        domains[slot.id] = tuple(sorted(kept, key=lambda c: c.candidate_id))

    # --- influence sets --------------------------------------------------
    influence: Dict[str, Tuple[str, ...]] = {}
    for rid in list(must_true) + list(must_false):
        influence[rid] = rule_influence(rules_by_id[rid], by_table)

    # --- slot graph ------------------------------------------------------
    dsu = _DSU()
    for rid in list(must_true) + list(must_false):
        inf = influence[rid]
        for sid in inf:
            dsu.add(sid)
        for sid in inf[1:]:
            dsu.union(inf[0], sid)

    groups: Dict[str, List[str]] = {}
    for sid in dsu.parent:
        groups.setdefault(dsu.find(sid), []).append(sid)

    # map rule -> group root; rules with empty influence are trivial/implied
    root_of: Dict[str, str] = {}
    for rid in list(must_true) + list(must_false):
        inf = influence[rid]
        if inf:
            root_of[rid] = dsu.find(inf[0])

    # deterministic component keys: smallest search-slot id
    rule_component: Dict[str, str] = {}
    components: List[Component] = []
    for root, sids in groups.items():
        search_slot_ids = tuple(sorted(sids))
        key = f"component:{search_slot_ids[0]}"
        comp_rules = [rid for rid in root_of if root_of[rid] == root]
        comp_must_true = tuple(sorted(rid for rid in comp_rules if rid in must_true))
        comp_must_false = tuple(sorted(rid for rid in comp_rules if rid in must_false))
        rule_tables_set: Set[str] = set()
        for rid in comp_rules:
            rule_tables_set.update(rule_tables(rules_by_id[rid]))
        for sid in search_slot_ids:
            rule_tables_set.add(contract_slot_table(contract, sid))
        tables = tuple(sorted(rule_tables_set))
        table_set = set(tables)
        all_slots = tuple(sorted(
            (slot for slot in contract.slots if slot.table in table_set),
            key=lambda s: s.id))
        search_slots = tuple(
            next(slot for slot in contract.slots if slot.id == sid)
            for sid in search_slot_ids)
        material_rule_objs = tuple(sorted(
            (rule for rule in contract.rules
             if set(rule_tables(rule)) <= table_set),
            key=lambda r: r.id))
        component = Component(
            key=key, tables=tables, slots=all_slots,
            search_slots=search_slots, rules=material_rule_objs,
            must_true=comp_must_true, must_false=comp_must_false)
        components.append(component)
        for rid in comp_rules:
            rule_component[rid] = key

    components.sort(key=lambda c: c.key)

    involved = set(dsu.parent)
    free_slots = tuple(sorted(
        (slot for slot in contract.slots if slot.id not in involved),
        key=lambda s: s.id))
    unfalsifiable = tuple(sorted(
        rid for rid in must_false if not influence.get(rid)))
    trivial = tuple(sorted(
        rid for rid in must_true if not influence.get(rid)))
    return Analysis(domains=domains, components=tuple(components),
                    free_slots=free_slots, rule_component=rule_component,
                    unfalsifiable=unfalsifiable, trivial_rules=trivial)


def contract_slot_table(contract: Contract, slot_id: str) -> str:
    for slot in contract.slots:
        if slot.id == slot_id:
            return slot.table
    raise AssertionError(f"internal error: no slot {slot_id!r}")


# ---------------------------------------------------------------------------
# component solving


@dataclass
class ComponentResult:
    assignment: Optional[Dict[str, Candidate]]
    nodes: int
    exhaustive: bool = True


class _ComponentSolver:
    def __init__(self, contract: Contract, component: Component,
                 domains: Mapping[str, Sequence[Candidate]], max_nodes: int) -> None:
        self.contract = contract
        self.component = component
        self.slots = component.search_slots
        self.domains = [domains[slot.id] for slot in self.slots]
        self.by_id = {rule.id: rule for rule in contract.rules}
        self.must_true = component.must_true
        self.must_false = component.must_false
        self.max_nodes = max_nodes
        self.assigned: Dict[str, Candidate] = {}
        self.assigned_ids: Set[str] = set()
        self.nodes = 0
        table_slots: Dict[str, List[Slot]] = {}
        for slot in self.slots:
            table_slots.setdefault(slot.table, []).append(slot)
        self.table_slots = {t: tuple(v) for t, v in table_slots.items()}

    def _rows(self, table: str) -> Iterator[Candidate]:
        for slot in self.table_slots.get(table, ()):
            if slot.id in self.assigned_ids:
                yield self.assigned[slot.id]

    def _table_complete(self, table: str) -> bool:
        return all(slot.id in self.assigned_ids
                   for slot in self.table_slots.get(table, ()))

    # -- partial soundness pruning for must-true rules --------------------
    def _partial_true(self, rule: Rule) -> bool:
        p = rule.params
        if rule.kind == "unique":
            seen = set()
            for candidate in self._rows(p["table"]):
                key = row_key(candidate, p["key"])
                if key is None:
                    continue
                if key in seen:
                    return False
                seen.add(key)
            return True
        if rule.kind == "fk":
            parent_ready = self._table_complete(p["parent"])
            keys = set()
            if parent_ready:
                for candidate in self._rows(p["parent"]):
                    key = row_key(candidate, p["parent_key"])
                    if key is not None:
                        keys.add(key)
            for candidate in self._rows(p["child"]):
                status, values = row_reference(candidate, p["child_fields"])
                if status in ("missing", "partial"):
                    return False
                if status == "null":
                    continue
                if parent_ready and values not in keys:
                    return False
            return True
        if rule.kind == "aggregate":
            child_ready = self._table_complete(p["child"])
            parent_ready = self._table_complete(p["parent"])
            # Child amounts are only required for rows that actually match a
            # selected parent, which needs both tables complete.
            for candidate in self._rows(p["parent"]):
                if row_amount(candidate, p["parent_amount"]) is None:
                    return False
            if child_ready and parent_ready:
                return evaluate_aggregate(self.contract, rule, self.assigned)
            return True
        raise AssertionError(rule.kind)

    def _partial_ok(self) -> bool:
        return all(self._partial_true(self.by_id[rid]) for rid in self.must_true)

    def _leaf_ok(self) -> bool:
        for rid in self.must_true:
            if not evaluate_rule(self.contract, self.by_id[rid], self.assigned):
                return False
        for rid in self.must_false:
            if evaluate_rule(self.contract, self.by_id[rid], self.assigned):
                return False
        return True

    def solve(self) -> ComponentResult:
        try:
            assignment = self._dfs(0)
        except SearchLimitExceeded:
            return ComponentResult(None, self.nodes, exhaustive=False)
        return ComponentResult(assignment, self.nodes, exhaustive=True)

    def _dfs(self, index: int) -> Optional[Dict[str, Candidate]]:
        if index == len(self.slots):
            return dict(self.assigned) if self._leaf_ok() else None
        slot = self.slots[index]
        for candidate in self.domains[index]:
            self.nodes += 1
            if self.nodes > self.max_nodes:
                raise SearchLimitExceeded(
                    f"node budget {self.max_nodes} exceeded in {self.component.key}")
            self.assigned[slot.id] = candidate
            self.assigned_ids.add(slot.id)
            if self._partial_ok():
                found = self._dfs(index + 1)
                if found is not None:
                    return found
            self.assigned_ids.remove(slot.id)
            del self.assigned[slot.id]
        return None


def solve_component(contract: Contract, component: Component,
                    domains: Mapping[str, Sequence[Candidate]],
                    max_nodes: int = 5_000_000) -> ComponentResult:
    return _ComponentSolver(contract, component, domains, max_nodes).solve()


# ---------------------------------------------------------------------------
# fixtures


def selection_from_assignment(
        contract: Contract,
        assignment: Mapping[str, Candidate]) -> Dict[str, Dict[str, Any]]:
    selection: Dict[str, Dict[str, Any]] = {}
    # canonical order: slot identity, independent of input file order
    for slot in sorted(contract.slots, key=lambda s: s.id):
        candidate = assignment[slot.id]
        selection[slot.id] = {
            "candidate_id": candidate.candidate_id,
            "row": dict(candidate.row),
        }
    return selection


@dataclass
class Fixture:
    kind: str                       # "normal" or "isolated"
    target: Optional[str]
    selection: Dict[str, Dict[str, Any]]
    rule_results: Dict[str, bool]

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema": "fixture/1",
            "kind": self.kind,
            "target": self.target,
            "slots": self.selection,
            "rules": self.rule_results,
        }


@dataclass
class NoFixture:
    kind: str                       # "normal" or "isolated"
    target: Optional[str]
    reason: str
    status: str                     # "proved" or "undetermined"
    material: Optional[Dict[str, Any]] = None

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema": "no-fixture/1",
            "kind": self.kind,
            "target": self.target,
            "reason": self.reason,
            "status": self.status,
            "material": self.material,
        }


@dataclass
class Report:
    contract_id: str
    normal: Any
    isolations: Dict[str, Any]
    stats: Dict[str, Any]

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema": "fixture-report/1",
            "contract_id": self.contract_id,
            "normal": self.normal.to_json(),
            "isolations": {k: v.to_json() for k, v in self.isolations.items()},
            "stats": self.stats,
        }


# ---------------------------------------------------------------------------
# generation


def _assemble(parts: Sequence[Mapping[str, Candidate]]) -> Dict[str, Candidate]:
    assignment: Dict[str, Candidate] = {}
    for part in parts:
        assignment.update(part)
    return assignment


def _check_selection(contract: Contract, assignment: Mapping[str, Candidate],
                     must_true: Sequence[str], must_false: Sequence[str]) -> None:
    missing = [slot.id for slot in contract.slots if slot.id not in assignment]
    if missing:
        raise AssertionError(f"internal error: unassigned slots {missing}")
    for rid in must_true:
        if not evaluate_rule(contract, contract.rule(rid), assignment):
            raise AssertionError(f"internal error: rule {rid!r} unexpectedly false")
    for rid in must_false:
        if evaluate_rule(contract, contract.rule(rid), assignment):
            raise AssertionError(f"internal error: rule {rid!r} unexpectedly true")


def _fixture_from_assignment(contract: Contract, kind: str, target: Optional[str],
                             assignment: Mapping[str, Candidate]) -> Fixture:
    results = evaluate_rules(contract, assignment)
    if kind == "normal":
        if not all(results.values()):
            false_rules = sorted(k for k, v in results.items() if not v)
            raise AssertionError(
                f"internal error: normal fixture has false rule(s) {false_rules}")
    elif kind == "isolated":
        if target is None or results.get(target) is not False:
            raise AssertionError("internal error: isolated fixture does not falsify target")
        if any(not v for k, v in results.items() if k != target):
            others = sorted(k for k, v in results.items() if k != target and not v)
            raise AssertionError(
                f"internal error: isolated fixture leaves other rule(s) false: {others}")
    else:
        raise AssertionError(kind)
    return Fixture(kind, target, selection_from_assignment(contract, assignment), results)


def _material(contract: Contract, claim_specs, conclusion: str,
              solver_stats: Mapping[str, Any]) -> Dict[str, Any]:
    from .material import build_material
    return build_material(contract, claim_specs, conclusion, solver_stats)


def _free_assignment(analysis: Analysis) -> Dict[str, Candidate]:
    """Smallest admissible candidate for every slot outside all influence sets."""
    out: Dict[str, Candidate] = {}
    for slot in analysis.free_slots:
        domain = analysis.domains[slot.id]
        if not domain:
            # Cannot happen for free slots (an empty domain means a candidate
            # unarily falsified a must-true rule, which would put the slot in
            # that rule's influence set), but keep the guard honest.
            raise AssertionError(
                f"internal error: free slot {slot.id!r} has empty domain")
        out[slot.id] = domain[0]
    return out


def _solve_analysis(contract: Contract, analysis: Analysis,
                    max_nodes: int, extra_stats: Dict[str, Any]) -> Tuple[
        Optional[Dict[str, Candidate]], List[Component], List[Component], bool]:
    """Solve all components; return (assignment, failing, undetermined, ok)."""
    parts: List[Mapping[str, Candidate]] = [_free_assignment(analysis)]
    failing: List[Component] = []
    undetermined: List[Component] = []
    for component in analysis.components:
        result = solve_component(contract, component, analysis.domains,
                                 max_nodes=max_nodes)
        extra_stats["search_nodes"] = extra_stats.get("search_nodes", 0) + result.nodes
        if result.assignment is None:
            if result.exhaustive:
                failing.append(component)
            else:
                undetermined.append(component)
        else:
            parts.append(result.assignment)
    if failing or undetermined:
        return None, failing, undetermined, False
    return _assemble(parts), [], [], True


def generate(contract: Contract, isolate: Sequence[str] = (),
             max_nodes: int = 5_000_000) -> Report:
    """Build the normal fixture and each requested single-rule isolation.

    Each report entry is a :class:`Fixture` or a :class:`NoFixture`.  A
    ``NoFixture`` with status ``"proved"`` carries finite replayable material;
    ``"undetermined"`` means a search budget was exhausted and is never
    presented as a proof.
    """
    known = {r.id for r in contract.rules}
    target_ids = list(dict.fromkeys(isolate))
    for rule_id in target_ids:
        if rule_id not in known:
            raise FixtureRequestError(f"unknown rule {rule_id!r} in isolation request")

    all_rule_ids = [r.id for r in contract.rules]
    stats_extra: Dict[str, Any] = {"search_nodes": 0}
    plan_stats = {
        "slot_count": len(contract.slots),
        "rule_count": len(contract.rules),
    }

    # --- normal fixture --------------------------------------------------
    normal_analysis = build_analysis(contract, all_rule_ids, ())
    assignment, failing, undetermined, ok = _solve_analysis(
        contract, normal_analysis, max_nodes, stats_extra)

    def public_stats() -> Dict[str, Any]:
        out = dict(plan_stats)
        out["component_count"] = len(normal_analysis.components)
        out["free_slot_count"] = len(normal_analysis.free_slots)
        out["component_search_slot_counts"] = {
            c.key: len(c.search_slots) for c in normal_analysis.components}
        out["component_product_sizes"] = {
            c.key: c.product_size for c in normal_analysis.components}
        out["max_component_product"] = max(
            (c.product_size for c in normal_analysis.components), default=1)
        out["search_nodes"] = stats_extra["search_nodes"]
        return out

    if ok:
        normal: Any = _fixture_from_assignment(
            contract, "normal", None, assignment)
        _check_selection(contract, assignment, all_rule_ids, ())
    elif undetermined:
        normal = NoFixture(
            "normal", None,
            reason=("search budget exhausted for "
                    + ", ".join(c.key for c in undetermined)
                    + "; existence of a normal fixture is undetermined"),
            status="undetermined", material=None)
    else:
        blocks = [(c, c.must_true, ()) for c in failing]
        material = _material(
            contract, blocks,
            conclusion=("no assignment makes every rule true "
                        f"(unsatisfiable component(s): "
                        f"{', '.join(c.key for c in failing)})"),
            solver_stats=public_stats())
        normal = NoFixture(
            "normal", None,
            reason=("no assignment satisfies all rules; unsatisfiable "
                    "component(s): " + ", ".join(c.key for c in failing)),
            status="proved", material=material)

    # --- isolations ------------------------------------------------------
    isolations: Dict[str, Any] = {}
    for target_id in target_ids:
        must_true = [rid for rid in all_rule_ids if rid != target_id]
        analysis = build_analysis(contract, must_true, [target_id])

        # target whose truth no slot can affect: it can never be falsified
        if target_id in analysis.unfalsifiable:
            block = _unfalsifiable_block(contract, target_id)
            material = _material(
                contract, [block],
                conclusion=(f"no assignment falsifies {target_id}: no slot can "
                            "change its truth value"),
                solver_stats=public_stats())
            isolations[target_id] = NoFixture(
                "isolated", target_id,
                reason=(f"rule {target_id!r} cannot be falsified at all: "
                        "no candidate row influences it, so no isolation is "
                        "constructible"),
                status="proved", material=material)
            continue

        assignment, failing, undetermined, ok = _solve_analysis(
            contract, analysis, max_nodes, stats_extra)
        if undetermined:
            isolations[target_id] = NoFixture(
                "isolated", target_id,
                reason=("search budget exhausted for "
                        + ", ".join(c.key for c in undetermined)
                        + "; isolation is undetermined"),
                status="undetermined", material=None)
            continue
        if failing:
            blocks = [(c, c.must_true, c.must_false) for c in failing]
            target_blocks = [c for c in failing if target_id in c.must_false]
            if target_blocks:
                reason = (f"rule {target_id!r} cannot be falsified in isolation: "
                          "it is implied by the other rules it interacts with")
                conclusion = (f"cannot isolate {target_id}: no assignment "
                              f"falsifies {target_id} while the other rules in "
                              f"{', '.join(c.key for c in target_blocks)} are true")
            else:
                reason = ("unrelated component(s) have no all-true assignment, "
                          "so no fixture can keep every non-target rule true: "
                          + ", ".join(c.key for c in failing))
                conclusion = (f"cannot isolate {target_id}: component(s) "
                              + ", ".join(c.key for c in failing)
                              + " cannot make their rules true")
            material = _material(
                contract, blocks, conclusion=conclusion,
                solver_stats=public_stats())
            isolations[target_id] = NoFixture(
                "isolated", target_id, reason=reason,
                status="proved", material=material)
            continue
        isolations[target_id] = _fixture_from_assignment(
            contract, "isolated", target_id, assignment)
        _check_selection(contract, assignment, must_true, [target_id])

    return Report(contract.contract_id, normal, isolations, public_stats())


def _unfalsifiable_block(contract: Contract, rule_id: str) -> Tuple[Component, List[str], List[str]]:
    """A standalone block for "this rule can never be false" conclusions."""
    rule = contract.rule(rule_id)
    tables = rule_tables(rule)
    table_set = set(tables)
    slots = tuple(sorted(
        (slot for slot in contract.slots if slot.table in table_set),
        key=lambda s: s.id))
    return (Component(
        key=f"rule:{rule_id}", tables=tables, slots=slots,
        search_slots=slots, rules=(rule,), must_true=(), must_false=(rule_id,)),
        [], [rule_id])


# ---------------------------------------------------------------------------
# verification


def verify_fixture(contract: Contract, fixture: Mapping[str, Any]) -> Dict[str, Any]:
    """Re-evaluate a fixture from its selected rows, without trusting the generator."""
    errors: List[str] = []
    selection = fixture.get("slots")
    if not isinstance(selection, dict):
        return {"ok": False, "errors": ["fixture has no 'slots' object"], "computed": {}}

    kind = fixture.get("kind")
    target = fixture.get("target")
    if kind not in ("normal", "isolated"):
        errors.append(f"unknown fixture kind {kind!r}")
    if kind == "isolated" and target not in {r.id for r in contract.rules}:
        errors.append(f"isolated fixture names unknown rule {target!r}")

    contract_slots = {slot.id for slot in contract.slots}
    fixture_slots = set(selection)
    missing = sorted(contract_slots - fixture_slots)
    extra = sorted(fixture_slots - contract_slots)
    if missing:
        errors.append(f"missing slot(s): {missing}")
    if extra:
        errors.append(f"unknown slot(s): {extra}")

    assignment: Dict[str, Candidate] = {}
    for slot in contract.slots:
        entry = selection.get(slot.id)
        if entry is None:
            continue
        if not isinstance(entry, dict) or "candidate_id" not in entry or "row" not in entry:
            errors.append(f"slot {slot.id!r}: malformed selection entry")
            continue
        cid = entry["candidate_id"]
        match = next((c for c in slot.candidates if c.candidate_id == cid), None)
        if match is None:
            errors.append(
                f"slot {slot.id!r}: candidate {cid!r} is not offered by the contract")
            continue
        if dict(match.row) != entry["row"]:
            errors.append(
                f"slot {slot.id!r}: selected row does not match candidate {cid!r}")
            continue
        assignment[slot.id] = match

    if errors:
        return {"ok": False, "errors": errors, "computed": {}}

    computed = evaluate_rules(contract, assignment)

    stated = fixture.get("rules")
    if not isinstance(stated, dict):
        errors.append("fixture has no 'rules' object")
    else:
        if set(stated) != set(computed):
            errors.append("rule result keys do not match the contract's rules")
        for rule_id, value in computed.items():
            if rule_id in stated and stated[rule_id] != value:
                errors.append(
                    f"rule {rule_id!r}: fixture states {stated[rule_id]!r}, "
                    f"independent evaluation gives {value!r}")

    if kind == "normal":
        false_rules = sorted(k for k, v in computed.items() if not v)
        if false_rules:
            errors.append(f"normal fixture has false rule(s): {false_rules}")
    elif kind == "isolated" and target in computed:
        if computed[target] is not False:
            errors.append(f"isolated fixture does not falsify {target!r}")
        others = sorted(k for k, v in computed.items() if k != target and not v)
        if others:
            errors.append(f"isolated fixture leaves other rule(s) false: {others}")

    return {"ok": not errors, "errors": errors, "computed": computed}


def verify_report(contract: Contract, report: Mapping[str, Any]) -> Dict[str, Any]:
    """Verify every fixture contained in a generator report.

    Additionally, for every ``NoFixture`` that carries material, the
    impossibility claim is replayed against the independent naive enumerator
    when it is small enough, so a report is not accepted merely because its
    fixtures happen to be fine.
    """
    from .material import replay_material

    results: Dict[str, Any] = {}
    errors: List[str] = []
    normal = report.get("normal") or {}
    if normal.get("schema") == "fixture/1":
        results["normal"] = verify_fixture(contract, normal)
        if not results["normal"]["ok"]:
            errors.append("normal fixture failed verification")
    elif normal.get("schema") == "no-fixture/1":
        result = _verify_no_fixture(contract, normal, replay_material)
        results["normal"] = result
        if not result["ok"]:
            errors.append(f"normal no-fixture claim failed: {result['errors']}")
    else:
        errors.append("report has no normal entry")

    for rule_id, entry in (report.get("isolations") or {}).items():
        if not isinstance(entry, dict):
            errors.append(f"isolation {rule_id!r}: malformed entry")
            continue
        if entry.get("schema") == "fixture/1":
            results[rule_id] = verify_fixture(contract, entry)
            if not results[rule_id]["ok"]:
                errors.append(f"isolation {rule_id!r} fixture failed verification")
        elif entry.get("schema") == "no-fixture/1":
            result = _verify_no_fixture(contract, entry, replay_material)
            results[rule_id] = result
            if not result["ok"]:
                errors.append(
                    f"isolation {rule_id!r} no-fixture claim failed: {result['errors']}")
        else:
            errors.append(f"isolation {rule_id!r}: unknown entry schema")
    return {"ok": not errors, "fixtures": results, "errors": errors}


def _verify_no_fixture(contract: Contract, entry: Mapping[str, Any], replay) -> Dict[str, Any]:
    if entry.get("kind") == "normal":
        target = None
    else:
        target = entry.get("target")
        if target not in {r.id for r in contract.rules}:
            return {"ok": False, "errors": [f"unknown target rule {target!r}"]}
    status = entry.get("status")
    if status == "undetermined":
        # An honest admission, not a claim: nothing to replay.
        return {"ok": True, "replayed": False, "reason": "undetermined"}
    if status != "proved":
        return {"ok": False, "errors": [f"unknown no-fixture status {status!r}"]}
    material = entry.get("material")
    if not isinstance(material, dict):
        return {"ok": False, "errors": ["proved no-fixture has no material"]}
    result = replay(material)
    if result.get("undetermined"):
        return {"ok": True, "replayed": False,
                "reason": "material too large for naive replay",
                "blocks": result.get("blocks")}
    if not result["ok"]:
        return {"ok": False, "errors": ["material replay failed: "
                                        + "; ".join(
                                            e for b in result.get("blocks", [])
                                            for e in b.get("errors", []))]}
    return {"ok": True, "replayed": True, "blocks": result.get("blocks")}


# ---------------------------------------------------------------------------
# independent brute-force enumeration (small slot sets only)


def enumerate_assignments(contract: Contract,
                          max_assignments: int = 1_000_000) -> Iterator[
                              Dict[str, Candidate]]:
    """Yield complete assignments in slot-id / candidate-id lexicographic order.

    Deliberately the naive product; it exists for small independent
    cross-checks and for replaying impossibility material, never as the
    generator's search.
    """
    slots = sorted(contract.slots, key=lambda s: s.id)
    count = 0
    for combo in itertools.product(*[
        sorted(slot.candidates, key=lambda c: c.candidate_id) for slot in slots
    ]):
        count += 1
        if count > max_assignments:
            raise SearchLimitExceeded(
                f"enumeration exceeds max_assignments={max_assignments}")
        yield {slot.id: candidate for slot, candidate in zip(slots, combo)}


def brute_force_first(contract: Contract, must_true: Sequence[str],
                      must_false: Sequence[str] = (),
                      max_assignments: int = 1_000_000) -> Optional[
                          Dict[str, Candidate]]:
    """First satisfying assignment in lexicographic order, by naive enumeration."""
    for assignment in enumerate_assignments(contract, max_assignments):
        if all(evaluate_rule(contract, contract.rule(rid), assignment)
               for rid in must_true) and not any(
                   evaluate_rule(contract, contract.rule(rid), assignment)
                   for rid in must_false):
            return assignment
    return None
