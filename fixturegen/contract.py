"""Strict parsing and semantic validation of the narrow contract family.

A contract is a finite JSON object.  We *reject* (raise ContractError) any
document that is malformed or that contains undefined candidate references:
a malformed contract must never be reported as "unsatisfiable".

Schema (all keys required unless marked optional)
-------------------------------------------------

Top level::

    {
      "schema":        "fixture-contract/1",
      "contract_id":   "<opaque string>",
      "tables":        [ Table, ... ],
      "slots":         [ Slot, ... ],
      "rules":         [ Rule, ... ]
    }

Table::

    {
      "name":        "<id>",
      "kind":        "parent" | "child",
      "fields":      [ {"name": "<id>", "type": "int"|"str", "nullable": bool}, ... ],
      "primary_key": [ "<field>", ... ]          # parents only
    }

Child tables have no primary_key.  Field names are unique per table.  The
field types are ``int`` (JSON integer, not bool) and ``str``.  ``nullable``
says whether JSON ``null`` is a *value* of the field (and so may appear in a
relation row).  A relation row may also *omit* a field; omission is distinct
from null and is handled by the rules below.

Slot::

    {
      "id":  "<id>",
      "table": "<table name>",
      "candidates": [
          {"candidate_id": "<id>", "row": { "<field>": <value or null>, ... }},
          ...
      ]
    }

Each slot selects exactly one of its candidates.  A candidate's ``row`` is
that complete relation row.  Candidate rows are *independently annotated*
templates: choosing a candidate does not constrain any other slot.  Candidate
ids must be unique within a slot; a candidate may not mention an undefined
field.  Slots are on parent tables or on child tables.

Rule (three variants)::

    # uniqueness: the triple <table, key> must be duplicate-free.
    {"id": "<id>", "kind": "unique",
     "table": "<table>", "key": ["<field>", ...]}

    # foreign key: every selected row of `child` has a reference over
    # `child_fields` either wholly null or wholly non-null; when non-null it
    # must equal the `parent_key` of some selected parent row.
    {"id": "<id>", "kind": "fk",
     "child": "<table>", "child_fields": ["<field>", ...],
     "parent": "<table>", "parent_key": ["<field>", ...]}

    # aggregate: for each selected parent row p (keyed by `parent_key`)
    # compute sum over selected child rows whose reference over
    # `child_fields` equals p's key; the sum must equal p's `parent_amount`
    # field.  A child row with a wholly-null reference contributes nowhere.
    # A child row whose reference hits several parents counts once for each.
    {"id": "<id>", "kind": "aggregate",
     "child": "<table>", "child_amount": "<field>",
     "child_fields": ["<field>", ...],
     "parent": "<table>",
     "parent_key": ["<field>", ...],       # or "parent_key_rule": "<unique id>"
     "parent_amount": "<field>",
     "reference_rule": "<fk rule id>",     # optional cross-check
     "parent_key_rule": "<unique rule id>" # optional: declares the key group
    }

``parent_key_rule`` names a *unique* rule on the parent table; its key is
the key group used by the aggregate.  When both ``parent_key`` and
``parent_key_rule`` are given they must agree.  ``reference_rule`` names the
FK rule the aggregate is paired with; child fields and parent key must agree.
At least one of ``parent_key`` / ``parent_key_rule`` must be present.

Semantic conventions codified in the solver
-------------------------------------------

* A *missing field* (absent from a candidate row) never satisfies a key or a
  reference, and an amount that is missing makes the aggregate rule fail.
* A *wholly-null* reference never matches a parent.
* *Duplicate null key values* still collide for uniqueness.  A missing key
  field, on the other hand, is not a key (no collision).
* All rules are evaluated on the *complete product fixture* independently;
  a failure of one rule never short-circuits another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "fixture-contract/1"
TABLE_KINDS = {"parent", "child"}
FIELD_TYPES = {"int", "str"}
RULE_KINDS = {"unique", "fk", "aggregate"}
MISSING = object()  # sentinel for an omitted field; never appears in JSON


class ContractError(ValueError):
    """The contract document is malformed or internally inconsistent."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _value_ok(value: Any, ftype: str) -> bool:
    if value is None:
        return True  # null is allowed at the JSON level; nullable is enforced
    if ftype == "int":
        return _is_int(value)
    return isinstance(value, str)


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class Table:
    name: str
    kind: str
    fields: Tuple[Field, ...]
    primary_key: Tuple[str, ...]

    def field(self, name: str) -> Field:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    @property
    def field_names(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.fields)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    # tuple of (field, value) in table field order; values are JSON scalars or None
    row: Tuple[Tuple[str, Any], ...]

    def get(self, field_name: str) -> Any:
        for name, value in self.row:
            if name == field_name:
                return value
        return MISSING


