"""Command line access to the historical FK attribution index.

Examples::

    # build the local index for one FK rule (reads the ledger through once)
    python3 -m fixturegen.attribution_cli build \
        --contract examples/billing.json \
        --ledger examples/history-demo/ledger.db \
        --rule F-invoice-client \
        --index examples/history-demo/index.db

    # later, after the ledger received more transactions
    python3 -m fixturegen.attribution_cli refresh \
        --ledger examples/history-demo/ledger.db \
        --index examples/history-demo/index.db

    # what can be queried right now?
    python3 -m fixturegen.attribution_cli status \
        --index examples/history-demo/index.db

    # answer a JSONL batch of {"seq": N, "child_slot_id": "..."} lines
    python3 -m fixturegen.attribution_cli query \
        --index examples/history-demo/index.db \
        examples/history-demo/requests.jsonl

The query side needs only the index file and the list; it never opens the
ledger.  Results are JSONL, one object per input line, in input order; a
problem line is reported in place and never falls back to current values.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from .attribution import (
    AttributionIndex,
    AttributionError,
    build_index,
    index_status,
    refresh_index,
    run_requests,
)
from .contract import ContractError, load_contract
from .ledger import LedgerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fixturegen.attribution_cli",
        description=("Maintain and query a local historical attribution "
                     "index for one FK rule over a ledger/1 database."),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="create the index (reads the ledger "
                                         "through once)")
    build.add_argument("--contract", required=True, help="contract JSON path")
    build.add_argument("--ledger", required=True,
                       help="ledger/1 SQLite database path")
    build.add_argument("--rule", required=True,
                       help="fk rule id to index")
    build.add_argument("--index", required=True,
                       help="index SQLite database to create")

    refresh = sub.add_parser("refresh", help="advance the index to the "
                                             "ledger's current commit point")
    refresh.add_argument("--ledger", required=True,
                         help="ledger/1 SQLite database path")
    refresh.add_argument("--index", required=True,
                         help="index SQLite database path")

    status = sub.add_parser("status", help="print the queryable cutoff and "
                                           "index shape")
    status.add_argument("--index", required=True, help="index SQLite path")

    query = sub.add_parser("query", help="answer a JSONL request batch")
    query.add_argument("--index", required=True, help="index SQLite path")
    query.add_argument("requests", help="JSONL file, or - for stdin")
    return parser


def _print(doc: Any) -> None:
    json.dump(doc, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def _load_contract(path: str):
    try:
        return load_contract(path)
    except ContractError as exc:
        raise AttributionError(f"malformed contract: {exc}") from None
    except OSError as exc:
        raise AttributionError(f"cannot read contract: {exc}") from None


def run(args: argparse.Namespace) -> int:
    try:
        if args.command == "build":
            contract = _load_contract(args.contract)
            _print(build_index(args.index, contract, args.ledger, args.rule))
            return 0

        if args.command == "refresh":
            _print(refresh_index(args.index, args.ledger))
            return 0

        if args.command == "status":
            _print(index_status(args.index))
            return 0

        # query
        if args.requests == "-":
            lines = sys.stdin
        else:
            try:
                lines = open(args.requests, "r", encoding="utf-8")
            except OSError as exc:
                raise AttributionError(
                    f"cannot read requests: {exc}") from None
        try:
            with AttributionIndex(args.index) as index:
                results, errors = run_requests(index, lines)
        finally:
            if args.requests != "-":
                lines.close()
        for result in results:
            sys.stdout.write(json.dumps(
                result, sort_keys=True, ensure_ascii=False,
                separators=(",", ":")) + "\n")
        if errors:
            print(f"error: {errors} of {len(results)} request line(s) "
                  f"could not be answered", file=sys.stderr)
            return 1
        return 0
    except (AttributionError, LedgerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
