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

## Transaction-quality ledger

`fixturegen.ledger` turns one verified normal fixture into an append-only,
reopenable ledger of synthetic data changes.  It never reads real data: the
only accepted values are candidate rows the contract already declares, and
slots/contract are fixed at initialization.

A JSONL transaction selects a candidate for one or more slots:

```json
{"seq": 1, "changes": [{"slot_id": "S01-client", "candidate_id": "c2"}]}
```

`seq` is a contiguous positive integer; every line must select at least one
slot, each slot at most once, and only a candidate declared for that slot.
Adding/removing slots or changing the contract is unsupported.  True/false is
decided on the **whole selection after the transaction**, with the semantics of
`evaluate_rules`; intermediate states inside a transaction are never recorded.
A false rule is recorded, never a rejection reason.

```bash
python3 -m fixturegen.ledger_cli init \
    --contract examples/billing.json \
    --fixture out/billing/normal.fixture.json \
    --db examples/ledger-demo/ledger.db

python3 -m fixturegen.ledger_cli apply \
    --contract examples/billing.json --db examples/ledger-demo/ledger.db \
    examples/ledger-transactions.jsonl

python3 -m fixturegen.ledger_cli status \
    --contract examples/billing.json --db examples/ledger-demo/ledger.db

python3 -m fixturegen.ledger_cli flow \
    --contract examples/billing.json --db examples/ledger-demo/ledger.db

# reopen and audit the whole recorded flow and digest chain
python3 -m fixturegen.ledger_cli verify \
    --contract examples/billing.json --db examples/ledger-demo/ledger.db
```

`status` prints the current sequence, the slot→candidate selection and each
rule's truth.  `flow` prints, ordered by sequence, the submitted changes plus
`newly_false` and `recovered`, both sorted by rule id.  The checked-in
`examples/ledger-transactions.jsonl` is a small synthetic replay of six
transactions:

| seq | change | newly false | recovered |
|-----|--------|-------------|-----------|
| 1 | `S03-branch` → `b1` | `F-invoice-branch` | |
| 2 | `S01-client` → `c2` | `F-invoice-client` | |
| 3 | `S01-client` → `c1` | | `F-invoice-client` |
| 4 | `S03-branch` → `b2` | | `F-invoice-branch` |
| 5 | `S04-invoice` → `i2` | `A-client-billed` | |
| 6 | `S04-invoice` → `i4` | | `A-client-billed` |

Re-running `apply` on the same file reports six `skipped` transactions and
adds no flow rows.

### Per-sequence digest metadata (additive)

A ledger may additionally carry a `flow_digest` table mapping every
committed sequence to the state digest chained up to that point.  Ledgers
created by this version have it from initialization and `apply` keeps it
current; older databases gain it through a one-time, idempotent, atomic
backfill that recomputes the chain from the recorded flow and stores
nothing unless it reproduces the committed state digest:

```bash
python3 -m fixturegen.ledger_cli backfill \
    --contract examples/billing.json --db examples/ledger-demo/ledger.db
```

The table is derived metadata only: a ledger whose table is missing or
merely lags behind behaves exactly as before under every existing command,
and the backfill repairs it.  Reopening refuses only a table that is
non-contiguous, ahead of the flow, or fully covering yet disagreeing with
the committed state digest; `verify` cross-checks the stored rows against
the audited chain.  The historical attribution index below uses this
metadata to recognise an already-indexed transaction prefix without
re-reading history.

Behaviour guarantees:

* **Verified initialization.** The fixture's selected rows are re-parsed and
  every rule is re-evaluated; a self-reported rule table that disagrees, a row
  that is not the named candidate, or any false rule at sequence 0 is refused.
* **Atomic per-transaction commit.** Slot updates, flow row, sequence and
  digests share one SQLite `BEGIN IMMEDIATE`/`COMMIT`; an interrupted writer
  leaves either the whole transaction or none of it.  Initialization builds a
  temp database and renames it into place.
