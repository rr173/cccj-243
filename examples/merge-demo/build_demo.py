"""Generate the checked-in merge demo artifacts.

It builds two normal, independently verified fixtures for the same contract
(batch B1 and batch B2, deliberately sharing the hot codes X/Y/N1 so naive
concatenation would collide), the three whitelist variants and the merged
pack.  The failing merges are exercised but, per the no-trace-on-failure
guarantee, never write a pack; their witnesses are printed instead.

Run from the repository root:

    python3 -I examples/merge-demo/build_demo.py
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from fixturegen.contract import parse_contract  # noqa: E402
from fixturegen.engine import evaluate_rules, verify_fixture  # noqa: E402
from fixturegen.merge import (  # noqa: E402
    MergeConflictError,
    merge_fixtures,
    verify_pack,
    write_pack_atomic,
)

HERE = os.path.dirname(os.path.abspath(__file__))

B1 = {
    "S01-settle": "k1", "S02-settle": "k1",
    "S03-report": "k1", "S04-report": "k1",
    "S05-detail": "k1", "S06-detail": "k1",
    "S07-note": "k1", "S08-note": "k1",
}
# B2 re-picks the same hot codes X and Y and the note N1, which is exactly
# what breaks naive concatenation.
B2 = {
    "S01-settle": "k1", "S02-settle": "k1",
    "S03-report": "k1", "S04-report": "k1",
    "S05-detail": "k1", "S06-detail": "k1",
    "S07-note": "k1", "S08-note": "k2",
}

SUCCESS_FIELDS = [
    {"table": "SETTLEMENT", "field": "settle_code"},
    {"table": "REPORT", "field": "report_code"},
    {"table": "DETAIL", "field": "detail_code"},
    {"table": "NOTE", "field": "note_code"},
    {"table": "DETAIL", "field": "note_ref"},
]


def write_json(name: str, doc) -> None:
    with open(os.path.join(HERE, name), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")


def fixture_doc(contract, selection_ids: dict) -> dict:
    assignment = {}
    selection = {}
    for slot in contract.slots:
        candidate = next(c for c in slot.candidates
                         if c.candidate_id == selection_ids[slot.id])
        assignment[slot.id] = candidate
        selection[slot.id] = {
            "candidate_id": candidate.candidate_id,
            "row": dict(candidate.row),
        }
    doc = {
        "schema": "fixture/1",
        "kind": "normal",
        "target": None,
        "slots": selection,
        "rules": evaluate_rules(contract, assignment),
    }
    assert verify_fixture(contract, doc)["ok"]
    return doc


def main() -> int:
    with open(os.path.join(HERE, "contract.json"), encoding="utf-8") as fh:
        contract = parse_contract(json.load(fh))

    b1 = fixture_doc(contract, B1)
    b2 = fixture_doc(contract, B2)
    write_json("batch-B1.fixture.json", b1)
    write_json("batch-B2.fixture.json", b2)

    success = {"schema": "rewrite-whitelist/1", "fields": SUCCESS_FIELDS}
    write_json("whitelist-success.json", success)
    # Omitting the child column of the transitive domain locks X/Y in that
    # whole batch-domain, so the parent-side columns cannot be renamed either.
    write_json("whitelist-fails-domain-locked.json", {
        "schema": "rewrite-whitelist/1",
        "fields": [f for f in SUCCESS_FIELDS
                   if f["field"] != "detail_code"],
    })
    # Omitting just one parent column locks its original value in the domain;
    # the cross-batch U-settle collision then cannot be broken.
    write_json("whitelist-fails-column-locked.json", {
        "schema": "rewrite-whitelist/1",
        "fields": [f for f in SUCCESS_FIELDS
                   if f["field"] != "settle_code"],
    })

    pack = merge_fixtures(
        contract, [("B1", b1), ("B2", b2)], success)
    audit = verify_pack(contract, pack)
    assert audit["ok"], audit
    write_pack_atomic(pack, os.path.join(HERE, "merged-pack.json"))
    print("wrote merged-pack.json")

    for name in ("whitelist-fails-domain-locked.json",
                 "whitelist-fails-column-locked.json"):
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            whitelist = json.load(fh)
        try:
            merge_fixtures(contract, [("B1", b1), ("B2", b2)], whitelist)
        except MergeConflictError as exc:
            print(f"\n{name}: merge impossible (no pack written); first "
                  f"witness per rule:")
            print(json.dumps(exc.witnesses, indent=2, sort_keys=True))
        else:
            raise AssertionError(f"{name} unexpectedly merged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
