"""Shared test helpers: small contract builders and independent brute force."""

from __future__ import annotations

import itertools
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fixturegen.contract import Candidate, Contract, parse_contract
from fixturegen.engine import evaluate_rule, evaluate_rules  # noqa: E402


def table(name: str, kind: str, fields: Sequence[Tuple[str, str, bool]],
          primary_key: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "name": name,
        "kind": kind,
        "fields": [{"name": n, "type": t, "nullable": nl} for n, t, nl in fields],
    }
    if primary_key is not None:
        doc["primary_key"] = list(primary_key)
    return doc


def slot(sid: str, table_name: str, candidates: Sequence[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
    return {
        "id": sid,
        "table": table_name,
        "candidates": [{"candidate_id": cid, "row": row} for cid, row in candidates],
    }


def contract(tables, slots, rules, contract_id: str = "t") -> Dict[str, Any]:
    return {
        "schema": "fixture-contract/1",
        "contract_id": contract_id,
        "tables": list(tables),
        "slots": list(slots),
        "rules": list(rules),
    }


# --- independent semantics -------------------------------------------------
#
# A separate, deliberately naive re-implementation used by the tests.  It
# shares no code with the package: it reads the raw JSON document and applies
# the contract's written semantics directly.


def _row_get(row: Mapping[str, Any], name: str):
    return row[name] if name in row else _MISSING


_MISSING = object()


def _key(row, fields):
    values = []
    for f in fields:
        v = _row_get(row, f)
        if v is _MISSING:
            return None
        values.append(v)
    return tuple(values)


def _reference(row, fields):
    values = []
    for f in fields:
        v = _row_get(row, f)
        if v is _MISSING:
            return ("missing", ())
        values.append(v)
    if all(v is not None for v in values):
        return ("value", tuple(values))
    if all(v is None for v in values):
        return ("null", ())
    return ("partial", ())


def _amount(row, field):
    v = _row_get(row, field)
    if v is _MISSING or v is None or isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def naive_rule_truth(doc: Mapping[str, Any], selection: Mapping[str, Mapping[str, Any]],
                     rule: Mapping[str, Any]) -> bool:
    """Independent evaluator over raw JSON.  selection maps slot id -> row."""
    slots_by_table: Dict[str, List[Mapping[str, Any]]] = {}
    for s in doc["slots"]:
        slot_id = s["id"]
        if slot_id not in selection:
            raise ValueError(f"missing slot {slot_id}")
        slots_by_table.setdefault(s["table"], []).append(selection[slot_id])

    kind = rule["kind"]
    if kind == "unique":
        seen = set()
        for row in slots_by_table.get(rule["table"], []):
            key = _key(row, rule["key"])
            if key is None:
                continue
            if key in seen:
                return False
            seen.add(key)
        return True
    if kind == "fk":
        keys = {_key(r, rule["parent_key"])
                for r in slots_by_table.get(rule["parent"], [])}
        keys.discard(None)
        for row in slots_by_table.get(rule["child"], []):
            status, values = _reference(row, rule["child_fields"])
            if status in ("missing", "partial"):
                return False
            if status == "value" and values not in keys:
                return False
        return True
    if kind == "aggregate":
        parents = []
        for row in slots_by_table.get(rule["parent"], []):
            amt = _amount(row, rule["parent_amount"])
            if amt is None:
                return False
            parents.append((_key(row, rule["parent_key"]), amt))
        pkeys = {k for k, _ in parents if k is not None}
        sums: Dict[Tuple[Any, ...], int] = {}
        for row in slots_by_table.get(rule["child"], []):
            status, values = _reference(row, rule["child_fields"])
            if status != "value" or values not in pkeys:
                continue
            amt = _amount(row, rule["child_amount"])
            if amt is None:
                return False
            sums[values] = sums.get(values, 0) + amt
        for key, total in parents:
            if (0 if key is None else sums.get(key, 0)) != total:
                return False
        return True
    raise ValueError(kind)


def naive_all_truth(doc, selection) -> Dict[str, bool]:
    return {r["id"]: naive_rule_truth(doc, selection, r) for r in doc["rules"]}


def naive_order(doc):
    """Slot ids then candidate ids, in the contract's own identity space."""
    out = []
    for s in sorted(doc["slots"], key=lambda s: s["id"]):
        out.append((s["id"], [c["candidate_id"] for c in
                              sorted(s["candidates"], key=lambda c: c["candidate_id"])]))
    return out


def naive_first(doc, must_true: Sequence[str], must_false: Sequence[str] = (),
                limit: int = 2_000_000):
    """Lexicographic first satisfying selection by naive product enumeration."""
    order = naive_order(doc)
    rows = {s["id"]: {c["candidate_id"]: c["row"] for c in s["candidates"]}
            for s in doc["slots"]}
    ids = [sid for sid, _ in order]
    domains = [[(sid, cid) for cid in cids] for sid, cids in order]
    count = 0
    for choice in itertools.product(*domains):
        count += 1
        if count > limit:
            raise AssertionError("naive enumeration limit exceeded")
        selection = {sid: rows[sid][cid] for sid, cid in choice}
        truth = naive_all_truth(doc, selection)
        if all(truth[rid] for rid in must_true) and not any(
                truth[rid] for rid in must_false):
            return {sid: cid for sid, cid in choice}
    return None
