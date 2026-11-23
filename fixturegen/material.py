"""Finite, replayable material for "no fixture can be built" conclusions.

When the generator cannot produce a normal fixture or a requested isolation,
the report says so explicitly *and* hands over enough finite material to
re-check the conclusion without trusting the generator.  The material is a set
of self-contained blocks, one per unsatisfiable search component.  Each block
holds

* the exact tables and fields the block's rules mention,
* **every** slot on those tables (all candidates, unchanged),
* **every** rule on those tables (unchanged),
* the claim that must be replayed: rules required true and rules required
  false,

so a replayer can enumerate that block's slot product and observe that the
claim has no model.  A document-level decomposition lemma explains why an
unsatisfiable component entitles us to the global conclusion.  Blocks are not
minimal: keeping all slots and applicable rules is explicitly allowed, and
none of the generator's pruning is assumed by the replay.

Replay is independent of the generator's search: it re-parses each
sub-contract and enumerates the naive product with an explicit bound.  When a
block is too large for naive replay, the replay reports ``undetermined``
rather than pretending to have checked it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .contract import Contract, ContractError, parse_contract
from .engine import (
    Component,
    SearchLimitExceeded,
    enumerate_assignments,
    evaluate_rule,
)

MATERIAL_SCHEMA = "impossibility-material/1"
NAIVE_REPLAY_LIMIT = 2_000_000

_DECOMPOSITION_LEMMA = (
    "For the stated truth requirement, two slots interact only if some rule's "
    "truth can depend on both.  The generator therefore partitions slots into "
    "components (slots linked when they share a rule's influence set); a rule "
    "appears in every component whose slots can affect it, and every slot is "
    "in exactly one component or is free.  Free slots cannot change any rule, "
    "so they take any offered candidate.  Consequently the whole fixture "
    "satisfies the requirement iff every component selection satisfies the "
    "rules listed in its claim.  An unsatisfiable block therefore proves the "
    "global no-fixture conclusion for its truth requirement.  Each block "
    "contains every slot and every rule of its component, unmodified."
)


def _table_doc(table) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "name": table.name,
        "kind": table.kind,
        "fields": [
            {"name": f.name, "type": f.type, "nullable": f.nullable}
            for f in table.fields
        ],
    }
    if table.kind == "parent":
        doc["primary_key"] = list(table.primary_key)
    return doc


def _rule_doc(rule) -> Dict[str, Any]:
    doc: Dict[str, Any] = {"id": rule.id, "kind": rule.kind}
    for key, value in rule.params.items():
        doc[key] = list(value) if isinstance(value, tuple) else value
    return doc


def sub_contract_doc(contract: Contract, component: Component) -> Dict[str, Any]:
    """A standalone contract containing exactly one block."""
    return {
        "schema": "fixture-contract/1",
        "contract_id": f"{contract.contract_id}#{component.key}",
        "tables": [_table_doc(contract.tables[name])
                   for name in sorted(component.tables)],
        "slots": [
            {
                "id": slot.id,
                "table": slot.table,
                "candidates": [
                    {"candidate_id": candidate.candidate_id,
                     "row": dict(sorted(candidate.row))}
                    for candidate in sorted(slot.candidates,
                                            key=lambda c: c.candidate_id)
                ],
            }
            for slot in sorted(component.slots, key=lambda s: s.id)
        ],
        "rules": [_rule_doc(r) for r in sorted(component.rules, key=lambda r: r.id)],
    }


def build_material(contract: Contract,
                   claim_specs: Sequence[Any],
                   conclusion: str,
                   solver_stats: Mapping[str, Any]) -> Dict[str, Any]:
    """Assemble impossibility material.

    ``claim_specs`` is a sequence of ``(component, must_true, must_false)``.
    """
    blocks: List[Dict[str, Any]] = []
    for component, must_true, must_false in claim_specs:
        must_true = list(must_true)
        must_false = list(must_false)
        sub = sub_contract_doc(contract, component)
        parse_contract(sub)  # the sub-contract must itself be a valid contract
        known = {r.id for r in component.rules}
        unknown = sorted((set(must_true) | set(must_false)) - known)
        if unknown:
            raise AssertionError(
                f"internal error: claim references rules outside block: {unknown}")
        if set(must_true) & set(must_false):
            raise AssertionError(
                "internal error: claim requires a rule both true and false")
        blocks.append({
            "id": component.key,
            "tables": list(component.tables),
            "claim": {
                "must_true": must_true,
                "must_false": must_false,
                "statement": (
                    "no selection of this block's slots satisfies every "
                    "must_true rule and violates every must_false rule"),
            },
            "sub_contract": sub,
            "search": {
                "method": ("depth-first over slots in slot-id order and "
                           "candidates in candidate-id order; exhaustive "
                           "unless the node budget was hit"),
                "seat": "block information; the naive replayer ignores it",
                "slot_count": len(component.slots),
                "product_size": component.product_size,
            },
        })

    doc = {
        "schema": MATERIAL_SCHEMA,
        "contract_id": contract.contract_id,
        "generated_by": "fixturegen",
        "conclusion": conclusion,
        "decomposition_lemma": _DECOMPOSITION_LEMMA,
        "blocks": blocks,
        "replay": "python3 -m fixturegen.material <this-file>",
    }

    # Self-check every block with the independent naive enumerator.  Blocks
    # too large to enumerate inside the generator are emitted anyway: the
    # material is still a complete, finite statement of the claim, and a
    # replayer may use any sound solver.  We never fail generation merely
    # because the naive check is too slow.
    for block in blocks:
        result = replay_block(block, max_assignments=NAIVE_REPLAY_LIMIT)
        if not result["ok"] and not result.get("undetermined"):
            raise AssertionError(
                f"internal error: emitted material does not replay: {result}")
    return doc


def write_material(doc: Mapping[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def load_material(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != MATERIAL_SCHEMA:
        raise ContractError(
            f"{path}: not impossibility material ({MATERIAL_SCHEMA!r})")
    return doc


def replay_block(block: Mapping[str, Any],
                 max_assignments: int = NAIVE_REPLAY_LIMIT) -> Dict[str, Any]:
    """Independently replay one block by naive enumeration."""
    errors: List[str] = []
    try:
        sub = parse_contract(block["sub_contract"])
    except (KeyError, ContractError) as exc:
        return {"ok": False, "errors": [f"block {block.get('id')!r}: {exc}"]}
    claim = block.get("claim") or {}
    must_true = claim.get("must_true") or []
    must_false = claim.get("must_false") or []
    known = {r.id for r in sub.rules}
    for rid in list(must_true) + list(must_false):
        if rid not in known:
            errors.append(
                f"block {block.get('id')!r}: claim names unknown rule {rid!r}")
    if errors:
        return {"ok": False, "errors": errors}

    product = 1
    for slot in sub.slots:
        product *= len(slot.candidates)
    if product > max_assignments:
        return {
            "ok": False,
            "undetermined": True,
            "errors": [f"block {block.get('id')!r}: product {product} exceeds "
                       f"replay bound {max_assignments}"],
        }

    model = None
    checked = 0
    try:
        for assignment in enumerate_assignments(sub, max_assignments=max_assignments):
            checked += 1
            if all(evaluate_rule(sub, sub.rule(rid), assignment)
                   for rid in must_true) and not any(
                       evaluate_rule(sub, sub.rule(rid), assignment)
                       for rid in must_false):
                model = assignment
                break
    except SearchLimitExceeded as exc:
        return {"ok": False, "undetermined": True,
                "errors": [f"block {block.get('id')!r}: {exc}"]}

    if model is not None:
        selection = {sid: c.candidate_id for sid, c in model.items()}
        errors.append(
            f"block {block.get('id')!r}: claim is false; counterexample selection "
            f"{selection}")
    return {
        "ok": not errors,
        "errors": errors,
        "block": block.get("id"),
        "assignments_checked": checked,
        "product_size": product,
    }


def replay_material(doc: Mapping[str, Any],
                    max_assignments: int = NAIVE_REPLAY_LIMIT) -> Dict[str, Any]:
    """Replay every block of a material document."""
    if not isinstance(doc, dict) or doc.get("schema") != MATERIAL_SCHEMA:
        return {"ok": False, "errors": ["not impossibility material"]}
    blocks = doc.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return {"ok": False, "errors": ["material has no blocks"]}
    results = []
    ok = True
    undetermined = False
    for block in blocks:
        result = replay_block(block, max_assignments=max_assignments)
        results.append(result)
        ok = ok and result["ok"]
        undetermined = undetermined or bool(result.get("undetermined"))
    return {"ok": ok, "undetermined": undetermined, "blocks": results,
            "conclusion": doc.get("conclusion")}


def main(argv: Optional[Sequence[str]] = None) -> int:
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python3 -m fixturegen.material <material.json>",
              file=sys.stderr)
        return 2
    try:
        doc = load_material(args[0])
    except (OSError, ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = replay_material(doc)
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["ok"]:
        return 0
    return 3 if result.get("undetermined") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
