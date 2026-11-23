"""One-shot multi-batch merge of verified fixtures into one loadable pack.

Several teams independently produce synthetic fixtures for the *same*
contract.  Each fixture verifies on its own, but concatenating them collides
on rule keys and can attach child detail rows to another batch's parent rows.
This module merges the fixtures without changing the target schema: no batch
column is added, no row is dropped and no amount is changed.

Inputs
------
* one parsed :class:`~fixturegen.contract.Contract`;
* at least two ``fixture/1`` documents (normal or single-rule isolations),
  each carrying a distinct batch id;
* a rewrite whitelist listing, by table and field, the only columns whose
  string values may be replaced.

Every fixture is independently re-verified with
:func:`fixturegen.engine.verify_fixture`; the rows actually used are the
contract's own candidate rows that verification matched, never the rows or the
self-reported rule table as written in the fixture document.

Identity domains
----------------
Rules compare columns position by position on composite keys.  Two rule-key
columns are in the same *identity domain* when a rule compares them directly
(FK / aggregate child field vs parent key field at the same composite
position) or when they are linked transitively through other rules; a rule-key
column connected to no other column forms a domain of its own.  Uniqueness
rules contribute their key columns to the domain graph as nodes.

Rewrite discipline
------------------
Only whitelisted columns participate in rewriting, and the whitelist may only
name string-typed rule-key columns.  Absent fields, explicit JSON nulls,
integers and every other field are reproduced untouched.  Within one batch and
one domain:

* the same original string is replaced by the same replacement everywhere it
  occurs in whitelisted columns, and distinct original strings get distinct
  replacements (injectivity);
* an original string that also occurs in a *non-whitelisted* column of the
  same batch and domain is **locked**: it is kept verbatim everywhere in that
  batch and domain.  Locking one value never locks the domain's other values.

Every non-locked value receives a replacement that is globally unique across
the whole pack and disjoint from every original string appearing in any
fixture, so a free position can never equal anything elsewhere after the
merge.  The minting loop keeps drawing fresh identifiers until an unused one
is found: an occupied prefix can never make an otherwise feasible merge fail.

Cross-batch safety and why the conflict scan is not pairwise
------------------------------------------------------------
The pack must not (a) create equal keys under one uniqueness rule across
batches, or (b) make a child row match a parent row of another batch under any
FK or aggregate rule.  After rewriting, a cross-batch equality can survive
only when *every* compared position is a fixed value (null, integer or a
locked string): a free position carries a globally unique replacement.  The
conflict scan therefore indexes, per rule, only the rows whose compared tuple
is fully fixed and reports the first cross-batch index hit.  It is linear in
the number of rows and never enumerates the cross-batch row-pair product,
which matters when a hot code is repeated across many large fixtures.  Each
unresolvable conflict is reported as a checkable witness naming the rule id
and the two batches and slots involved, so the whitelist can be revised.

Everything that is not a cross-batch equality is preserved: within-batch rule
truth (including existing uniqueness collisions, missing/partial
references, multi-parent hits and aggregate contribution attribution),
absent-vs-null distinctions and all integers.  Pack success is never inferred
from unchanged whole-pack rule truth values or from cancelling amounts.

The output is deterministic: only the contract, whitelist and the multiset of
batched fixtures matter; reordering the input documents produces a byte-equal
pack.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contract import Candidate, Contract, MISSING
from .engine import evaluate_rules, row_key, row_reference, verify_fixture

PACK_SCHEMA = "merge-pack/1"
WHITELIST_SCHEMA = "rewrite-whitelist/1"
FIXTURE_SCHEMA = "fixture/1"


class MergeInputError(ValueError):
    """The merge request or one of its input documents is not acceptable."""


class MergeConflictError(ValueError):
    """The fixed values leave at least one cross-batch conflict unbreakable."""

    def __init__(self, witnesses: Sequence[Mapping[str, Any]]) -> None:
        if not witnesses:
            raise AssertionError("MergeConflictError needs at least one witness")
        self.witnesses: List[Dict[str, Any]] = [dict(w) for w in witnesses]
        first = self.witnesses[0]
        super().__init__(
            f"unresolvable cross-batch conflict on rule {first['rule_id']!r}; "
            f"{len(self.witnesses)} witness(es) provided")


# ---------------------------------------------------------------------------
# identity domains


@dataclass(frozen=True)
class Domain:
    domain_id: str
    columns: Tuple[Tuple[str, str], ...]  # (table, field), sorted


class _DSU:
    def __init__(self) -> None:
        self.parent: Dict[Tuple[str, str], Tuple[str, str]] = {}

    def add(self, item: Tuple[str, str]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: Tuple[str, str]) -> Tuple[str, str]:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a: Tuple[str, str], b: Tuple[str, str]) -> None:
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _rule_column_pairs(rule) -> List[Tuple[Tuple[str, str], Tuple[str, str]]]:
    """Equal-position column pairs a rule compares; empty for uniqueness."""
    p = rule.params
    if rule.kind == "unique":
        return []
    child_table = p["child"]
    parent_table = p["parent"]
    return [
        ((child_table, cf), (parent_table, pf))
        for cf, pf in zip(p["child_fields"], p["parent_key"])
    ]


def build_identity_domains(contract: Contract) -> Tuple[Domain, ...]:
    """Union-find over rule-key columns following every rule comparison."""
    dsu = _DSU()
    for rule in contract.rules:
        if rule.kind == "unique":
            table = rule.params["table"]
            for fname in rule.params["key"]:
                dsu.add((table, fname))
            continue
        for left, right in _rule_column_pairs(rule):
            dsu.union(left, right)

    groups: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for column in dsu.parent:
        groups.setdefault(dsu.find(column), []).append(column)

    domains: List[Domain] = []
    for index, columns in enumerate(sorted(sorted(cols) for cols in groups.values())):
        domains.append(Domain(domain_id=f"D{index + 1:02d}",
                              columns=tuple(columns)))
    return tuple(domains)


# ---------------------------------------------------------------------------
# whitelist parsing and validation


def parse_whitelist(doc: Any, contract: Contract) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(doc, dict) or doc.get("schema") != WHITELIST_SCHEMA:
        raise MergeInputError(
            f"whitelist must be a JSON object with schema {WHITELIST_SCHEMA!r}")
    raw = doc.get("fields")
    if not isinstance(raw, list):
        raise MergeInputError("whitelist 'fields' must be a list")
    rule_columns: Set[Tuple[str, str]] = set()
    for domain in build_identity_domains(contract):
        rule_columns.update(domain.columns)

    out: Set[Tuple[str, str]] = set()
    for index, entry in enumerate(raw):
        where = f"whitelist.fields[{index}]"
        if not isinstance(entry, dict):
            raise MergeInputError(f"{where}: entry must be an object")
        table_name = entry.get("table")
        field_name = entry.get("field")
        if not isinstance(table_name, str) or not isinstance(field_name, str):
            raise MergeInputError(f"{where}: 'table' and 'field' must be strings")
        if table_name not in contract.tables:
            raise MergeInputError(f"{where}: undefined table {table_name!r}")
        table = contract.tables[table_name]
        try:
            fld = table.field(field_name)
        except KeyError:
            raise MergeInputError(
                f"{where}: field {field_name!r} is not defined on table "
                f"{table_name!r}") from None
        if fld.type != "str":
            raise MergeInputError(
                f"{where}: only string fields are rewritable, but "
                f"{table_name}.{field_name} is {fld.type}")
        if (table_name, field_name) not in rule_columns:
            raise MergeInputError(
                f"{where}: {table_name}.{field_name} does not participate in "
                "any rule key and cannot be rewritten")
        if (table_name, field_name) in out:
            raise MergeInputError(
                f"{where}: duplicate whitelist entry "
                f"{table_name}.{field_name}")
        out.add((table_name, field_name))
    return tuple(sorted(out))


def load_whitelist(path: str, contract: Contract) -> Tuple[Tuple[str, str], ...]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise MergeInputError(f"invalid JSON in {path}: {exc}") from exc
    return parse_whitelist(doc, contract)


def load_fixture_doc(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise MergeInputError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != FIXTURE_SCHEMA:
        raise MergeInputError(
            f"{path}: not a {FIXTURE_SCHEMA} fixture (normal or isolated "
            "fixture documents only; no-fixture documents cannot be merged)")
    return doc


# ---------------------------------------------------------------------------
# merge


@dataclass(frozen=True)
class _Batch:
    batch_id: str
    kind: str
    target: Optional[str]
    rule_results: Dict[str, bool]
    assignment: Dict[str, Candidate]


def _verify_batches(contract: Contract,
                    batches: Sequence[Tuple[str, Mapping[str, Any]]]
                    ) -> List[_Batch]:
    if len(batches) < 2:
        raise MergeInputError("a merge needs at least two batches")
    seen_ids: Set[str] = set()
    verified: List[_Batch] = []
    for batch_id, fixture in batches:
        if not isinstance(batch_id, str) or not batch_id:
            raise MergeInputError("batch ids must be non-empty strings")
        if batch_id in seen_ids:
            raise MergeInputError(f"duplicate batch id {batch_id!r}")
        seen_ids.add(batch_id)
        result = verify_fixture(contract, fixture)
        if not result["ok"]:
            raise MergeInputError(
                f"batch {batch_id!r} failed independent fixture verification: "
                + "; ".join(result["errors"]))
        kind = fixture.get("kind")
        target = fixture.get("target")
        assignment: Dict[str, Candidate] = {}
        for slot in contract.slots:
            entry = fixture["slots"][slot.id]
            candidate = next(c for c in slot.candidates
                             if c.candidate_id == entry["candidate_id"])
            assignment[slot.id] = candidate
        verified.append(_Batch(
            batch_id=batch_id, kind=kind, target=target,
            rule_results=dict(result["computed"]), assignment=assignment))
    return verified


def _all_original_strings(contract: Contract,
                          batches: Sequence[_Batch]) -> Set[str]:
    used: Set[str] = set()
    for batch in batches:
        for slot in contract.slots:
            candidate = batch.assignment[slot.id]
            for _, value in candidate.row:
                if isinstance(value, str):
                    used.add(value)
    return used


def _build_rewrites(
    contract: Contract,
    batches: Sequence[_Batch],
    domains: Sequence[Domain],
    whitelist: Set[Tuple[str, str]],
) -> Tuple[Dict[Tuple[str, str], Dict[str, str]],
           Dict[Tuple[str, str], Set[str]]]:
    """Return (replacements, locked) keyed by (batch_id, domain_id)."""
    column_domain: Dict[Tuple[str, str], str] = {}
    for domain in domains:
        for column in domain.columns:
            column_domain[column] = domain.domain_id

    locked: Dict[Tuple[str, str], Set[str]] = {}
    free_values: Dict[Tuple[str, str], Set[str]] = {}
    for batch in batches:
        for slot in contract.slots:
            candidate = batch.assignment[slot.id]
            for field_name, value in candidate.row:
                if not isinstance(value, str):
                    continue
                column = (slot.table, field_name)
                if column not in column_domain:
                    continue
                domain_id = column_domain[column]
                key = (batch.batch_id, domain_id)
                if column in whitelist:
                    free_values.setdefault(key, set()).add(value)
                else:
                    locked.setdefault(key, set()).add(value)

    # A value is locked when it occurs in a non-whitelisted column anywhere in
    # the same batch and domain; it then stays verbatim in every position.
    replacements: Dict[Tuple[str, str], Dict[str, str]] = {}
    used = _all_original_strings(contract, batches)
    counter = 0

    def mint() -> str:
        nonlocal counter
        while True:
            candidate = f"fg-merge-{counter}"
            counter += 1
            if candidate not in used:
                used.add(candidate)
                return candidate

    for batch_id in sorted(b.batch_id for b in batches):
        for domain in domains:
            key = (batch_id, domain.domain_id)
            locked_here = locked.get(key, set())
            maps: Dict[str, str] = {}
            for original in sorted(free_values.get(key, set())):
                if original in locked_here:
                    continue
                maps[original] = mint()
            if maps:
                replacements[key] = maps
    return replacements, locked


def _rewritten_row(table_name: str, candidate: Candidate,
                   batch_id: str,
                   column_domain: Mapping[Tuple[str, str], str],
                   whitelist: Set[Tuple[str, str]],
                   replacements: Mapping[Tuple[str, str], Mapping[str, str]]
                   ) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for field_name, value in candidate.row:
        column = (table_name, field_name)
        if isinstance(value, str) and column in whitelist:
            domain_id = column_domain[column]
            value = replacements.get((batch_id, domain_id), {}).get(
                value, value)
        out[field_name] = value
    return out


def _is_fixed(value: Any, batch_id: str, domain_id: str,
              locked: Mapping[Tuple[str, str], Set[str]],
              replacements: Mapping[Tuple[str, str], Mapping[str, str]]) -> bool:
    if not isinstance(value, str):
        return True  # null and integers can never be rewritten
    if value in locked.get((batch_id, domain_id), set()):
        return True
    return value not in replacements.get((batch_id, domain_id), {})


def _witness(rule_id: str, kind: str,
             a: Tuple[str, str, str], b: Tuple[str, str, str],
             key_tuple: Sequence[Any], note: str) -> Dict[str, Any]:
    def side(item: Tuple[str, str, str]) -> Dict[str, str]:
        batch_id, slot_id, table_name = item
        return {"batch_id": batch_id, "slot_id": slot_id, "table": table_name}

    return {
        "rule_id": rule_id,
        "rule_kind": kind,
        "a": side(a),
        "b": side(b),
        "fixed_key": list(key_tuple),
        "detail": note,
    }


def _cross_batch_conflicts(
    contract: Contract,
    batches: Sequence[_Batch],
    domains: Sequence[Domain],
        locked: Mapping[Tuple[str, str], Set[str]],
        replacements: Mapping[Tuple[str, str], Mapping[str, str]],
) -> List[Dict[str, Any]]:
    """Linear index scan for the conflicts rewriting cannot break.

    Only fully fixed tuples are indexed: a tuple with any free position
    carries a globally unique replacement afterwards and cannot equal any
    tuple of another batch.
    """
    column_domain: Dict[Tuple[str, str], str] = {}
    for domain in domains:
        for column in domain.columns:
            column_domain[column] = domain.domain_id

    def fixed_tuple(batch_id: str, table_name: str,
                    candidate: Candidate, fields: Sequence[str]
                    ) -> Optional[Tuple[Any, ...]]:
        values: List[Any] = []
        for field_name in fields:
            value = candidate.get(field_name)
            if value is MISSING:
                return None
            domain_id = column_domain.get((table_name, field_name))
            if domain_id is None or _is_fixed(value, batch_id, domain_id,
                                              locked, replacements):
                values.append(value)
            else:
                return None
        return tuple(values)

    witnesses: List[Dict[str, Any]] = []
    by_id = {batch.batch_id: batch for batch in batches}

    for rule in contract.rules:
        p = rule.params
        if rule.kind == "unique":
            table_name = p["table"]
            index: Dict[Tuple[Any, ...], List[Tuple[str, str, str]]] = {}
            hit = False
            for batch_id in sorted(by_id):
                if hit:
                    break
                batch = by_id[batch_id]
                for slot in contract.slots_for_table(table_name):
                    candidate = batch.assignment[slot.id]
                    key = fixed_tuple(batch_id, table_name, candidate, p["key"])
                    if key is None:
                        continue
                    side = (batch_id, slot.id, table_name)
                    prior_hit = next((x for x in index.setdefault(key, [])
                                      if x[0] != batch_id), None)
                    if prior_hit is not None:
                        witnesses.append(_witness(
                            rule.id, "unique", prior_hit, side, key,
                            "duplicate fully-fixed unique key across batches; "
                            "first witness only, more may exist"))
                        hit = True
                        break
                    index[key].append(side)
            continue

        # FK and aggregate rules share the child-tuple vs parent-key shape.
        parent_table = p["parent"]
        child_table = p["child"]
        parent_fields = p["parent_key"]
        child_fields = p["child_fields"]
        parent_index: Dict[Tuple[Any, ...], List[Tuple[str, str, str]]] = {}
        for batch_id in sorted(by_id):
            batch = by_id[batch_id]
            for slot in contract.slots_for_table(parent_table):
                candidate = batch.assignment[slot.id]
                key = fixed_tuple(batch_id, parent_table, candidate,
                                  parent_fields)
                if key is None:
                    continue
                parent_index.setdefault(key, []).append(
                    (batch_id, slot.id, parent_table))

        hit = False
        for batch_id in sorted(by_id):
            if hit:
                break
            batch = by_id[batch_id]
            for slot in contract.slots_for_table(child_table):
                candidate = batch.assignment[slot.id]
                status, values = row_reference(candidate, child_fields)
                if status != "value":
                    continue  # null / partial / missing references cannot match
                key = fixed_tuple(batch_id, child_table, candidate,
                                  child_fields)
                if key is None:
                    continue
                side = (batch_id, slot.id, child_table)
                parent_hit = next((x for x in parent_index.get(key, ())
                                   if x[0] != batch_id), None)
                if parent_hit is not None:
                    witnesses.append(_witness(
                        rule.id, rule.kind, side, parent_hit, key,
                        "child reference matches a parent row of another "
                        "batch through fully-fixed values; first witness "
                        "only, more may exist"))
                    hit = True
                    break
    return witnesses


def merge_fixtures(contract: Contract,
                   batches: Sequence[Tuple[str, Mapping[str, Any]]],
                   whitelist_doc: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the deterministic multi-batch pack or raise with witnesses."""
    whitelist = set(parse_whitelist(whitelist_doc, contract))
    verified = sorted(_verify_batches(contract, batches),
                      key=lambda b: b.batch_id)
    domains = build_identity_domains(contract)
    replacements, locked = _build_rewrites(
        contract, verified, domains, whitelist)

    witnesses = _cross_batch_conflicts(
        contract, verified, domains, locked, replacements)
    if witnesses:
        raise MergeConflictError(witnesses)

    column_domain: Dict[Tuple[str, str], str] = {}
    for domain in domains:
        for column in domain.columns:
            column_domain[column] = domain.domain_id

    slot_order = {slot.id: index for index, slot in enumerate(contract.slots)}
    tables_out: Dict[str, List[Dict[str, Any]]] = {}
    for batch in verified:
        for slot in contract.slots:
            candidate = batch.assignment[slot.id]
            row = _rewritten_row(slot.table, candidate, batch.batch_id,
                                 column_domain, whitelist, replacements)
            tables_out.setdefault(slot.table, []).append({
                "source": {
                    "batch_id": batch.batch_id,
                    "slot_id": slot.id,
                    "candidate_id": candidate.candidate_id,
                },
                "row": row,
            })
    for entries in tables_out.values():
        entries.sort(key=lambda e: (e["source"]["batch_id"],
                                    slot_order[e["source"]["slot_id"]]))

    rewrites_out = [
        {"batch_id": batch_id,
         "domain_id": domain_id,
         "original": original,
         "replacement": maps[original]}
        for (batch_id, domain_id), maps in sorted(replacements.items())
        for original in sorted(maps)
    ]

    return {
        "schema": PACK_SCHEMA,
        "contract_id": contract.contract_id,
        "whitelist": [{"table": t, "field": f} for t, f in sorted(whitelist)],
        "domains": [
            {"domain_id": domain.domain_id,
             "columns": [{"table": t, "field": f} for t, f in domain.columns]}
            for domain in domains
        ],
        "batches": [
            {"batch_id": batch.batch_id,
             "fixture_kind": batch.kind,
             "target": batch.target,
             "rule_results": dict(sorted(batch.rule_results.items()))}
            for batch in verified
        ],
        "tables": {name: tables_out[name]
                   for name in sorted(tables_out)},
        "rewrites": rewrites_out,
    }


