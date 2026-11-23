"""Generate the checked-in history-demo artifacts and print the walkthrough.

The scenario: one synthetic fact — the invoice in slot ``S04-invoice``,
whose ``client_ref`` is 1 and never changes — is attributed to a parent
CLIENT row through the FK rule ``F-invoice-client`` at different ledger
sequences.  The parent side alone moves:

* seq 0 (initial fixture): ``S01-client`` carries ``client_id`` 1
  (candidate ``c1``, region "north") — the fact attaches to that row;
* seq 1: ``S02-client`` switches to candidate ``d2``, which *also* carries
  ``client_id`` 1 — two parent rows now carry the key, so the reference is
  ambiguous (the ledger records ``U-client-id`` false here, and the FK rule
  itself is *true*; neither fact changes the attribution analysis);
* seq 2: ``S01-client`` moves to ``c2`` (``client_id`` 2) — the key lands
  solely on ``S02-client``/``d2`` (region "east"): the same fact now
  attaches to a *different* parent row.

A report that re-joins with the ledger's *current* values would silently
rewrite the seq-0 attribution.  The attribution index answers each point in
time from its own local data instead.

The script initialises a fresh ledger, applies the first two transactions,
builds the index (cutoff 2), shows that seq 3 is not queryable yet, applies
two more transactions, refreshes (cutoff 4, then idempotently again) and
finally answers ``requests.jsonl`` into ``answers.jsonl``.

Run from the repository root:

    python3 -I examples/history-demo/build_demo.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from fixturegen.attribution import (  # noqa: E402
    AttributionIndex,
    build_index,
    refresh_index,
    run_requests,
)
from fixturegen.contract import load_contract  # noqa: E402
from fixturegen.engine import generate  # noqa: E402
from fixturegen.ledger import init_ledger, open_ledger  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RULE = "F-invoice-client"
FACT = "S04-invoice"

TRANSACTIONS = [
    # S02-client switches to d2: it now also carries client_id 1.
    {"seq": 1, "changes": [{"slot_id": "S02-client", "candidate_id": "d2"}]},
    # S01-client leaves client_id 1: the key moves wholly to S02-client.
    {"seq": 2, "changes": [{"slot_id": "S01-client", "candidate_id": "c2"}]},
    # The fact itself is rewritten to reference client 9 (no such parent).
    {"seq": 3, "changes": [{"slot_id": "S04-invoice", "candidate_id": "i5"}]},
    # ... and back to client 1, which now lives on S02-client/d2.
    {"seq": 4, "changes": [{"slot_id": "S04-invoice", "candidate_id": "i4"}]},
]

REQUESTS = [
    {"seq": 2, "child_slot_id": FACT},   # the same fact ...
    {"seq": 0, "child_slot_id": FACT},   # ... at an earlier ...
    {"seq": 1, "child_slot_id": FACT},   # ... and in-between point in time
    {"seq": 3, "child_slot_id": FACT},   # rewritten to reference client 9
    {"seq": 4, "child_slot_id": FACT},   # back to client 1 (now on S02)
    {"seq": 0, "child_slot_id": "S05-invoice"},  # a wholly-null reference
    {"seq": 0, "child_slot_id": FACT},   # a duplicate request
    {"seq": 9, "child_slot_id": FACT},   # beyond the indexed cutoff
    {"seq": 1, "child_slot_id": "S01-client"},   # not a child slot of the rule
]


def write_jsonl(name: str, docs) -> None:
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
        for doc in docs:
            fh.write(json.dumps(doc, sort_keys=True) + "\n")


def main() -> int:
    contract = load_contract(os.path.join(ROOT, "examples", "billing.json"))
    fixture = generate(contract).normal.to_json()

    ledger_path = os.path.join(HERE, "ledger.db")
    index_path = os.path.join(HERE, "index.db")
    for path in (ledger_path, index_path):
        if os.path.exists(path):
            os.unlink(path)

    write_jsonl("transactions.jsonl", TRANSACTIONS)
    write_jsonl("requests.jsonl", REQUESTS)

    ledger = init_ledger(ledger_path, contract, fixture)
    for tx in TRANSACTIONS[:2]:
        ledger.apply_payload(tx)
    ledger.close()

    built = build_index(index_path, contract, ledger_path, RULE)
    print(f"index built: queryable cutoff seq = {built['indexed_seq']}")

    with AttributionIndex(index_path) as index:
        try:
            index.answer(3, FACT)
        except Exception as exc:
            print(f"seq 3 before refresh: error: {exc}")
        at_zero = index.answer(0, FACT)

    ledger = open_ledger(ledger_path, contract)
    for tx in TRANSACTIONS[2:]:
        ledger.apply_payload(tx)
    ledger.close()

    moved = refresh_index(index_path, ledger_path)
    print(f"refresh: cutoff advanced to seq = {moved['indexed_seq']}")
    again = refresh_index(index_path, ledger_path)
    print(f"refresh again: refreshed={again['refreshed']} "
          f"(same prefix, nothing changed)")

    with open(os.path.join(HERE, "requests.jsonl"), encoding="utf-8") as fh:
        with AttributionIndex(index_path) as index:
            results, errors = run_requests(index, fh)
    write_jsonl("answers.jsonl", results)

    print("\nthe same fact (S04-invoice, client_ref = 1) over time:")
    for result in results[:5]:
        seq = result["seq"]
        status = result["status"]
        if status == "matched":
            row = result["parent_row"]
            print(f"  seq {seq}: matched {result['parent_slot_id']}/"
                  f"{result['parent_candidate_id']} "
                  f"(client_id={row['client_id']}, region={row['region']!r})")
        elif status == "ambiguous":
            print(f"  seq {seq}: ambiguous — {result['parent_matches']} "
                  f"parent rows carry the key (none is picked)")
        else:
            print(f"  seq {seq}: {status}")
    print(f"\n{len(results)} request line(s) answered, {errors} error(s); "
          f"see answers.jsonl")
    assert at_zero["parent_slot_id"] == "S01-client"
    assert results[0]["parent_slot_id"] == "S02-client"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
