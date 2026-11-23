#!/usr/bin/env python3
"""Read a merge pack back: list load rows per table with their provenance.

The pack's ``tables`` section is what the loader consumes: every entry is one
load row plus a ``source`` pointer.  The source pointer is metadata, not a
target-table column; loaders strip it before inserting.

Usage:
    python3 -I examples/merge-demo/read_pack.py PACK.json [TABLE]
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1], encoding="utf-8") as fh:
        pack = json.load(fh)
    if pack.get("schema") != "merge-pack/1":
        print("not a merge-pack/1 document", file=sys.stderr)
        return 2

    only = argv[2] if len(argv) == 3 else None
    for table in sorted(pack["tables"]):
        if only is not None and table != only:
            continue
        print(f"table {table}:")
        for entry in pack["tables"][table]:
            source = entry["source"]
            print(
                f"  row {json.dumps(entry['row'], sort_keys=True)}"
                f"  <- batch={source['batch_id']}"
                f" slot={source['slot_id']}"
                f" candidate={source['candidate_id']}")

    print("\nidentity domains:")
    for domain in pack["domains"]:
        cols = ", ".join(f"{c['table']}.{c['field']}"
                         for c in domain["columns"])
        print(f"  {domain['domain_id']}: {cols}")

    print("\nrenames applied (batch/domain):")
    for item in pack["rewrites"]:
        print(f"  {item['batch_id']} {item['domain_id']}: "
              f"{item['original']!r} -> {item['replacement']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