# ---------------------------------------------------------------------------
# independent pack verification


def _pack_candidate(contract: Contract, table_name: str,
                    row: Mapping[str, Any]) -> Candidate:
    table = contract.tables[table_name]
    items: List[Tuple[str, Any]] = []
    for fld in table.fields:
        if fld.name in row:
            items.append((fld.name, row[fld.name]))
    return Candidate(candidate_id="pack", row=tuple(items))


def verify_pack(contract: Contract, pack: Mapping[str, Any]) -> Dict[str, Any]:
    """Audit a pack without trusting the builder.

    Re-derives every load row from the named contract candidate and the
    pack's own rewrite table, re-evaluates every rule per batch on the
    rewritten rows, and re-runs the linear cross-batch scan on the final
    values.
    """
    errors: List[str] = []
    if not isinstance(pack, dict) or pack.get("schema") != PACK_SCHEMA:
        return {"ok": False, "errors": [f"not a {PACK_SCHEMA} document"]}
    if pack.get("contract_id") != contract.contract_id:
        errors.append("contract_id does not match the contract")

    try:
        whitelist = set(parse_whitelist(
            {"schema": WHITELIST_SCHEMA, "fields": pack.get("whitelist")},
            contract))
    except MergeInputError as exc:
        errors.append(str(exc))
        whitelist = set()

    domains = build_identity_domains(contract)
    pack_domains = pack.get("domains")
    expected_domains = [
        {"domain_id": d.domain_id,
         "columns": [{"table": t, "field": f} for t, f in d.columns]}
        for d in domains]
    if pack_domains != expected_domains:
        errors.append("identity domains do not match the contract's rule graph")

    raw_batches = pack.get("batches")
    raw_tables = pack.get("tables")
    if not isinstance(raw_batches, list) or len(raw_batches) < 2:
        errors.append("pack must contain at least two batches")
        return {"ok": False, "errors": errors}
    if not isinstance(raw_tables, dict):
        errors.append("pack 'tables' must be an object")
        return {"ok": False, "errors": errors}

    if not all(isinstance(b, dict) for b in raw_batches):
        errors.append("every batches[] entry must be an object")
        return {"ok": False, "errors": errors}
    batch_ids = [b.get("batch_id") for b in raw_batches]
    if len(set(batch_ids)) != len(batch_ids) or not all(
            isinstance(x, str) and x for x in batch_ids):
        errors.append("batch ids must be distinct non-empty strings")

    column_domain: Dict[Tuple[str, str], str] = {}
    for domain in domains:
        for column in domain.columns:
            column_domain[column] = domain.domain_id

    # Re-derive rewrite maps from the pack's rewrite ledger, checking
    # injectivity and disjointness from every original candidate string.
    replacements: Dict[Tuple[str, str], Dict[str, str]] = {}
    originals: Set[str] = set()
    for slot in contract.slots:
        for candidate in slot.candidates:
            originals.update(v for _, v in candidate.row
                             if isinstance(v, str))
    domain_ids = {d.domain_id for d in domains}
    for entry in pack.get("rewrites", []):
        if not all(isinstance(entry.get(k), str) for k in
                   ("batch_id", "domain_id", "original", "replacement")):
            errors.append(f"malformed rewrite entry {entry!r}")
            continue
        if entry["batch_id"] not in set(batch_ids):
            errors.append(
                f"rewrite entry names unknown batch {entry['batch_id']!r}")
            continue
        if entry["domain_id"] not in domain_ids:
            errors.append(
                f"rewrite entry names unknown domain "
                f"{entry['domain_id']!r}")
            continue
        key = (entry["batch_id"], entry["domain_id"])
        maps = replacements.setdefault(key, {})
        if entry["original"] in maps:
            errors.append(
                f"duplicate rewrite for {key}: {entry['original']!r}")
        if entry["replacement"] in originals:
            errors.append(
                f"replacement {entry['replacement']!r} collides with an "
                "original fixture value")
        if entry["replacement"] in maps.values():
            errors.append(
                f"non-injective rewrite in {key}: {entry['replacement']!r}")
        maps[entry["original"]] = entry["replacement"]

    # Reconstruct each batch's rows from the contract candidate named by the
    # source ledger and compare against the packaged row, field by field,
    # including absent fields and explicit nulls.
    per_batch_rows: Dict[str, Dict[str, Candidate]] = {
        bid: {} for bid in batch_ids}
    seen_sources: Set[Tuple[str, str]] = set()
    expected_count = len(batch_ids) * len(contract.slots)
    actual_count = 0
    for table_name in sorted(contract.tables):
        entries = raw_tables.get(table_name, [])
        if not isinstance(entries, list):
            errors.append(f"tables.{table_name} must be a list")
            continue
        for entry in entries:
            actual_count += 1
            source = entry.get("source") if isinstance(entry, dict) else None
            row = entry.get("row") if isinstance(entry, dict) else None
            if not isinstance(source, dict) or not isinstance(row, dict):
                errors.append(f"tables.{table_name}: malformed entry")
                continue
            bid, sid, cid = (source.get("batch_id"), source.get("slot_id"),
                             source.get("candidate_id"))
            if bid not in per_batch_rows:
                errors.append(f"entry names unknown batch {bid!r}")
                continue
            slot = next((s for s in contract.slots if s.id == sid), None)
            if slot is None or slot.table != table_name:
                errors.append(
                    f"source {source!r} does not match a slot of {table_name!r}")
                continue
            if (bid, sid) in seen_sources:
                errors.append(f"duplicate pack row for batch {bid!r} slot {sid!r}")
            seen_sources.add((bid, sid))
            candidate = next((c for c in slot.candidates
                              if c.candidate_id == cid), None)
            if candidate is None:
                errors.append(
                    f"unknown candidate {cid!r} for slot {sid!r}")
                continue
            expected_row = _rewritten_row(
                table_name, candidate, bid, column_domain, whitelist,
                replacements)
            if dict(row) != expected_row:
                errors.append(
                    f"pack row for {bid!r}/{sid!r} does not follow from "
                    f"candidate {cid!r} and the rewrite ledger")
            per_batch_rows[bid][sid] = _pack_candidate(
                contract, table_name, row)

    if actual_count != expected_count:
        errors.append(
            f"pack has {actual_count} rows, expected one row per batch per "
            f"slot ({expected_count})")
    for bid in batch_ids:
        missing = sorted(s.id for s in contract.slots
                         if s.id not in per_batch_rows.get(bid, {}))
        if missing:
            errors.append(f"batch {bid!r} is missing rows for slots {missing}")

    # Per-batch rule truth on the rewritten rows must match the stated,
    # independently computed fixture results.
    for batch_doc in raw_batches:
        bid = batch_doc.get("batch_id")
        rows = per_batch_rows.get(bid, {})
        if len(rows) != len(contract.slots):
            continue
        computed = evaluate_rules(contract, rows)
        stated = batch_doc.get("rule_results")
        if not isinstance(stated, dict) or stated != computed:
            errors.append(
                f"batch {bid!r}: packaged rule results disagree with "
                "independent evaluation")
        kind = batch_doc.get("fixture_kind")
        target = batch_doc.get("target")
        if kind == "normal" and not all(computed.values()):
            errors.append(f"batch {bid!r}: normal fixture has false rules")
        if kind == "isolated":
            if computed.get(target) is not False or any(
                    v for k, v in computed.items() if k != target and not v):
                errors.append(
                    f"batch {bid!r}: isolation semantics not preserved")

    # Final-value cross-batch scan on the packaged rows: index every complete
    # fixed tuple and flag only equalities spanning two batches.
    pack_batches = [
        _Batch(batch_id=bid, kind="", target=None,
               rule_results={}, assignment=per_batch_rows[bid])
        for bid in batch_ids if len(per_batch_rows.get(bid, {})) ==
        len(contract.slots)]
    if pack_batches:
        # In the final rows every replacement is already applied, so from the
        # scanner's perspective all final values behave as fixed.
        final_witnesses = _final_cross_batch_scan(contract, pack_batches)
        for witness in final_witnesses:
            errors.append(
                f"cross-batch conflict on rule {witness['rule_id']!r}: "
                f"{witness['a']['batch_id']}/{witness['a']['slot_id']} vs "
                f"{witness['b']['batch_id']}/{witness['b']['slot_id']}")
    return {"ok": not errors, "errors": errors}