* **Reopen checks one completed prefix.** Selection, sequence and flow must be
  the contiguous prefix `1..seq`; the selection digest is recomputed and
  compared, and rules are re-derived from the stored selection.  `verify`
  additionally replays deltas and the digest chain.
* **Idempotent re-submission.** A recorded sequence with the same slot→candidate
  mapping is skipped without a new flow row, regardless of JSON formatting or
  `changes` order.  A different mapping, a gap, an unknown slot/candidate, a
  duplicate slot or an invalid value stops the batch and reports the line;
  earlier transactions stay committed.  Resubmitting a batch whose prefix is
  already committed continues from the first new sequence.
* **Incremental, not rescanning.** After initialization a transaction touches
  only the changed slots, the old/new key groups they affect and per-rule
  aggregates (`fixturegen/ledger_rules.py`).  Unchanged children react to a
  parent key or amount change through those indexes, and rule truth computed
  from the indexes is cross-checked against `evaluate_rules` in the tests.
  Reopening rebuilds the indexes from the *current* selection once; it does not
  replay history.  Only a single writer is supported.

## Historical attribution (which parent row *at that time*)

Reports that join synthetic facts to parent rows must not be re-run against
the ledger's *current* values: a fact whose reference landed on parent row A
last week may land on row B today, and rewriting the parent fields with
current values silently rewrites history.  `fixturegen.attribution` builds a
local, self-contained **historical index** from the existing contract and one
`ledger/1` database, for **one selected FK rule** of the contract.  The
report side then needs only the index file and its to-be-linked list — it
never opens the ledger.

```bash
# build once (the only pass that reads the ledger through)
python3 -m fixturegen.attribution_cli build \
    --contract examples/billing.json \
    --ledger examples/history-demo/ledger.db \
    --rule F-invoice-client \
    --index examples/history-demo/index.db

# later, after the ledger received more transactions
python3 -m fixturegen.attribution_cli refresh \
    --ledger examples/history-demo/ledger.db \
    --index examples/history-demo/index.db

# what is queryable right now (cutoff sequence, rule, shape)
python3 -m fixturegen.attribution_cli status --index examples/history-demo/index.db

# answer a batch
python3 -m fixturegen.attribution_cli query \
    --index examples/history-demo/index.db \
    examples/history-demo/requests.jsonl
```

The request list is JSONL, one `{"seq": N, "child_slot_id": "..."}` object
per line, in any order, duplicates allowed.  Sequence 0 is the initial
fixture; any other `seq` is the state after that transaction committed in
full.  The answer is JSONL, one object per input line **in input order**,
echoing the two locating fields plus a `status`:

* `missing` / `partial` / `null` — the reference fields are absent, partly
  null, or wholly null (the `row_reference` classification);
* `unmatched` — a complete non-null reference no selected parent key equals;
* `matched` — exactly one parent slot carries the key at that sequence; the
  answer adds `parent_slot_id`, the `parent_candidate_id` selected *there
  and then*, and the full `parent_row` *at that sequence*;
* `ambiguous` — several parent slots carry the key; the answer adds only
  `parent_matches` (how many).  It never picks one and never lists them.

An answer depends only on the rule's own composite key.  The rule truth the
ledger recorded is never consulted: a sequence whose flow entry lists failed
rules is analysed like any other, and a true FK rule does not turn an
ambiguous reference into a unique one.  A parent slot whose key is unchanged
but whose other fields changed still returns the candidate and row selected
at the requested sequence.  Bad requests — an unknown or non-child slot, a
sequence beyond the indexed cutoff — are reported in place with their line
number and never fall back to current values; the exit code is 1 if any line
failed.

Refresh attaches to the complete commit point seen in one consistent read
and reports the new queryable cutoff.  It **refuses and keeps the previous
index** when the source ledger is shorter, when the contract or the initial
fixture differs, or when the already-indexed transaction prefix differs
(recognised through the ledger's `flow_digest` metadata; ledgers created
before it existed need the one-time `ledger_cli backfill` above).  Re-running
a refresh against an unchanged ledger changes nothing.  The index write is a
single transaction, so after a failed or interrupted refresh a reopened
index serves either the old or the fully refreshed cutoff, and answers at or
below the old cutoff never change.

