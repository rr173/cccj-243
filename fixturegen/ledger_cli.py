"""Command line access to the transaction-quality ledger.

Examples::

    # initialize from the verified normal fixture (sequence starts at 0)
    python3 -m fixturegen.ledger_cli init \
        --contract examples/billing.json \
        --fixture out/billing/normal.fixture.json \
        --db out/billing/ledger.db

    # apply JSONL transactions (one transaction per line)
    python3 -m fixturegen.ledger_cli apply \
        --contract examples/billing.json \
        --db out/billing/ledger.db \
        examples/ledger-transactions.jsonl

    # current sequence, selection and rule truth
    python3 -m fixturegen.ledger_cli status \
        --contract examples/billing.json --db out/billing/ledger.db

    # recorded flow, ordered by sequence
    python3 -m fixturegen.ledger_cli flow \
        --contract examples/billing.json --db out/billing/ledger.db

    # reopen with a full audit of the recorded flow and digest chain
    python3 -m fixturegen.ledger_cli verify \
        --contract examples/billing.json --db out/billing/ledger.db
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Optional, Sequence

from .contract import ContractError, load_contract
from .ledger import LedgerError, init_ledger, load_fixture, open_ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m fixturegen.ledger_cli",
        description=("Maintain an append-only transaction-quality ledger over "
                     "one verified normal fixture.  Synthetic candidates only."),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--contract", required=True, help="contract JSON path")
        p.add_argument("--db", required=True, help="ledger SQLite database path")

    init = sub.add_parser("init", help="create an empty ledger (seq 0)")
    common(init)
    init.add_argument("--fixture", required=True,
                      help="verified normal fixture JSON path")

    apply = sub.add_parser("apply", help="apply JSONL transactions; stop at the "
                                         "first bad line")
    common(apply)
    apply.add_argument("transactions", help="JSONL file, or - for stdin")
    apply.add_argument("--verify-history", action="store_true",
                       help="audit the whole recorded flow and digest chain")

    status = sub.add_parser("status", help="print sequence, selection and rules")
    common(status)

    flow = sub.add_parser("flow", help="print the flow ordered by sequence")
    common(flow)

    verify = sub.add_parser("verify", help="reopen and audit the recorded flow")
    common(verify)

    backfill = sub.add_parser(
        "backfill", help="add or extend the per-sequence digest metadata "
                         "(one-time, additive; for ledgers created before "
                         "flow_digest existed)")
    common(backfill)
    return parser


def _load(args: argparse.Namespace):
    try:
        contract = load_contract(args.contract)
    except ContractError as exc:
        raise LedgerError(f"malformed contract: {exc}") from None
    except OSError as exc:
        raise LedgerError(f"cannot read contract: {exc}") from None
    return contract


def _print(doc: Any) -> None:
    json.dump(doc, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")


def run(args: argparse.Namespace) -> int:
    try:
        contract = _load(args)
        if args.command == "init":
            fixture = load_fixture(args.fixture)
            ledger = init_ledger(args.db, contract, fixture)
            with ledger:
                _print({"ok": True, "created": args.db,
                        "status": ledger.status()})
            return 0

        if args.command == "verify":
            ledger = open_ledger(args.db, contract, verify_history=True)
            with ledger:
                _print({"ok": True, "status": ledger.status(),
                        "transactions": len(ledger.flow())})
            return 0

        if args.command == "backfill":
            ledger = open_ledger(args.db, contract)
            with ledger:
                added = ledger.backfill_flow_digests()
                _print({"ok": True, "seq": ledger.current_seq,
                        "digests_added": added})
            return 0

        ledger = open_ledger(args.db, contract,
                             verify_history=getattr(args, "verify_history",
                                                    False))
        with ledger:
            if args.command == "status":
                _print(ledger.status())
                return 0
            if args.command == "flow":
                _print({"seq": ledger.current_seq,
                        "flow": [entry.to_dict() for entry in ledger.flow()]})
                return 0

            if args.transactions == "-":
                lines = sys.stdin
                source = "<stdin>"
            else:
                try:
                    lines = open(args.transactions, "r", encoding="utf-8")
                except OSError as exc:
                    raise LedgerError(
                        f"cannot read transactions: {exc}") from None
                source = args.transactions
            try:
                result = ledger.replay(lines, source=source)
            finally:
                if args.transactions != "-":
                    lines.close()
            doc = result.to_dict()
            doc["status"] = ledger.status()
            _print(doc)
            return 0 if result.ok else 1
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
