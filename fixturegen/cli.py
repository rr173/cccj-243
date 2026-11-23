"""Command line interface for the synthetic fixture generator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from .contract import ContractError, load_contract
from .engine import FixtureRequestError, generate, verify_report
from .material import load_material, replay_material, write_material


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fixturegen.cli",
        description=("Generate synthetic fixtures from a finite, self-contained "
                     "handover contract.  Never reads customer data."),
    )
    parser.add_argument("contract", nargs="?", help="path to the contract JSON")
    parser.add_argument("--isolate", action="append", default=[], metavar="RULE_ID",
                        help="rule identity to isolate (repeatable); the fixture "
                             "must falsify it and keep every other rule true")
    parser.add_argument("--out", default=None, metavar="DIR",
                        help="directory for report.json, fixtures and material "
                             "(default: print the report to stdout)")
    parser.add_argument("--max-nodes", type=int, default=5_000_000,
                        help="per-search node budget (default: 5,000,000)")
    parser.add_argument("--replay-material", default=None, metavar="FILE",
                        help="independently replay an impossibility material file")
    parser.add_argument("--max-assignments", type=int, default=2_000_000,
                        help="naive product bound for --replay-material")
    return parser


def _write_json(path: str, doc) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _fixture_filename(kind: str, target: Optional[str]) -> str:
    return "normal.fixture.json" if kind == "normal" else f"isolation-{target}.fixture.json"


def _no_fixture_filename(kind: str, target: Optional[str]) -> str:
    return ("normal.no-fixture.json" if kind == "normal"
            else f"isolation-{target}.no-fixture.json")


def _material_filename(kind: str, target: Optional[str]) -> str:
    return "normal.material.json" if kind == "normal" else f"isolation-{target}.material.json"


def _run_replay(args: argparse.Namespace) -> int:
    try:
        doc = load_material(args.replay_material)
    except (OSError, ContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    result = replay_material(doc, max_assignments=args.max_assignments)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if result["ok"]:
        return 0
    return 3 if result.get("undetermined") else 1


def run(args: argparse.Namespace) -> int:
    if args.replay_material is not None:
        if args.contract is not None:
            print("error: give either a contract or --replay-material, not both",
                  file=sys.stderr)
            return 2
        return _run_replay(args)
    if args.contract is None:
        print("error: a contract path or --replay-material is required",
              file=sys.stderr)
        return 2

    try:
        contract = load_contract(args.contract)
    except ContractError as exc:
        print(f"error: malformed contract: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot read contract: {exc}", file=sys.stderr)
        return 2

    try:
        report = generate(contract, isolate=args.isolate, max_nodes=args.max_nodes)
    except FixtureRequestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = report.to_json()
    verification = verify_report(contract, payload)
    payload["verification"] = verification

    if not verification["ok"]:
        print("error: generated fixtures failed independent verification",
              file=sys.stderr)
        print(json.dumps(verification, indent=2), file=sys.stderr)
        return 3

    if args.out is None:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    os.makedirs(args.out, exist_ok=True)
    _write_json(os.path.join(args.out, "report.json"), payload)

    entries = [("normal", None, report.normal)]
    entries += [("isolated", rid, entry) for rid, entry in report.isolations.items()]
    for kind, target, entry in entries:
        doc = entry.to_json()
        label = "normal" if kind == "normal" else f"isolation[{target}]"
        if doc["schema"] == "fixture/1":
            _write_json(os.path.join(args.out, _fixture_filename(kind, target)), doc)
            print(f"{label}: fixture written", file=sys.stderr)
        else:
            _write_json(os.path.join(args.out, _no_fixture_filename(kind, target)), doc)
            if doc.get("material"):
                write_material(
                    doc["material"],
                    os.path.join(args.out, _material_filename(kind, target)))
            print(f"{label}: no fixture ({doc['status']}): {doc['reason']}",
                  file=sys.stderr)
    print(f"report: {os.path.join(args.out, 'report.json')}", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
