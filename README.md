# Synthetic fixture generation for a narrow handover contract

This package turns a **finite declarative contract** into directly loadable
synthetic fixtures.  It never reads, writes or derives customer data: the only
input is the contract JSON, and the only values in fixtures are the
candidate rows the contract declares.

The contract family is deliberately narrow:

* parent tables and child tables;
* a fixed set of **row-identity slots**, each selecting exactly one candidate
  row from an independently annotated list (a candidate is a complete
  relation row with explicit scalars, explicit nulls and possibly absent
  fields);
* exactly three kinds of rule — composite-key **uniqueness**, composite
  **foreign key**, and **signed-integer aggregate** reconciliation.

## Quick start

```bash
python3 -m fixturegen.cli examples/billing.json \
    --isolate F-invoice-client --isolate A-client-billed \
    --out out/billing
```

This writes `out/billing/report.json` containing

* `normal` — a fixture (or a `no-fixture` entry),
* `isolations` — one entry per requested rule identity,
* `stats` — slot/component/slot-product and search-node counts.

Every fixture stores **only** the selected candidate row per slot and the
verified rule truth table:

```json
{
  "schema": "fixture/1",
  "kind": "normal",
  "target": null,
  "slots": {"S01-client": {"candidate_id": "c1", "row": {...}}, ...},
  "rules": {"U-client-id": true, "F-invoice-client": true, ...}
}
```

Fixtures are verified by re-parsing their rows and evaluating every rule
independently (`verify_fixture`/`verify_report`, and the CLI).  A fixture
whose stated `rules` table disagrees with independent evaluation is rejected.

## Contract format

See the module docstring of `fixturegen/contract.py` for the full schema.
The essentials:

* `tables` — fields with `type: "int"|"str"` and `nullable`; parents declare
  `primary_key`.
* `slots` — `id`, `table`, `candidates[{candidate_id,row}]`.  Candidate
  ids are unique within a slot.  Candidate rows may omit fields; an omitted
  field is distinct from a JSON `null`.
* `rules` — one of

  ```json
  {"id": "U...", "kind": "unique", "table": "T", "key": ["f1", "f2"]}
  {"id": "F...", "kind": "fk", "child": "C", "child_fields": ["a"],
   "parent": "P", "parent_key": ["p"]}
  {"id": "A...", "kind": "aggregate", "child": "C", "child_fields": ["a"],
   "child_amount": "amt", "parent": "P",
   "parent_key": ["p"], "parent_key_rule": "U...",
   "parent_amount": "total", "reference_rule": "F..."}
  ```

  An aggregate names the key group it sums against either directly
  (`parent_key`) or through `parent_key_rule` (a unique rule); `reference_rule`
  optionally ties it to the FK rule it complements.  Both cross-references
  are validated — a dangling or mismatched reference is a **bad contract**,
  never an unsatisfiable one.

## Semantics (the part that matters for isolation)

* **Uniqueness.** Rows with all key fields present form keys.  JSON `null`
  is an ordinary key value: duplicate null keys collide.  A row that omits a
  key field has no key and cannot collide.
* **Foreign key.** A reference is either *wholly null* (no reference), or
  *wholly present and non-null* (must equal some selected parent key, in order
  of the composite key).  Partial nulls and missing fields fail the rule.
* **Aggregate.** Every selected child amount and every selected parent amount
  must be a present JSON integer, otherwise the rule fails.  For each parent
  row the matching children are summed (no match ⇒ 0) and compared with the
  parent amount.  Children with a wholly-null (or partial/missing) reference
  contribute to no parent; a child that matches several parent rows (possible
  once a uniqueness rule is relaxed) contributes once to each.
* **Independent evaluation.** Every rule is evaluated on the complete
  fixture; no rule is skipped because another failed.  Multiple rules may
  mention the same table and composite-key group.

## Generation guarantee

The generator returns the **lexicographically smallest selection** for every
solved component, where ordering is by slot id, then by candidate id within
each slot.  Input order in the JSON is irrelevant (fixtures are emitted in
canonical slot order).  Proof sketch: constraints decompose across connected
components, and depth-first search visits slots and candidates in that order,
so the first solution is the component minimum; combining per-component
minima yields the global minimum.

Only requested rules are solved for isolation; each isolation is solved as
*target false, every other rule true*.  A rule that no candidate row can
influence is reported as falsifiable-in-no-way rather than silently skipped.

### No fixture is an explicit outcome

If all rules true is impossible, `normal` is a `no-fixture` entry with
`status: "proved"` and **impossibility material**.  Likewise, if a requested
rule cannot be falsified while every other rule is true (typically because it
is *implied* by them), the isolation entry is a `no-fixture` with its own
material.  The material contains, per unsatisfiable search component, a
standalone sub-contract with **every slot and rule of that component** (not a
minimised core) plus the exact claim (`must_true`, `must_false`).  It can be
replayed without the generator:

```bash
python3 -m fixturegen.material out/billing/isolation-U-client-id.material.json
# or
python3 -m fixturegen.cli --replay-material out/billing/isolation-U-client-id.material.json
```

`status: "undetermined"` (search budget exhausted) is reported separately; it
is never presented as a proof.  `verify_report` replays proved no-fixture
claims, so a `report.json` is only accepted when both its fixtures *and* its
impossibility claims check out.

## Scaling behaviour

The generator never materialises the cross-slot product.  For each truth
requirement it computes per-rule **influence sets** (slots whose row can
change that rule's truth).  Slots outside every influence set are *free* and
take their smallest admissible candidate independently.  The remaining slots
form components, linked when they share a rule's influence set; each
component is searched separately with unary filtering and partial soundness
pruning.  `stats.component_product_sizes` shows the per-search product (the
largest thing searched), never the global product — 200 unrelated slots cost
linear work, not `5**200`.

## Layout

```
fixturegen/contract.py   strict parsing + semantic validation
fixturegen/engine.py     rule evaluation, component planning, search, verification
fixturegen/material.py   impossibility material, sub-contracts, naive replayer
fixturegen/cli.py        command line entry point
examples/                example contract and generated walkthrough
tests/                   unit + independent small-enumeration tests
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite includes independent brute-force enumeration over small slot spaces
to cross-check normal fixtures, isolations, lexicographic tie-breaking,
impossibility conclusions and material replay.  It also covers the two
pedagogical cases requested: breaking one uniqueness rule without touching FK
or aggregate rules, and a rule implied by others that cannot be isolated.