The index never copies all slots per transaction: it stores one version row
per *changed* involved slot and one key-state row per parent key whose
carrier set changed.  A query is a constant number of indexed lookups — no
flow replay, and no one-by-one scan of same-key parent rows (their count is
maintained by the indexing pass).  A refresh reads only the new flow rows
plus the digest row that locates the old progress — never a replay of
indexed history, never a rescan of the ledger's current slot table.

`examples/history-demo/` contains a checked-in, reproducible walkthrough
(regenerate with `python3 -I examples/history-demo/build_demo.py`): the same
invoice (`S04-invoice`, `client_ref = 1`, never rewritten) attaches to
`S01-client/c1` (region "north") at sequence 0, is ambiguous at sequence 1
(two parents carry the key, `U-client-id` recorded false, the FK rule itself
true), and attaches to `S02-client/d2` (region "east") from sequence 2 —
exactly the re-attribution that re-joining with current values would get
wrong.

## Multi-batch merge (one combined test warehouse)

Several teams can each produce a verified fixture for the *same* contract and
load them together once.  Naive concatenation collides on rule keys and can
attach child rows to another batch's parents; `fixturegen.merge` combines the
fixtures without touching the target schema — no batch column, no dropped
rows, no changed amounts.

Inputs are one contract, at least two `fixture/1` documents (normal or
single-rule isolations) with distinct batch ids, and a rewrite whitelist:

```json
{"schema": "rewrite-whitelist/1",
 "fields": [{"table": "SETTLEMENT", "field": "settle_code"}]}
```

Every fixture is independently re-verified before use; the merged rows come
from the contract candidates that verification matched, never from the
fixture's self-reported rule table.  Only **string-typed columns that
participate in a rule key** may be whitelisted; integers, explicit nulls,
absent fields and all other columns are reproduced verbatim.

```bash
python3 -m fixturegen.merge_cli build examples/merge-demo/contract.json \
  --whitelist examples/merge-demo/whitelist-success.json \
  --batch B1=examples/merge-demo/batch-B1.fixture.json \
  --batch B2=examples/merge-demo/batch-B2.fixture.json \
  --out examples/merge-demo/merged-pack.json
python3 -m fixturegen.merge_cli verify examples/merge-demo/contract.json \
  examples/merge-demo/merged-pack.json
```

### Identity domains and the rewrite discipline

Rule-key columns form **identity domains**: two columns are in the same
domain when a rule compares them at the same composite-key position, or when
they are linked transitively through other rules (a uniqueness-only column is
a domain of its own).  The pack lists every domain and its columns.

Within one batch and one domain, rewriting is injective: the same original
string is replaced identically everywhere (so a detail field connected to
both a settlement code and a report code cannot drift apart), and distinct
originals stay distinct.  An original string that also occurs in a
**non-whitelisted** column of the same batch/domain is *locked*: it is kept
verbatim everywhere in that batch/domain.  Locking one value never locks the
domain's other values.  Every non-locked value receives a replacement that is
globally unique across the pack and disjoint from every original fixture
string; the minting loop simply draws another identifier when one is taken,
so an occupied prefix can never turn an otherwise feasible merge into a
failure.

### What must hold across batches, and how conflicts are reported

The merge rules out (a) equal keys under one uniqueness rule across batches
and (b) any child-to-parent match across batches under an FK or aggregate
rule.  A cross-batch equality can survive rewriting only when *every*
compared position is fixed (null, integer, or a locked string): any free
position carries a globally unique replacement.  The conflict scan therefore
indexes only fully-fixed tuples per rule — linear in the row count, never an
enumeration of cross-batch row pairs, which matters for a hot code repeated
across large fixtures.  On failure the command exits non-zero, writes no file
and leaves existing outputs untouched, reporting at least one checkable
witness per rule with the rule id and both sides' batch ids, slot ids and the
fixed key:

