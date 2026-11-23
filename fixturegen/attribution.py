"""A reusable historical foreign-key attribution index over a ``ledger/1`` db.

Reports that join synthetic facts to parent rows must answer "which parent
row did this child slot reference *at sequence N*?" — not "which parent row
would it reference now".  Re-running a report against the ledger's current
values silently re-attributes history: a fact that pointed at parent row A
last week may point at row B today.  This module builds a local, self
contained index from the existing contract and one ``ledger/1`` database,
for **one selected FK rule** of the contract.  The report side then needs
only the index file and its to-be-linked list; it never opens the ledger.

The index answers, for any ``(seq, child_slot_id)`` with ``0 <= seq <=``
cutoff (sequence 0 is the initial fixture, any other ``seq`` is the state
after that transaction committed in full):

* ``missing``  — the child's reference fields are not all present
  (:func:`fixturegen.engine.row_reference` "missing");
* ``partial``  — the reference mixes null and non-null fields;
* ``null``     — the reference is wholly null (no reference at all);
* ``unmatched``— a complete non-null reference no selected parent key equals;
* ``matched``  — exactly one parent slot carries that key at ``seq``; the
  answer names the parent slot, the candidate selected there *at that
  sequence* and the full parent row *at that sequence*;
* ``ambiguous``— several parent slots carry that key at ``seq``; the answer
  reports only how many (``parent_matches``).  It never picks one and never
  lists them.

An answer depends only on the rule's own composite key.  The rule truth the
ledger recorded is never consulted: a sequence whose flow entry lists failed
rules is analysed exactly like any other, and a true FK rule does not turn
an ambiguous reference into a unique one (nor a false one into "unmatched").
A parent slot whose key is unchanged but whose other fields changed keeps
one key timeline; the answer still returns the candidate and row selected at
the requested sequence.

Storage shape (why queries and refreshes stay cheap)
----------------------------------------------------
The index never copies all slots per transaction:

* ``slot_version(slot_id, seq, candidate_id)`` — one row per *changed*
  involved slot per transaction, plus the sequence-0 initial rows.  A query
  finds the version in force with one ``seq <= ?`` descending lookup.
* ``parent_key_state(key_json, seq, parent_count, only_slot_id)`` — one row
  per parent key whose carrier set changed at that sequence.  A query finds
  the count (and, when unique, the slot) with one descending lookup, however
  many parent rows share the key: same-key parents are counted by the
  indexing pass, never read one by one at query time.
* ``parent_key_member(key_json, slot_id)`` — the carrier set *at the
  indexed cutoff*, maintained only so a refresh can update ``only_slot_id``
  when a key's membership changes; queries never touch it.
* ``candidate_row(slot_id, candidate_id, row_json)`` — the declared rows of
  the involved slots, stored once.

Building the first index reads the ledger through once (meta, initial
snapshot, whole flow) and verifies the digest chain against the committed
state digest.  A refresh reads, in one consistent snapshot, only the meta,
the digest row at the indexed cutoff and the flow plus digest rows after
it — never a replay of indexed history, never a rescan of the ledger's
current slot table.  The prefix is recognised by comparing the ledger's
``flow_digest`` at the indexed cutoff with the digest the index stored;
ledgers created before that metadata existed need the one-time
``python3 -m fixturegen.ledger_cli backfill`` upgrade first.

Refresh refuses — and keeps the previous index untouched — when the source
ledger is shorter than the index, when the contract or the initial fixture
differs, or when the already-indexed transaction prefix differs.  Re-running
a refresh against an unchanged ledger changes nothing.  The index write is a
single transaction, so after a failed or interrupted refresh a reopened
index serves either the old or the fully refreshed cutoff; answers for
sequences at or below the old cutoff never change.

CLI: see :mod:`fixturegen.attribution_cli`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote

from .contract import Candidate, Contract, ContractError
from .engine import row_key, row_reference
from .ledger import (
    FORMAT as LEDGER_FORMAT,
    LedgerError,
    _canon,
    _canon_changes,
    _contract_digest,
    _decode_changes,
    _decode_rule_list,
    _step_digest,
)

FORMAT = "fk-attribution/1"

STATUSES = ("missing", "partial", "null", "unmatched", "matched", "ambiguous")


class AttributionError(ValueError):
    """The index cannot be built, refreshed or queried as requested."""


# ---------------------------------------------------------------------------
# small canonical helpers


def _canon_text(obj: Any) -> str:
    return _canon(obj).decode("utf-8")


def _key_json(key: Tuple[Any, ...]) -> str:
    return _canon_text(list(key))


def _row_json(candidate: Candidate) -> str:
    return json.dumps(dict(candidate.row), separators=(",", ":"),
                      ensure_ascii=False)


def _read_only_connection(path: str, what: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise AttributionError(f"{what} {path!r} does not exist")
    uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


# ---------------------------------------------------------------------------
# reading the source ledger (one consistent snapshot)


class _LedgerSnapshot:
    """The ledger meta plus the flow/digest rows needed for one operation."""

    def __init__(self, ledger_path: str, contract: Optional[Contract],
                 from_seq: int, need_initial: bool) -> None:
        self.contract_id = ""
        self.contract_digest = ""
        self.initial_digest = ""
        self.state_digest = ""
        self.seq = 0
        self.initial: Dict[str, str] = {}
        self.flow_rows: List[Tuple[int, Tuple[Tuple[str, str], ...],
                                   Tuple[str, ...], Tuple[str, ...]]] = []
        self.has_digest_table = False
        self.digest_rows: Dict[int, str] = {}
        self._read(ledger_path, contract, from_seq, need_initial)

    def _read(self, ledger_path: str, contract: Optional[Contract],
              from_seq: int, need_initial: bool) -> None:
        conn = _read_only_connection(ledger_path, "ledger database")
        try:
            conn.execute("BEGIN")
            try:
                meta = dict(conn.execute("SELECT key, value FROM meta"))
            except sqlite3.Error:
                raise AttributionError(
                    f"ledger database {ledger_path!r} has no meta table; "
                    f"not a {LEDGER_FORMAT} ledger") from None
            if meta.get("format") != LEDGER_FORMAT:
                raise AttributionError(
                    f"unsupported ledger format {meta.get('format')!r}")
            self.contract_id = meta.get("contract_id", "")
            self.contract_digest = meta.get("contract_digest", "")
            self.initial_digest = meta.get("initial_digest", "")
            self.state_digest = meta.get("state_digest", "")
            try:
                self.seq = int(meta["seq"])
            except (KeyError, ValueError):
                raise AttributionError("ledger meta is malformed") from None
            if self.seq < 0:
                raise AttributionError("ledger sequence is negative")
            if contract is not None:
                if self.contract_id != contract.contract_id:
                    raise AttributionError(
                        f"ledger belongs to contract {self.contract_id!r}, "
                        f"not {contract.contract_id!r}")
                if self.contract_digest != _contract_digest(contract):
                    raise AttributionError(
                        "ledger was initialized with a different contract "
                        "document")
            if need_initial:
                self.initial = self._read_initial(conn, contract)
            self.has_digest_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND "
                "name='flow_digest'").fetchone() is not None
            self.flow_rows = self._read_flow(conn, contract, from_seq)
            if self.has_digest_table:
                self.digest_rows = dict(conn.execute(
                    "SELECT seq, digest FROM flow_digest WHERE seq>=?",
                    (from_seq,)))
            conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise AttributionError(
                f"cannot read ledger database {ledger_path!r}: {exc}") from None
        finally:
            conn.close()

    def _read_initial(self, conn: sqlite3.Connection,
                      contract: Optional[Contract]) -> Dict[str, str]:
        rows = conn.execute(
            "SELECT slot_id, candidate_id FROM initial_slot").fetchall()
        initial = dict(rows)
        if len(initial) != len(rows):
            raise AttributionError("ledger initial snapshot has duplicate slots")
        if contract is not None:
            slots = {slot.id: slot for slot in contract.slots}
            missing = sorted(set(slots) - set(initial))
            extra = sorted(set(initial) - set(slots))
            if missing or extra:
                raise AttributionError(
                    f"ledger initial snapshot does not cover the contract "
                    f"slots (missing={missing}, extra={extra})")
            for slot_id, candidate_id in initial.items():
                declared = {c.candidate_id for c in slots[slot_id].candidates}
                if candidate_id not in declared:
                    raise AttributionError(
                        f"ledger initial snapshot names undeclared candidate "
                        f"{candidate_id!r} for slot {slot_id!r}")
        return initial

    def _read_flow(self, conn: sqlite3.Connection,
                   contract: Optional[Contract], from_seq: int):
        rows = conn.execute(
            "SELECT seq, changes, newly_false, recovered FROM flow "
            "WHERE seq>? ORDER BY seq", (from_seq,)).fetchall()
        if contract is not None:
            slots = {slot.id: slot for slot in contract.slots}
            candidates = {slot.id: {c.candidate_id: c
                                    for c in slot.candidates}
                          for slot in contract.slots}
        out = []
        for seq, changes_json, false_json, recovered_json in rows:
            where = f"flow seq {seq}"
            if contract is not None:
                try:
                    changes = _decode_changes(changes_json, slots, candidates,
                                              f"ledger {where}")
                    newly_false = _decode_rule_list(false_json, contract,
                                                    f"ledger {where}")
                    recovered = _decode_rule_list(recovered_json, contract,
                                                  f"ledger {where}")
                except LedgerError as exc:
                    raise AttributionError(str(exc)) from None
            else:
                changes = _decode_changes_lenient(changes_json, where)
                newly_false = _decode_rule_list_lenient(false_json, where)
                recovered = _decode_rule_list_lenient(recovered_json, where)
            out.append((seq, changes, newly_false, recovered))
        return out


def _decode_changes_lenient(raw: str, where: str) -> Tuple[Tuple[str, str], ...]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AttributionError(
            f"ledger {where}: malformed changes ({exc.msg})") from None
    if not isinstance(items, list):
        raise AttributionError(f"ledger {where}: changes must be a list")
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(v, str) for v in item)):
            raise AttributionError(f"ledger {where}: malformed change entry")
        slot_id, candidate_id = item
        if slot_id in seen:
            raise AttributionError(f"ledger {where}: duplicate slot in changes")
        seen.add(slot_id)
        out.append((slot_id, candidate_id))
    return tuple(out)


def _decode_rule_list_lenient(raw: str, where: str) -> Tuple[str, ...]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AttributionError(
            f"ledger {where}: malformed rule list ({exc.msg})") from None
    if (not isinstance(items, list)
            or not all(isinstance(v, str) for v in items)
            or len(set(items)) != len(items)
            or items != sorted(items)):
        raise AttributionError(f"ledger {where}: malformed rule list")
    return tuple(items)


# ---------------------------------------------------------------------------
# the index schema


_INDEX_TABLES = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE slot_version("
    "slot_id TEXT NOT NULL, seq INTEGER NOT NULL, "
    "candidate_id TEXT NOT NULL, PRIMARY KEY (slot_id, seq))",
    "CREATE TABLE candidate_row("
    "slot_id TEXT NOT NULL, candidate_id TEXT NOT NULL, "
    "row_json TEXT NOT NULL, PRIMARY KEY (slot_id, candidate_id))",
    "CREATE TABLE parent_key_state("
    "key_json TEXT NOT NULL, seq INTEGER NOT NULL, "
    "parent_count INTEGER NOT NULL, only_slot_id TEXT, "
    "PRIMARY KEY (key_json, seq))",
    "CREATE TABLE parent_key_member("
    "key_json TEXT NOT NULL, slot_id TEXT NOT NULL, "
    "PRIMARY KEY (key_json, slot_id))",
)


def _write_index_meta(conn: sqlite3.Connection, values: Mapping[str, Any]) -> None:
    for key, value in values.items():
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


def _read_index_meta(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        raise AttributionError(
            f"not a {FORMAT} index (no meta table)") from None
    if meta.get("format") != FORMAT:
        raise AttributionError(
            f"unsupported index format {meta.get('format')!r}")
    return meta


def _load_catalog(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Candidate]:
    catalog: Dict[Tuple[str, str], Candidate] = {}
    for slot_id, candidate_id, row_json in conn.execute(
            "SELECT slot_id, candidate_id, row_json FROM candidate_row"):
        row = json.loads(row_json)
        catalog[(slot_id, candidate_id)] = Candidate(
            candidate_id, tuple(row.items()))
    return catalog


# ---------------------------------------------------------------------------
# the incremental indexing pass (shared by build and refresh)


def _extend_index(conn: sqlite3.Connection,
                  catalog: Mapping[Tuple[str, str], Candidate],
                  parent_slots: Sequence[str],
                  parent_key_fields: Sequence[str],
                  flow_rows: Iterable[Tuple[int, Tuple[Tuple[str, str], ...],
                                            Tuple[str, ...], Tuple[str, ...]]],
                  base_seq: int) -> None:
    """Fold decoded flow rows (all with seq > ``base_seq``) into the index.

    Reads, per touched slot or key, only that slot's or key's current index
    row at ``base_seq``; never rescans the indexed history or the full
    current table.  ``catalog`` maps every declared (slot, candidate) of the
    involved slots to its row.
    """
    involved = {slot_id for (slot_id, _cid) in catalog}
    parent_set = set(parent_slots)
    parent_key_of = {
        key: row_key(candidate, parent_key_fields)
        for key, candidate in catalog.items() if key[0] in parent_set
    }
    current: Dict[str, str] = {}
    key_count: Dict[Tuple[Any, ...], int] = {}

    def candidate_at_base(slot_id: str) -> str:
        row = conn.execute(
            "SELECT candidate_id FROM slot_version WHERE slot_id=? AND seq<=? "
            "ORDER BY seq DESC LIMIT 1", (slot_id, base_seq)).fetchone()
        if row is None:
            raise AttributionError(
                f"index has no version at or before sequence {base_seq} "
                f"for slot {slot_id!r}")
        return row[0]

    def count_at_base(key: Tuple[Any, ...]) -> int:
        row = conn.execute(
            "SELECT parent_count FROM parent_key_state "
            "WHERE key_json=? AND seq<=? ORDER BY seq DESC LIMIT 1",
            (_key_json(key), base_seq)).fetchone()
        return row[0] if row is not None else 0

    def key_remove(key: Tuple[Any, ...], slot_id: str, seq: int) -> None:
        count = key_count.get(key)
        if count is None:
            count = count_at_base(key)
        if count < 1:
            raise AttributionError(
                "index membership is inconsistent with its key states")
        kj = _key_json(key)
        conn.execute(
            "DELETE FROM parent_key_member WHERE key_json=? AND slot_id=?",
            (kj, slot_id))
        new_count = count - 1
        only: Optional[str] = None
        if new_count == 1:
            members = conn.execute(
                "SELECT slot_id FROM parent_key_member WHERE key_json=?",
                (kj,)).fetchall()
            if len(members) != 1:
                raise AttributionError(
                    "index membership is inconsistent with its key states")
            only = members[0][0]
        _store_key_state(conn, kj, seq, new_count, only)
        key_count[key] = new_count

    def key_add(key: Tuple[Any, ...], slot_id: str, seq: int) -> None:
        count = key_count.get(key)
        if count is None:
            count = count_at_base(key)
        kj = _key_json(key)
        try:
            conn.execute(
                "INSERT INTO parent_key_member(key_json, slot_id) "
                "VALUES (?,?)", (kj, slot_id))
        except sqlite3.IntegrityError:
            raise AttributionError(
                "index membership is inconsistent with its key states") from None
        new_count = count + 1
        _store_key_state(conn, kj, seq, new_count,
                         slot_id if new_count == 1 else None)
        key_count[key] = new_count

    for tx_seq, changes, _newly_false, _recovered in flow_rows:
        for slot_id, candidate_id in changes:
            if slot_id not in involved:
                continue
            known = catalog.get((slot_id, candidate_id))
            if known is None:
                raise AttributionError(
                    f"ledger flow seq {tx_seq} names undeclared candidate "
                    f"{candidate_id!r} for slot {slot_id!r}")
            old = current.get(slot_id)
            if old is None:
                old = candidate_at_base(slot_id)
                current[slot_id] = old
            if old == candidate_id:
                continue
            current[slot_id] = candidate_id
            conn.execute(
                "INSERT INTO slot_version(slot_id, seq, candidate_id) "
                "VALUES (?,?,?)", (slot_id, tx_seq, candidate_id))
            if slot_id in parent_set:
                old_key = parent_key_of[(slot_id, old)]
                new_key = parent_key_of[(slot_id, candidate_id)]
                if old_key != new_key:
                    if old_key is not None:
                        key_remove(old_key, slot_id, tx_seq)
                    if new_key is not None:
                        key_add(new_key, slot_id, tx_seq)


def _store_key_state(conn: sqlite3.Connection, key_json: str, seq: int,
                     count: int, only_slot_id: Optional[str]) -> None:
    # One row per (key, sequence): the end-of-transaction state.  A key
    # touched twice inside one transaction keeps only its final row, just
    # like the ledger never records intermediate states.
    conn.execute(
        "INSERT INTO parent_key_state(key_json, seq, parent_count, "
        "only_slot_id) VALUES (?,?,?,?) "
        "ON CONFLICT(key_json, seq) DO UPDATE SET "
        "parent_count=excluded.parent_count, "
        "only_slot_id=excluded.only_slot_id",
        (key_json, seq, count, only_slot_id))


def _initial_key_rows(catalog: Mapping[Tuple[str, str], Candidate],
                      parent_slots: Sequence[str],
                      parent_key_fields: Sequence[str],
                      initial: Mapping[str, str]):
    carriers: Dict[Tuple[Any, ...], List[str]] = {}
    for slot_id in parent_slots:
        candidate = catalog[(slot_id, initial[slot_id])]
        key = row_key(candidate, parent_key_fields)
        if key is not None:
            carriers.setdefault(key, []).append(slot_id)
    states = []
    members = []
    for key in sorted(carriers, key=_key_json):
        slots = sorted(carriers[key])
        states.append((_key_json(key), 0, len(slots),
                       slots[0] if len(slots) == 1 else None))
        for slot_id in slots:
            members.append((_key_json(key), slot_id))
    return states, members


# ---------------------------------------------------------------------------
# rule spec


def _rule_spec(contract: Contract, rule_id: str) -> Dict[str, Any]:
    try:
        rule = contract.rule(rule_id)
    except ContractError:
        raise AttributionError(
            f"contract declares no rule {rule_id!r}") from None
    if rule.kind != "fk":
        raise AttributionError(
            f"rule {rule_id!r} is a {rule.kind} rule; the attribution index "
            f"covers exactly one fk rule")
    params = rule.params
    return {
        "child": params["child"],
        "child_fields": list(params["child_fields"]),
        "parent": params["parent"],
        "parent_key": list(params["parent_key"]),
    }


def _chain_digest(initial_digest: str,
                  flow_rows: Iterable[Tuple[int, Tuple[Tuple[str, str], ...],
                                            Tuple[str, ...], Tuple[str, ...]]],
                  first_seq: int) -> Tuple[str, int, Dict[int, str]]:
    """Chain the digest over decoded flow rows.

    Returns ``(final digest, last sequence, {seq: digest})`` and requires
    the rows to be exactly the contiguous range ``first_seq+1 ..``.
    """
    digest = initial_digest
    expected = first_seq
    chain: Dict[int, str] = {}
    for seq, changes, newly_false, recovered in flow_rows:
        if seq != expected + 1:
            raise AttributionError(
                f"ledger flow has a sequence gap at {expected + 1} "
                f"(found {seq})")
        digest = _step_digest(digest, seq, _canon_changes(changes),
                              newly_false, recovered)
        chain[seq] = digest
        expected = seq
    return digest, expected, chain


# ---------------------------------------------------------------------------
# build


def build_index(index_path: str, contract: Contract, ledger_path: str,
                rule_id: str) -> Dict[str, Any]:
    """Create a new attribution index for one FK rule.

    Reads the whole ledger once (the only pass that may), verifies the
    contract identity, the initial snapshot and the digest chain, and writes
    the index atomically (temp file renamed into place).  Refuses to
    overwrite an existing index.
    """
    spec = _rule_spec(contract, rule_id)
    child_slots = tuple(sorted(
        s.id for s in contract.slots_for_table(spec["child"])))
    parent_slots = tuple(sorted(
        s.id for s in contract.slots_for_table(spec["parent"])))
    involved = set(child_slots) | set(parent_slots)
    catalog: Dict[Tuple[str, str], Candidate] = {}
    for slot in contract.slots:
        if slot.id not in involved:
            continue
        for candidate in slot.candidates:
            catalog[(slot.id, candidate.candidate_id)] = candidate

    snapshot = _LedgerSnapshot(ledger_path, contract, from_seq=0,
                               need_initial=True)
    initial_digest = hashlib.sha256(_canon({
        "contract": snapshot.contract_digest,
        "initial": sorted(snapshot.initial.items())})).hexdigest()
    if initial_digest != snapshot.initial_digest:
        raise AttributionError(
            "ledger initial snapshot does not match its committed digest")
    digest, last_seq, chain = _chain_digest(initial_digest,
                                            snapshot.flow_rows, 0)
    if last_seq != snapshot.seq:
        raise AttributionError(
            f"ledger flow is not the completed prefix 1..{snapshot.seq}")
    if digest != snapshot.state_digest:
        raise AttributionError(
            "ledger flow does not match the committed state digest")
    if snapshot.has_digest_table:
        # The ledger's own per-sequence metadata, when present, must agree
        # with the chain recomputed from the flow.
        for seq, chained in chain.items():
            stored = snapshot.digest_rows.get(seq)
            if stored is None:
                raise AttributionError(
                    "ledger digest metadata is incomplete; run "
                    "python3 -m fixturegen.ledger_cli backfill")
            if stored != chained:
                raise AttributionError(
                    f"ledger digest metadata disagrees with the flow at "
                    f"sequence {seq}")

    if os.path.exists(index_path):
        raise AttributionError(
            f"refusing to overwrite existing index {index_path!r}")
    directory = os.path.dirname(os.path.abspath(index_path)) or "."
    if not os.path.isdir(directory):
        raise AttributionError(f"directory does not exist: {directory!r}")
    temp_path = f"{index_path}.init-{os.getpid()}.tmp"
    if os.path.exists(temp_path):
        os.unlink(temp_path)
    conn = sqlite3.connect(temp_path, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _INDEX_TABLES:
                conn.execute(statement)
            _write_index_meta(conn, {
                "format": FORMAT,
                "contract_id": contract.contract_id,
                "contract_digest": snapshot.contract_digest,
                "rule_id": rule_id,
                "rule_spec": _canon_text(spec),
                "child_slots": _canon_text(list(child_slots)),
                "parent_slots": _canon_text(list(parent_slots)),
                "initial_digest": snapshot.initial_digest,
                "indexed_seq": snapshot.seq,
                "indexed_digest": digest,
            })
            conn.executemany(
                "INSERT INTO candidate_row(slot_id, candidate_id, row_json) "
                "VALUES (?,?,?)",
                [(slot_id, candidate_id, _row_json(candidate))
                 for (slot_id, candidate_id), candidate
                 in sorted(catalog.items())])
            conn.executemany(
                "INSERT INTO slot_version(slot_id, seq, candidate_id) "
                "VALUES (?,?,?)",
                [(slot_id, 0, snapshot.initial[slot_id])
                 for slot_id in sorted(involved)])
            states, members = _initial_key_rows(
                catalog, parent_slots, spec["parent_key"], snapshot.initial)
            conn.executemany(
                "INSERT INTO parent_key_state(key_json, seq, parent_count, "
                "only_slot_id) VALUES (?,?,?,?)", states)
            conn.executemany(
                "INSERT INTO parent_key_member(key_json, slot_id) "
                "VALUES (?,?)", members)
            _extend_index(conn, catalog, parent_slots, spec["parent_key"],
                          snapshot.flow_rows, base_seq=0)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if os.path.exists(index_path):
            raise AttributionError(
                f"refusing to overwrite existing index {index_path!r}")
        conn.close()
        os.replace(temp_path, index_path)
    except BaseException:
        conn.close()
        for path in (temp_path, temp_path + "-journal"):
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    return {
        "ok": True,
        "built": True,
        "rule_id": rule_id,
        "indexed_seq": snapshot.seq,
        "indexed_digest": digest,
        "transactions_indexed": last_seq,
    }


# ---------------------------------------------------------------------------
# refresh


def refresh_index(index_path: str, ledger_path: str,
                  before_commit_hook=None) -> Dict[str, Any]:
    """Advance an existing index to the ledger's current commit point.

    Reads only the ledger meta, the digest row at the indexed cutoff and
    the flow/digest rows after it — all in one consistent snapshot — then
    appends the new versions in a single index transaction.  Refuses (and
    keeps the previous index) when the source ledger is shorter, when the
    contract or initial fixture differs, or when the already-indexed
    transaction prefix differs.  Refreshing an unchanged ledger is a no-op.
    """
    if not os.path.exists(index_path):
        raise AttributionError(f"index database {index_path!r} does not exist")
    conn = sqlite3.connect(index_path, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        meta = _read_index_meta(conn)
        try:
            indexed_seq = int(meta["indexed_seq"])
            indexed_digest = meta["indexed_digest"]
            spec = json.loads(meta["rule_spec"])
            parent_slots = tuple(json.loads(meta["parent_slots"]))
        except (KeyError, ValueError, json.JSONDecodeError):
            raise AttributionError("index meta is malformed") from None
        catalog = _load_catalog(conn)

        snapshot = _LedgerSnapshot(ledger_path, None, from_seq=indexed_seq,
                                   need_initial=False)
        if snapshot.contract_id != meta["contract_id"] \
                or snapshot.contract_digest != meta["contract_digest"]:
            raise AttributionError(
                "ledger contract differs from the one the index was built "
                "from; refusing to refresh")
        if snapshot.initial_digest != meta["initial_digest"]:
            raise AttributionError(
                "ledger initial fixture differs from the one the index was "
                "built from; refusing to refresh")
        if snapshot.seq < indexed_seq:
            raise AttributionError(
                f"source ledger is shorter (sequence {snapshot.seq}) than "
                f"the indexed cutoff ({indexed_seq}); refusing to refresh")
        if snapshot.seq == indexed_seq:
            if snapshot.state_digest != indexed_digest:
                raise AttributionError(
                    "ledger content at the indexed sequence differs from "
                    "the indexed prefix; refusing to refresh")
            return {
                "ok": True,
                "refreshed": False,
                "rule_id": meta["rule_id"],
                "indexed_seq": indexed_seq,
                "indexed_digest": indexed_digest,
                "transactions_indexed": 0,
            }

        if not snapshot.has_digest_table:
            raise AttributionError(
                "ledger has no per-sequence digest metadata; run "
                "python3 -m fixturegen.ledger_cli backfill once before "
                "refreshing")
        base_digest = snapshot.digest_rows.get(indexed_seq)
        if base_digest is None:
            raise AttributionError(
                "ledger digest metadata does not cover the indexed cutoff; "
                "run python3 -m fixturegen.ledger_cli backfill")
        if base_digest != indexed_digest:
            raise AttributionError(
                "ledger transaction prefix differs from the indexed prefix; "
                "refusing to refresh")

        digest, last_seq, chain = _chain_digest(indexed_digest,
                                                snapshot.flow_rows,
                                                indexed_seq)
        for seq, chained in chain.items():
            stored = snapshot.digest_rows.get(seq)
            if stored is None:
                raise AttributionError(
                    "ledger digest metadata is incomplete; run "
                    "python3 -m fixturegen.ledger_cli backfill")
            if stored != chained:
                raise AttributionError(
                    f"ledger digest metadata disagrees with the flow at "
                    f"sequence {seq}")
        if last_seq != snapshot.seq:
            raise AttributionError(
                "ledger flow does not cover the committed sequence")
        if digest != snapshot.state_digest:
            raise AttributionError(
                "ledger flow does not match the committed state digest")

        conn.execute("BEGIN IMMEDIATE")
        try:
            _extend_index(conn, catalog, parent_slots, spec["parent_key"],
                          snapshot.flow_rows, base_seq=indexed_seq)
            _write_index_meta(conn, {
                "indexed_seq": snapshot.seq,
                "indexed_digest": digest,
            })
            if before_commit_hook is not None:
                before_commit_hook()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return {
            "ok": True,
            "refreshed": True,
            "rule_id": meta["rule_id"],
            "indexed_seq": snapshot.seq,
            "indexed_digest": digest,
            "transactions_indexed": snapshot.seq - indexed_seq,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# querying


class AttributionIndex:
    """A read-only handle on a built index, for any number of query batches."""

    def __init__(self, index_path: str) -> None:
        self.path = index_path
        conn = _read_only_connection(index_path, "index database")
        self.conn = conn
        try:
            meta = _read_index_meta(conn)
            self.rule_id = meta["rule_id"]
            self.contract_id = meta["contract_id"]
            self.contract_digest = meta["contract_digest"]
            self.spec = json.loads(meta["rule_spec"])
            self.child_slots = tuple(json.loads(meta["child_slots"]))
            self.parent_slots = tuple(json.loads(meta["parent_slots"]))
            self.indexed_seq = int(meta["indexed_seq"])
            self.indexed_digest = meta["indexed_digest"]
        except (KeyError, ValueError, json.JSONDecodeError):
            conn.close()
            raise AttributionError("index meta is malformed") from None
        except BaseException:
            conn.close()
            raise
        self._child_set = set(self.child_slots)
        self._catalog = _load_catalog(conn)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AttributionIndex":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def status(self) -> Dict[str, Any]:
        versions = self.conn.execute(
            "SELECT COUNT(*) FROM slot_version").fetchone()[0]
        key_states = self.conn.execute(
            "SELECT COUNT(*) FROM parent_key_state").fetchone()[0]
        return {
            "format": FORMAT,
            "contract_id": self.contract_id,
            "contract_digest": self.contract_digest,
            "rule_id": self.rule_id,
            "rule_spec": self.spec,
            "child_slots": list(self.child_slots),
            "parent_slots": list(self.parent_slots),
            "indexed_seq": self.indexed_seq,
            "indexed_digest": self.indexed_digest,
            "slot_versions": versions,
            "parent_key_states": key_states,
        }

    def _version_at(self, slot_id: str, seq: int) -> str:
        row = self.conn.execute(
            "SELECT candidate_id FROM slot_version WHERE slot_id=? AND seq<=? "
            "ORDER BY seq DESC LIMIT 1", (slot_id, seq)).fetchone()
        if row is None:
            raise AttributionError(
                f"index holds no version of slot {slot_id!r} at or before "
                f"sequence {seq}")
        return row[0]

    def answer(self, seq: int, child_slot_id: str) -> Dict[str, Any]:
        """The attribution of one child slot at one committed sequence."""
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise AttributionError(
                f"'seq' must be a non-negative integer, got {seq!r}")
        if child_slot_id not in self._child_set:
            raise AttributionError(
                f"slot {child_slot_id!r} is not a child slot of rule "
                f"{self.rule_id!r} (child table {self.spec['child']!r})")
        if seq < 0 or seq > self.indexed_seq:
            raise AttributionError(
                f"sequence {seq} is outside the indexed range "
                f"0..{self.indexed_seq}")
        candidate_id = self._version_at(child_slot_id, seq)
        candidate = self._catalog.get((child_slot_id, candidate_id))
        if candidate is None:
            raise AttributionError(
                f"index catalog has no row for candidate {candidate_id!r} "
                f"of slot {child_slot_id!r}")
        status, values = row_reference(candidate, self.spec["child_fields"])
        out: Dict[str, Any] = {
            "seq": seq,
            "child_slot_id": child_slot_id,
            "status": status,
        }
        if status != "value":
            return out
        row = self.conn.execute(
            "SELECT parent_count, only_slot_id FROM parent_key_state "
            "WHERE key_json=? AND seq<=? ORDER BY seq DESC LIMIT 1",
            (_key_json(values), seq)).fetchone()
        count = row[0] if row is not None else 0
        if count == 0:
            out["status"] = "unmatched"
            return out
        if count > 1:
            out["status"] = "ambiguous"
            out["parent_matches"] = count
            return out
        parent_slot_id = row[1]
        parent_candidate_id = self._version_at(parent_slot_id, seq)
        parent = self._catalog.get((parent_slot_id, parent_candidate_id))
        if parent is None:
            raise AttributionError(
                f"index catalog has no row for candidate "
                f"{parent_candidate_id!r} of slot {parent_slot_id!r}")
        out["status"] = "matched"
        out["parent_slot_id"] = parent_slot_id
        out["parent_candidate_id"] = parent_candidate_id
        out["parent_row"] = dict(parent.row)
        return out


def index_status(index_path: str) -> Dict[str, Any]:
    with AttributionIndex(index_path) as index:
        return index.status()


# ---------------------------------------------------------------------------
# JSONL request batches


def answer_request(index: AttributionIndex, payload: Any) -> Dict[str, Any]:
    """Validate one decoded request line and answer it (no line number)."""
    if not isinstance(payload, dict):
        raise AttributionError("request must be a JSON object")
    extra = sorted(str(k) for k in set(payload) - {"seq", "child_slot_id"})
    if extra:
        raise AttributionError(f"unsupported key(s) {extra}")
    if "seq" not in payload:
        raise AttributionError("missing 'seq'")
    if "child_slot_id" not in payload:
        raise AttributionError("missing 'child_slot_id'")
    seq = payload["seq"]
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise AttributionError(
            f"'seq' must be a non-negative integer, got {seq!r}")
    child_slot_id = payload["child_slot_id"]
    if not isinstance(child_slot_id, str) or not child_slot_id:
        raise AttributionError("'child_slot_id' must be a non-empty string")
    return index.answer(seq, child_slot_id)


def run_requests(index: AttributionIndex, lines: Iterable[str]
                 ) -> Tuple[List[Dict[str, Any]], int]:
    """Answer a JSONL batch, one result per input line, in input order.

    A bad line never aborts the batch and never falls back to current
    values: it is reported in place with its line number, and the remaining
    lines are answered normally.  Returns ``(results, error_count)``.
    """
    results: List[Dict[str, Any]] = []
    errors = 0
    for line_no, raw in enumerate(lines, 1):
        out: Dict[str, Any] = {"line": line_no}
        payload: Any = None
        parsed = False
        try:
            if not raw.strip():
                raise AttributionError("empty line")
            try:
                payload = json.loads(raw)
                parsed = isinstance(payload, dict)
            except json.JSONDecodeError as exc:
                raise AttributionError(f"invalid JSON: {exc.msg}") from None
            answer = answer_request(index, payload)
            out.update(answer)
        except AttributionError as exc:
            if parsed:
                for key in ("seq", "child_slot_id"):
                    if key in payload:
                        out[key] = payload[key]
            out["status"] = "error"
            out["error"] = str(exc)
            errors += 1
        results.append(out)
    return results, errors
