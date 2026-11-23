"""Command line entry for the one-shot multi-batch fixture merge."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contract import ContractError, load_contract
from .merge import (
    MergeConflictError,
    MergeInputError,
    load_fixture_doc,
    merge_fixtures,
    verify_pack,
    write_pack_atomic,
)


def _write_json(path: str, doc) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fixturegen.merge_cli",
        description=("Merge several independently verified fixtures of one "
                     "contract into one loadable, cross-batch-safe data pack.  "
                     "Never reads customer data."),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a merge pack")
    build.add_argument("contract", help="path to the contract JSON")
    build.add_argument("--whitelist", required=True,
                       help="rewrite whitelist JSON (table/field list)")
    build.add_argument("--batch", action="append", default=[],
                       metavar="BATCH_ID=FIXTURE.json",
                       help="a batch id and its fixture/1 document "
                            "(repeatable, at least twice)")
    build.add_argument("--out", default=None,
                       help="output pack path (default: print to stdout)")

    verify = sub.add_parser("verify", help="independently audit a merge pack")
    verify.add_argument("contract", help="path to the contract JSON")
    verify.add_argument("pack", help="path to the merge pack JSON")
    return parser


def _parse_batch(spec: str) -> Tuple[str, str]:
    if "=" not in spec:
        raise MergeInputError(
            f"--batch expects BATCH_ID=PATH, got {spec!r}")
    batch_id, path = spec.split("=", 1)
    if not batch_id:
        raise MergeInputError(f"--batch {spec!r}: empty batch id")
    return batch_id, path


def _run_build(args: argparse.Namespace) -> int:
    if len(args.batch) < 2:
        print("error: give at least two --batch entries", file=sys.stderr)
        return 2
    try:
        contract = load_contract(args.contract)
        whitelist_doc = _read_json(args.whitelist)
        fixtures: List[Tuple[str, Dict[str, Any]]] = []
        for spec in args.batch:
            batch_id, path = _parse_batch(spec)
            fixtures.append((batch_id, load_fixture_doc(path)))
        pack = merge_fixtures(contract, fixtures, whitelist_doc)
        audit = verify_pack(contract, pack)
        if not audit["ok"]:
            print("error: built pack failed independent audit: "
                  + "; ".join(audit["errors"]), file=sys.stderr)
            return 3
    except MergeConflictError as exc:
        print("error: merge impossible; unresolvable cross-batch conflict(s):",
              file=sys.stderr)
        print(json.dumps(exc.witnesses, indent=2, sort_keys=True),
              file=sys.stderr)
        return 1
    except (MergeInputError, ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out is None:
        json.dump(pack, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    try:
        write_pack_atomic(pack, args.out)
    except OSError as exc:
        print(f"error: cannot write pack: {exc}", file=sys.stderr)
        return 2
    print(f"pack: {args.out}", file=sys.stderr)
    return 0


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            raise MergeInputError(f"invalid JSON in {path}: {exc}") from exc


def _run_verify(args: argparse.Namespace) -> int:
    try:
        contract = load_contract(args.contract)
        pack = _read_json(args.pack)
        result = verify_pack(contract, pack)
    except (MergeInputError, ContractError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["ok"] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "build":
        return _run_build(args)
    return _run_verify(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