```
error: merge impossible; unresolvable cross-batch conflict(s):
[{"rule_id": "U-settle", "fixed_key": ["X"],
  "a": {"batch_id": "B1", "slot_id": "S01-settle", "table": "SETTLEMENT"},
  "b": {"batch_id": "B2", "slot_id": "S01-settle", "table": "SETTLEMENT"}, ...}]
```

Everything that is not a cross-batch equality is preserved: within-batch rule
truth including existing uniqueness collisions, missing/partial references,
multi-parent hits and aggregate contribution attribution, plus the
absent-vs-null distinction and every integer.  Success is never inferred from
unchanged whole-pack rule truth or cancelling amounts.  The output is
deterministic: swapping the input order yields a byte-equal pack.

### Reading the pack

A pack is organized per target table; each entry is one load row plus a
`source` pointer (metadata, not a target column):

```bash
python3 -I examples/merge-demo/read_pack.py \
  examples/merge-demo/merged-pack.json SETTLEMENT
# row {"settle_code": "fg-merge-0", "settle_total": 10}  <- batch=B1 slot=S01-settle candidate=k1
```

The pack also records the domain list, the per-batch independently computed
rule results and the full rewrite ledger, and `merge_cli verify` re-derives
every row from the named contract candidate plus that ledger and re-runs the
cross-batch scan on the final values.  A checked-in, reproducible walkthrough
lives in `examples/merge-demo/` (regenerate with
`python3 -I examples/merge-demo/build_demo.py`, including two whitelists that
fail with witnesses and write no pack).

## Layout

```
fixturegen/contract.py     strict parsing + semantic validation
fixturegen/engine.py       rule evaluation, component planning, search, verification
fixturegen/material.py     impossibility material, sub-contracts, naive replayer
fixturegen/cli.py          generator command line entry point
fixturegen/merge.py        multi-batch merge: identity domains, rewrites, pack audit
fixturegen/merge_cli.py    merge command line entry point (build / verify)
fixturegen/ledger.py       transactional ledger: persistence, flow, reopen, digests
fixturegen/ledger_rules.py incremental per-rule truth indexes
fixturegen/ledger_cli.py   ledger command line entry point (incl. backfill)
fixturegen/attribution.py  historical FK attribution index: build, refresh, query
fixturegen/attribution_cli.py  attribution command line entry point
examples/                  example contract, generated walkthrough, JSONL replay
examples/merge-demo/       reproducible merge contract, batches, whitelists and pack
examples/history-demo/     reproducible attribution walkthrough (same fact, two times)
tests/                     unit + independent small-enumeration tests
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

Ledger tests (`tests/test_ledger.py`, `tests/test_ledger_random.py`) compare
every incremental truth value and every flow difference against
`evaluate_rules`; cover identity swaps, unchanged children reacting to parent
keys/amounts, cancelling aggregate amounts, idempotent and conflicting replay,
sequence gaps, rejected lines, a simulated crash before commit, reopen with and
without the history audit, tampered databases, and randomised multi-hundred-step
flows including a mid-stream reopen.

Merge tests (`tests/test_merge.py`, `tests/test_merge_random.py`) cover
identity-domain construction (direct, transitive and composite-position
links), whitelist validation, injective within-domain rewriting, locked
values, null and absent-field handling, per-rule bounded conflict witnesses,
determinism under input reordering, the no-trace-on-failure and atomic-write
guarantees, CLI exit codes, and an independent naive cross-batch checker plus
randomized differential trials that re-derive every success and every
reported conflict without using the builder's logic.

Attribution tests (`tests/test_attribution.py`) replay every indexed
sequence against an independent naive oracle — billing and composite-key
contracts, plus randomized differential rounds over generated contracts —
and cover reference classification (missing/partial/null/unmatched/matched/
ambiguous), key-preserving parent moves, per-line request errors, refresh
extension/idempotence/interruption and every refusal case (shorter ledger,
changed contract or initial fixture, diverged prefix, missing or stale
digest metadata), the digest backfill itself, and the storage shape (one
version row per change, one key-state row per carrier-set change, constant
work per query even with many same-key parents).