def _final_cross_batch_scan(
        contract: Contract, batches: Sequence[_Batch]
        ) -> List[Dict[str, Any]]:
    """Cross-batch equalities over the final rows, linear indexing."""
    witnesses: List[Dict[str, Any]] = []
    by_id = {batch.batch_id: batch for batch in batches}
    for rule in contract.rules:
        p = rule.params
        if rule.kind == "unique":
            table_name = p["table"]
            index: Dict[Tuple[Any, ...], List[Tuple[str, str, str]]] = {}
            for bid in sorted(by_id):
                for slot in contract.slots_for_table(table_name):
                    candidate = by_id[bid].assignment[slot.id]
                    key = row_key(candidate, p["key"])
                    if key is None:
                        continue
                    side = (bid, slot.id, table_name)
                    for prior in index.setdefault(key, []):
                        if prior[0] != bid:
                            witnesses.append(_witness(
                                rule.id, "unique", prior, side, key,
                                "cross-batch duplicate key in packaged rows"))
                    index[key].append(side)
            continue

        parent_index: Dict[Tuple[Any, ...], List[Tuple[str, str, str]]] = {}
        for bid in sorted(by_id):
            for slot in contract.slots_for_table(p["parent"]):
                candidate = by_id[bid].assignment[slot.id]
                key = row_key(candidate, p["parent_key"])
                if key is None:
                    continue
                parent_index.setdefault(key, []).append(
                    (bid, slot.id, p["parent"]))
        for bid in sorted(by_id):
            for slot in contract.slots_for_table(p["child"]):
                candidate = by_id[bid].assignment[slot.id]
                status, values = row_reference(candidate, p["child_fields"])
                if status != "value":
                    continue
                side = (bid, slot.id, p["child"])
                for parent_side in parent_index.get(values, ()):
                    if parent_side[0] != bid:
                        witnesses.append(_witness(
                            rule.id, rule.kind, side, parent_side, values,
                            "cross-batch parent match in packaged rows"))
    return witnesses


# ---------------------------------------------------------------------------
# atomic output


def write_pack_atomic(pack: Mapping[str, Any], path: str) -> None:
    """Write the pack via a temp file and rename; failures leave no trace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".merge-pack-", suffix=".tmp",
                                    dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(pack, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