@dataclass(frozen=True)
class Slot:
    id: str
    table: str
    candidates: Tuple[Candidate, ...]


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    params: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Contract:
    contract_id: str
    tables: Dict[str, Table]
    slots: Tuple[Slot, ...]
    rules: Tuple[Rule, ...]
    raw: Dict[str, Any]

    def table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError:
            raise ContractError(f"undefined table {name!r}") from None

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise ContractError(f"undefined rule {rule_id!r}")

    def slots_for_table(self, table: str) -> Tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.table == table)


# ---------------------------------------------------------------------------
# parsing helpers


def _require(obj: Dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise ContractError(f"{where}: missing required key {key!r}")
    return obj[key]


def _as_list_of_str(value: Any, where: str, *, allow_empty: bool = False) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ContractError(f"{where}: expected a list of strings")
    out: List[str] = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or not item:
            raise ContractError(f"{where}: expected non-empty string entries")
        if item in seen:
            raise ContractError(f"{where}: duplicate entry {item!r}")
        seen.add(item)
        out.append(item)
    if not out and not allow_empty:
        raise ContractError(f"{where}: must not be empty")
    return tuple(out)


def _check_fields_exist(table: Table, names: Sequence[str], where: str) -> None:
    known = set(table.field_names)
    for n in names:
        if n not in known:
            raise ContractError(
                f"{where}: field {n!r} is not defined on table {table.name!r}"
            )


def _check_int_columns(table: Table, names: Sequence[str], where: str) -> None:
    for n in names:
        f = table.field(n)
        if f.type != "int":
            raise ContractError(f"{where}: field {n!r} on {table.name!r} must be type int")


def _parse_row(row: Any, table: Table, where: str) -> Tuple[Tuple[str, Any], ...]:
    if not isinstance(row, dict):
        raise ContractError(f"{where}: candidate row must be an object")
    known = set(table.field_names)
    for key in row:
        if key not in known:
            raise ContractError(f"{where}: undefined field {key!r} on table {table.name!r}")
    items: List[Tuple[str, Any]] = []
    for f in table.fields:
        if f.name in row:
            value = row[f.name]
            if value is None:
                if not f.nullable:
                    raise ContractError(
                        f"{where}: field {f.name!r} is not nullable"
                    )
            elif not _value_ok(value, f.type):
                raise ContractError(
                    f"{where}: field {f.name!r} has wrong type (expected {f.type})"
                )
            items.append((f.name, value))
    return tuple(items)


def _parse_rule(obj: Any, tables: Dict[str, Table], rule_ids: set, where: str) -> Rule:
    if not isinstance(obj, dict):
        raise ContractError(f"{where}: rule must be an object")
    rid = _require(obj, "id", where)
    if not isinstance(rid, str) or not rid:
        raise ContractError(f"{where}: rule id must be a non-empty string")
    if rid in rule_ids:
        raise ContractError(f"duplicate rule id {rid!r}")
    rule_ids.add(rid)
    kind = _require(obj, "kind", where)
    if kind not in RULE_KINDS:
        raise ContractError(f"rule {rid!r}: unknown kind {kind!r}")

    def _common_reference(prefix: str) -> Dict[str, Any]:
        child_name = _require(obj, "child", where)
        parent_name = _require(obj, "parent", where)
        child = tables.get(child_name)
        parent = tables.get(parent_name)
        if child is None:
            raise ContractError(f"rule {rid!r}: undefined child table {child_name!r}")
        if parent is None:
            raise ContractError(f"rule {rid!r}: undefined parent table {parent_name!r}")
        if parent.kind != "parent":
            raise ContractError(f"rule {rid!r}: {parent_name!r} is not a parent table")
        fields = _as_list_of_str(
            _require(obj, "child_fields", where), f"rule {rid!r}.child_fields")
        _check_fields_exist(child, fields, f"rule {rid!r}.child_fields")
        return {"child": child_name, "child_fields": fields, "parent": parent_name}

    if kind == "unique":
        table_name = _require(obj, "table", where)
        table = tables.get(table_name)
        if table is None:
            raise ContractError(f"rule {rid!r}: undefined table {table_name!r}")
        key = _as_list_of_str(_require(obj, "key", where), f"rule {rid!r}.key")
        _check_fields_exist(table, key, f"rule {rid!r}.key")
        params = {"table": table_name, "key": key}
    elif kind == "fk":
        params = _common_reference("fk")
        parent = tables[params["parent"]]
        pkey = _as_list_of_str(
            _require(obj, "parent_key", where), f"rule {rid!r}.parent_key")
        _check_fields_exist(parent, pkey, f"rule {rid!r}.parent_key")
        if len(params["child_fields"]) != len(pkey):
            raise ContractError(f"rule {rid!r}: child_fields and parent_key differ in arity")
        params["parent_key"] = pkey
    else:  # aggregate
        params = _common_reference("aggregate")
        parent = tables[params["parent"]]
        has_key = "parent_key" in obj
        key_rule = obj.get("parent_key_rule")
        if key_rule is not None and not isinstance(key_rule, str):
            raise ContractError(f"rule {rid!r}: parent_key_rule must be a string")
        if not has_key and key_rule is None:
            raise ContractError(
                f"rule {rid!r}: aggregate needs parent_key or parent_key_rule")
        pkey = None
        if has_key:
            pkey = _as_list_of_str(
                obj["parent_key"], f"rule {rid!r}.parent_key")
            _check_fields_exist(parent, pkey, f"rule {rid!r}.parent_key")
            if len(params["child_fields"]) != len(pkey):
                raise ContractError(
                    f"rule {rid!r}: child_fields and parent_key differ in arity")
            params["parent_key"] = pkey
        if key_rule is not None:
            params["parent_key_rule"] = key_rule
        amount = _require(obj, "child_amount", where)
        if not isinstance(amount, str):
            raise ContractError(f"rule {rid!r}: child_amount must be a string")
        _check_fields_exist(tables[params["child"]], [amount],
                            f"rule {rid!r}.child_amount")
        _check_int_columns(tables[params["child"]], [amount],
                           f"rule {rid!r}.child_amount")
        params["child_amount"] = amount
        pamount = _require(obj, "parent_amount", where)
        if not isinstance(pamount, str):
            raise ContractError(f"rule {rid!r}: parent_amount must be a string")
        _check_fields_exist(parent, [pamount], f"rule {rid!r}.parent_amount")
        _check_int_columns(parent, [pamount], f"rule {rid!r}.parent_amount")
        params["parent_amount"] = pamount
        ref = obj.get("reference_rule")
        if ref is not None:
            if not isinstance(ref, str):
                raise ContractError(f"rule {rid!r}: reference_rule must be a string")
            params["reference_rule"] = ref
    return Rule(id=rid, kind=kind, params=params)


def _resolve_rule_references(tables: Dict[str, Table], rules: List[Rule],
                             where: str = "contract") -> List[Rule]:
    """Second pass: resolve rule-to-rule references now that all rules exist."""
    by_id = {r.id: r for r in rules}
    resolved: List[Rule] = []
    for r in rules:
        params = dict(r.params)
        key_rule_id = params.pop("parent_key_rule", None) if r.kind == "aggregate" else None
        if key_rule_id is not None:
            target = by_id.get(key_rule_id)
            if target is None:
                raise ContractError(
                    f"rule {r.id!r}: parent_key_rule points to undefined rule {key_rule_id!r}")
            if target.kind != "unique":
                raise ContractError(
                    f"rule {r.id!r}: parent_key_rule {key_rule_id!r} is not a unique rule")
            if target.params["table"] != params["parent"]:
                raise ContractError(
                    f"rule {r.id!r}: parent_key_rule {key_rule_id!r} is on table "
                    f"{target.params['table']!r}, not {params['parent']!r}")
            if "parent_key" in params and tuple(params["parent_key"]) != tuple(target.params["key"]):
                raise ContractError(
                    f"rule {r.id!r}: parent_key disagrees with parent_key_rule {key_rule_id!r}")
            params["parent_key"] = tuple(target.params["key"])
            params["parent_key_rule"] = key_rule_id
            parent = tables[params["parent"]]
            if len(params["child_fields"]) != len(params["parent_key"]):
                raise ContractError(
                    f"rule {r.id!r}: child_fields and parent_key differ in arity")
        ref = params.get("reference_rule")
        if ref is not None:
            target = by_id.get(ref)
            if target is None:
                raise ContractError(
                    f"rule {r.id!r}: reference_rule points to undefined rule {ref!r}")
            if target.kind != "fk":
                raise ContractError(
                    f"rule {r.id!r}: reference_rule {ref!r} is not an fk rule")
            same = (target.params["child"] == params["child"]
                    and target.params["parent"] == params["parent"]
                    and tuple(target.params["child_fields"]) == tuple(params["child_fields"])
                    and tuple(target.params["parent_key"]) == tuple(params["parent_key"]))
            if not same:
                raise ContractError(
                    f"rule {r.id!r}: reference_rule {ref!r} does not describe "
                    f"the same reference as the aggregate rule")
        resolved.append(Rule(id=r.id, kind=r.kind, params=params))
    return resolved



def parse_contract(doc: Any) -> Contract:
    """Validate a decoded JSON document and return a Contract."""
    if not isinstance(doc, dict):
        raise ContractError("contract must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise ContractError(f"unsupported or missing schema (expected {SCHEMA!r})")
    contract_id = _require(doc, "contract_id", "contract")
    if not isinstance(contract_id, str) or not contract_id:
        raise ContractError("contract_id must be a non-empty string")

    tables: Dict[str, Table] = {}
    raw_tables = _require(doc, "tables", "contract")
    if not isinstance(raw_tables, list):
        raise ContractError("tables must be a list")
    for idx, t in enumerate(raw_tables):
        where = f"tables[{idx}]"
        if not isinstance(t, dict):
            raise ContractError(f"{where}: table must be an object")
        name = _require(t, "name", where)
        if not isinstance(name, str) or not name:
            raise ContractError(f"{where}: invalid table name")
        if name in tables:
            raise ContractError(f"duplicate table name {name!r}")
        kind = _require(t, "kind", where)
        if kind not in TABLE_KINDS:
            raise ContractError(f"table {name!r}: unknown kind {kind!r}")
        raw_fields = _require(t, "fields", where)
        if not isinstance(raw_fields, list) or not raw_fields:
            raise ContractError(f"table {name!r}: fields must be a non-empty list")
        fields: List[Field] = []
        seen_fields = set()
        for fi, f in enumerate(raw_fields):
            fw = f"{where}.fields[{fi}]"
            if not isinstance(f, dict):
                raise ContractError(f"{fw}: field must be an object")
            fname = _require(f, "name", fw)
            if not isinstance(fname, str) or not fname:
                raise ContractError(f"{fw}: invalid field name")
            if fname in seen_fields:
                raise ContractError(f"table {name!r}: duplicate field {fname!r}")
            seen_fields.add(fname)
            ftype = _require(f, "type", fw)
            if ftype not in FIELD_TYPES:
                raise ContractError(f"{fw}: unknown field type {ftype!r}")
            nullable = f.get("nullable", False)
            if not isinstance(nullable, bool):
                raise ContractError(f"{fw}: nullable must be a boolean")
            fields.append(Field(fname, ftype, nullable))
        if kind == "parent":
            pkey = _as_list_of_str(
                _require(t, "primary_key", where), f"table {name!r}.primary_key")
            table = Table(name, kind, tuple(fields), pkey)
            _check_fields_exist(table, pkey, f"table {name!r}.primary_key")
        else:
            if "primary_key" in t:
                raise ContractError(f"child table {name!r} must not declare primary_key")
            table = Table(name, kind, tuple(fields), ())
        tables[name] = table

    slots: List[Slot] = []
    slot_ids = set()
    raw_slots = _require(doc, "slots", "contract")
    if not isinstance(raw_slots, list):
        raise ContractError("slots must be a list")
    for si, s in enumerate(raw_slots):
        where = f"slots[{si}]"
        if not isinstance(s, dict):
            raise ContractError(f"{where}: slot must be an object")
        sid = _require(s, "id", where)
        if not isinstance(sid, str) or not sid:
            raise ContractError(f"{where}: invalid slot id")
        if sid in slot_ids:
            raise ContractError(f"duplicate slot id {sid!r}")
        slot_ids.add(sid)
        table_name = _require(s, "table", where)
        table = tables.get(table_name)
        if table is None:
            raise ContractError(f"slot {sid!r}: undefined table {table_name!r}")
        raw_cands = _require(s, "candidates", where)
        if not isinstance(raw_cands, list) or not raw_cands:
            raise ContractError(f"slot {sid!r}: candidates must be a non-empty list")
        cands: List[Candidate] = []
        cand_ids = set()
        for ci, c in enumerate(raw_cands):
            cw = f"{where}.candidates[{ci}]"
            if not isinstance(c, dict):
                raise ContractError(f"{cw}: candidate must be an object")
            cid = _require(c, "candidate_id", cw)
            if not isinstance(cid, str) or not cid:
                raise ContractError(f"{cw}: invalid candidate_id")
            if cid in cand_ids:
                raise ContractError(f"slot {sid!r}: duplicate candidate_id {cid!r}")
            cand_ids.add(cid)
            row = _parse_row(_require(c, "row", cw), table, cw)
            cands.append(Candidate(cid, row))
        slots.append(Slot(sid, table_name, tuple(cands)))

    rule_ids: set = set()
    raw_rules = _require(doc, "rules", "contract")
    if not isinstance(raw_rules, list):
        raise ContractError("rules must be a list")
    rules: List[Rule] = []
    for ri, r in enumerate(raw_rules):
        rules.append(_parse_rule(r, tables, rule_ids, f"rules[{ri}]"))

    # cross-references between rules (resolved in a second pass)
    rules = _resolve_rule_references(tables, rules)

    return Contract(contract_id=contract_id, tables=tables,
                    slots=tuple(slots), rules=tuple(rules), raw=doc)


def load_contract(path: str) -> Contract:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    return parse_contract(doc)
