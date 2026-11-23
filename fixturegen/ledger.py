"""A reopenable, append-only transaction-quality ledger over a fixture contract.

The ledger starts from *one verified normal fixture* of an existing
``fixture-contract/1`` document and then accepts a JSONL stream of synthetic
transactions.  A transaction selects, for one or more row-identity slots, a
candidate the contract already declares::

    {"seq": 1, "changes": [{"slot_id": "S01-client", "candidate_id": "c2"}]}

No customer data is read and no value outside the contract's candidate rows can
enter the ledger; slots and the contract are fixed at initialization.

Per-sequence digest metadata
----------------------------
In addition to the flow itself a ledger may carry a ``flow_digest`` table
mapping every committed sequence to the state digest chained up to that
point.  The table is additive metadata: ledgers created by this version have
it from initialization and ``apply`` keeps it current, while older databases
gain it through a one-time, idempotent :meth:`Ledger.backfill_flow_digests`
that recomputes the chain from the recorded flow and refuses to store
anything that disagrees with the committed state digest.  Reopening verifies
the table (when present) with a bounded aggregate query, never a replay.
The historical attribution index (:mod:`fixturegen.attribution`) uses this
metadata to recognise an already-indexed transaction prefix without
re-reading history; every ledger command behaves exactly as before whether
or not the table exists.

Per transaction the ledger records the submitted selection, the rules that
newly became false and the rules that recovered, both lists sorted by rule id.
Truth is decided on the *complete* selection after the whole transaction with
the semantics of :func:`fixturegen.engine.evaluate_rules`.  Intermediate states
while a transaction's changes are applied one by one are never recorded.  A
false rule is a recorded outcome, not a rejection: the next transaction
continues from the possibly-invalid state.

Persistence and crash safety
----------------------------
State lives in one SQLite database.  A transaction's slot updates, flow row,
sequence counter and state digests are written in a single ``BEGIN IMMEDIATE`` /
``COMMIT``.  An interrupted writer therefore leaves either the whole transaction
or none of it.

Reopening performs one full consistency read of the *current* selection: it
checks slot coverage and candidate validity, recomputes the selection digest
(maintained incrementally in O(changes) per transaction and compared against the
sequence written in the same commit), checks the flow is the contiguous prefix
``1..seq``, and re-evaluates every rule with the reference evaluator.  It never
replays historical flow to reconstruct state, and resuming does not depend on
history.  An optional ``verify_history=True`` additionally replays the deltas
and the digest chain as an audit.

After initialization a transaction only touches its changed slots, the old/new
key groups those slots affect, and per-rule aggregates
(:mod:`fixturegen.ledger_rules`); it never rescans unrelated keys, slots or
historical flow rows.

Only a single writer is supported while a ledger is open.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .contract import Candidate, Contract
from .engine import evaluate_rules, verify_fixture
from .ledger_rules import RuleTracker

FORMAT = "ledger/1"
_MOD = 1 << 256


class LedgerError(ValueError):
    """A ledger operation is impossible: bad input, bad state or bad reopen."""


class InputError(LedgerError):
    """One transaction line is not acceptable; carries the offending line."""

    def __init__(self, message: str, line: Optional[int] = None,
                 source: Optional[str] = None) -> None:
        self.reason = message
        self.line = line
        self.source = source
        location = ""
        if source is not None and line is not None:
            location = f"{source}:{line}: "
        elif line is not None:
            location = f"line {line}: "
        super().__init__(location + message)


# ---------------------------------------------------------------------------
# canonical encoding and digests


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _canon_json(obj: Any) -> str:
    return _canon(obj).decode("utf-8")


def _canon_changes(changes: Iterable[Tuple[str, str]]) -> List[List[str]]:
    return [[slot_id, candidate_id]
            for slot_id, candidate_id in sorted(changes)]


def _contract_digest(contract: Contract) -> str:
    return hashlib.sha256(_canon(contract.raw)).hexdigest()


def _slot_term(contract_digest: str, slot_id: str, candidate_id: str) -> int:
    payload = _canon([contract_digest, slot_id, candidate_id])
    return int.from_bytes(hashlib.sha256(payload).digest(), "big")


def _selection_sum(contract_digest: str,
                   selection: Mapping[str, str]) -> int:
    total = 0
    for slot_id, candidate_id in selection.items():
        total += _slot_term(contract_digest, slot_id, candidate_id)
    return total % _MOD


def _step_digest(previous_hex: str, seq: int,
                 changes: Sequence[Sequence[str]],
                 newly_false: Sequence[str],
                 recovered: Sequence[str]) -> str:
    payload = _canon({"seq": seq, "changes": list(changes),
                      "newly_false": list(newly_false),
                      "recovered": list(recovered)})
    digest = hashlib.sha256(bytes.fromhex(previous_hex))
    digest.update(payload)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# parsed transactions and results


@dataclass(frozen=True)
class Transaction:
    seq: int
    changes: Tuple[Tuple[str, str], ...]     # (slot_id, candidate_id), input order

    @property
    def mapping(self) -> Dict[str, str]:
        return dict(self.changes)

    @property
    def canonical(self) -> List[List[str]]:
        return _canon_changes(self.changes)


@dataclass(frozen=True)
class FlowEntry:
    seq: int
    changes: Tuple[Tuple[str, str], ...]
    newly_false: Tuple[str, ...]
    recovered: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "changes": [{"slot_id": slot_id, "candidate_id": candidate_id}
                        for slot_id, candidate_id in self.changes],
            "newly_false": list(self.newly_false),
            "recovered": list(self.recovered),
        }


@dataclass(frozen=True)
class ApplyResult:
    seq: int
    status: str                     # "applied" or "skipped"
    changes: Tuple[Tuple[str, str], ...]
    newly_false: Tuple[str, ...]
    recovered: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "status": self.status,
            "changes": [{"slot_id": slot_id, "candidate_id": candidate_id}
                        for slot_id, candidate_id in self.changes],
            "newly_false": list(self.newly_false),
            "recovered": list(self.recovered),
        }


@dataclass
class ReplayResult:
    results: List[ApplyResult]
    applied: int = 0
    skipped: int = 0
    error: Optional[InputError] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> Dict[str, Any]:
        doc: Dict[str, Any] = {
            "ok": self.ok,
            "applied": self.applied,
            "skipped": self.skipped,
            "transactions": [r.to_dict() for r in self.results],
        }
        if self.error is not None:
            doc["error"] = {
                "reason": self.error.reason,
                "line": self.error.line,
                "source": self.error.source,
            }
        return doc


# ---------------------------------------------------------------------------
# input validation


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _at(where: Optional[str], message: str) -> str:
    return f"{where}: {message}" if where else message


def parse_transaction(payload: Any, contract: Contract,
                      where: str = "transaction",
                      slot_index: Optional[Mapping[str, Sequence[str]]] = None
                      ) -> Transaction:
    """Validate one decoded JSONL line against the contract's declared surface.

    ``slot_index`` maps slot id -> declared candidate ids and may be passed by
    a hot caller so validation touches only the slots the line names.
    """
    if not isinstance(payload, dict):
        raise LedgerError(_at(where, "must be a JSON object"))
    extra = sorted(str(k) for k in set(payload) - {"seq", "changes"})
    if extra:
        raise LedgerError(_at(where, f"unsupported key(s) {extra}"))
    if "seq" not in payload:
        raise LedgerError(_at(where, "missing 'seq'"))
    if "changes" not in payload:
        raise LedgerError(_at(where, "missing 'changes'"))
    seq = payload["seq"]
    if not _is_int(seq) or seq < 1:
        raise LedgerError(
            _at(where, f"'seq' must be a positive integer, got {seq!r}"))
    raw_changes = payload["changes"]
    if not isinstance(raw_changes, list) or not raw_changes:
        raise LedgerError(_at(where, "'changes' must be a non-empty list"))

    slots = slot_index
    if slots is None:
        slots = {slot.id: tuple(c.candidate_id for c in slot.candidates)
                 for slot in contract.slots}
    seen: Dict[str, str] = {}
    changes: List[Tuple[str, str]] = []
    for index, entry in enumerate(raw_changes):
        cw = _at(where, f"changes[{index}]")
        if not isinstance(entry, dict):
            raise LedgerError(f"{cw}: must be an object")
        extra = sorted(str(k) for k in set(entry) - {"slot_id", "candidate_id"})
        if extra:
            raise LedgerError(f"{cw}: unsupported key(s) {extra}")
        slot_id = entry.get("slot_id")
        candidate_id = entry.get("candidate_id")
        if not isinstance(slot_id, str) or not slot_id:
            raise LedgerError(f"{cw}: 'slot_id' must be a non-empty string")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise LedgerError(f"{cw}: 'candidate_id' must be a non-empty string")
        if slot_id not in slots:
            raise LedgerError(f"{cw}: unknown slot {slot_id!r}")
        if candidate_id not in slots[slot_id]:
            raise LedgerError(
                f"{cw}: slot {slot_id!r} does not declare candidate "
                f"{candidate_id!r}")
        if slot_id in seen:
            raise LedgerError(
                f"{cw}: slot {slot_id!r} appears more than once in this "
                f"transaction")
        seen[slot_id] = candidate_id
        changes.append((slot_id, candidate_id))
    return Transaction(seq=seq, changes=tuple(changes))


# ---------------------------------------------------------------------------
# the ledger


class Ledger:
    """An open ledger.  Construct through :func:`init_ledger`/`open_ledger`."""

    def __init__(self, conn: sqlite3.Connection, contract: Contract,
                 db_path: str, before_commit_hook=None) -> None:
        self.conn = conn
        self.contract = contract
        self.db_path = db_path
        self._before_commit = before_commit_hook
        self._slots = {slot.id: slot for slot in contract.slots}
        self._candidates: Dict[str, Dict[str, Candidate]] = {
            slot.id: {c.candidate_id: c for c in slot.candidates}
            for slot in contract.slots
        }
        self._candidate_ids = {
            slot_id: tuple(candidates) for slot_id, candidates
            in self._candidates.items()}
        self._contract_digest = _contract_digest(contract)
        self.assignment: Dict[str, Candidate] = {}
        self.tracker: Optional[RuleTracker] = None
        self.seq = 0
        self._selection_sum = 0
        self._state_digest = ""
        self._digest_table = False

    # -- construction ----------------------------------------------------
    @classmethod
    def initialize(cls, db_path: str, contract: Contract,
                   fixture: Mapping[str, Any],
                   before_commit_hook=None) -> "Ledger":
        verification = verify_fixture(contract, fixture)
        if not verification["ok"]:
            raise LedgerError(
                "normal fixture failed independent verification: "
                + "; ".join(verification["errors"]))
        if fixture.get("kind") != "normal" or fixture.get("target") is not None:
            raise LedgerError("initial fixture must be a normal fixture")
        computed = verification["computed"]
        if fixture.get("rules") != computed:
            raise LedgerError(
                "fixture rule table disagrees with independent evaluation")
        if not all(computed.values()):
            raise LedgerError(
                "initial fixture must satisfy every rule; false: "
                + ", ".join(sorted(k for k, v in computed.items() if not v)))

        selection: Dict[str, Candidate] = {}
        stated_selection: Dict[str, str] = {}
        for slot in contract.slots:
            entry = fixture["slots"][slot.id]
            candidate = next(c for c in slot.candidates
                             if c.candidate_id == entry["candidate_id"])
            selection[slot.id] = candidate
            stated_selection[slot.id] = candidate.candidate_id

        # Cross-check the incremental indexes against the reference evaluator
        # before recording a single transaction.
        tracker = RuleTracker(contract, selection)
        if tracker.truths() != computed:
            raise LedgerError(
                "incremental rule indexes disagree with evaluate_rules at init")

        if os.path.exists(db_path):
            raise LedgerError(f"refusing to overwrite existing file {db_path!r}")

        # Build in a sibling temp file and atomically rename it into place, so
        # an interrupted initialization leaves neither a partial database nor
        # a destination to accidentally resume from.
        directory = os.path.dirname(os.path.abspath(db_path)) or "."
        if not os.path.isdir(directory):
            raise LedgerError(f"directory does not exist: {directory!r}")
        temp_path = f"{db_path}.init-{os.getpid()}.tmp"
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        conn = sqlite3.connect(temp_path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            ledger_contract_digest = _contract_digest(contract)
            selection_sum = _selection_sum(ledger_contract_digest,
                                           stated_selection)
            initial_digest = hashlib.sha256(
                _canon({"contract": ledger_contract_digest,
                        "initial": sorted(stated_selection.items())})
            ).hexdigest()
            state_digest = initial_digest
            with _immediate(conn):
                cls._create_schema(conn)
                rows = sorted(stated_selection.items())
                conn.executemany(
                    "INSERT INTO initial_slot(slot_id, candidate_id) VALUES (?,?)",
                    rows)
                conn.executemany(
                    "INSERT INTO slot_state(slot_id, candidate_id) VALUES (?,?)",
                    rows)
                ledger = cls(conn, contract, db_path, before_commit_hook)
                ledger._write_meta({
                    "format": FORMAT,
                    "contract_id": contract.contract_id,
                    "contract_digest": ledger_contract_digest,
                    "selection_sum": f"{selection_sum:064x}",
                    "initial_digest": initial_digest,
                    "state_digest": state_digest,
                    "seq": 0,
                    "rules_after": _canon_json(computed),
                })
            if os.path.exists(db_path):
                raise LedgerError(
                    f"refusing to overwrite existing file {db_path!r}")
            conn.close()
            os.replace(temp_path, db_path)
            # Reopen under the final name: SQLite binds its write lock to the
            # path it was opened with, and the temp name no longer exists.
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.execute("PRAGMA synchronous=FULL")
            ledger.conn = conn
        except BaseException:
            conn.close()
            for path in (temp_path, temp_path + "-journal"):
                try:
                    os.unlink(path)
                except OSError:
                    pass
            raise
        ledger.assignment = selection
        ledger.tracker = tracker
        ledger.seq = 0
        ledger._selection_sum = selection_sum
        ledger._state_digest = state_digest
        ledger._digest_table = True
        return ledger

    @classmethod
    def open(cls, db_path: str, contract: Contract,
             verify_history: bool = False,
             before_commit_hook=None) -> "Ledger":
        if not os.path.exists(db_path):
            raise LedgerError(f"ledger database {db_path!r} does not exist")
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        ledger = cls(conn, contract, db_path, before_commit_hook)
        try:
            ledger._load_and_verify(verify_history)
        except BaseException:
            conn.close()
            raise
        return ledger

    # -- schema ----------------------------------------------------------
    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE initial_slot(slot_id TEXT PRIMARY KEY, "
            "candidate_id TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE slot_state(slot_id TEXT PRIMARY KEY, "
            "candidate_id TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE flow(seq INTEGER PRIMARY KEY, changes TEXT NOT NULL, "
            "newly_false TEXT NOT NULL, recovered TEXT NOT NULL)")
        Ledger._create_digest_table(conn)

    @staticmethod
    def _create_digest_table(conn: sqlite3.Connection) -> None:
        """The additive per-sequence digest metadata (idempotent)."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS flow_digest("
            "seq INTEGER PRIMARY KEY, digest TEXT NOT NULL)")

    @staticmethod
    def _has_digest_table(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='flow_digest'").fetchone()
        return row is not None

    # -- meta ------------------------------------------------------------
    def _read_meta(self) -> Dict[str, str]:
        return dict(self.conn.execute("SELECT key, value FROM meta"))

    def _write_meta(self, values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    # -- reopen ----------------------------------------------------------
    def _load_and_verify(self, verify_history: bool) -> None:
        meta = self._read_meta()
        if meta.get("format") != FORMAT:
            raise LedgerError(f"unsupported ledger format {meta.get('format')!r}")
        if meta.get("contract_id") != self.contract.contract_id:
            raise LedgerError(
                f"ledger belongs to contract {meta.get('contract_id')!r}, "
                f"not {self.contract.contract_id!r}")
        if meta.get("contract_digest") != self._contract_digest:
            raise LedgerError(
                "contract content differs from the one the ledger was "
                "initialized with; contracts cannot be changed")
        try:
            stored_seq = int(meta["seq"])
            stored_sum = int(meta["selection_sum"], 16)
            stored_digest = meta["state_digest"]
        except (KeyError, ValueError) as exc:
            raise LedgerError(f"ledger meta is malformed: {exc}") from None
        if stored_seq < 0:
            raise LedgerError("ledger sequence is negative")

        initial = dict(self.conn.execute(
            "SELECT slot_id, candidate_id FROM initial_slot"))
        state = dict(self.conn.execute(
            "SELECT slot_id, candidate_id FROM slot_state"))
        for label, pairs in (("initial snapshot", initial),
                             ("current selection", state)):
            missing = sorted(set(self._slots) - set(pairs))
            extra = sorted(set(pairs) - set(self._slots))
            if missing or extra:
                raise LedgerError(
                    f"ledger {label} does not cover the contract slots "
                    f"(missing={missing}, extra={extra})")
            for slot_id, candidate_id in pairs.items():
                if candidate_id not in self._candidates[slot_id]:
                    raise LedgerError(
                        f"ledger {label} names undeclared candidate "
                        f"{candidate_id!r} for slot {slot_id!r}")

        # The flow must be exactly the contiguous prefix 1..seq.  This is a
        # bounded aggregate query, not a replay.
        count, maximum, minimum = self.conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq),0), COALESCE(MIN(seq),0) "
            "FROM flow").fetchone()
        if stored_seq == 0:
            if count != 0:
                raise LedgerError(
                    f"ledger sequence is 0 but {count} flow row(s) exist")
        elif count != stored_seq or maximum != stored_seq or minimum != 1:
            raise LedgerError(
                f"ledger flow is not the completed prefix 1..{stored_seq} "
                f"(rows={count}, min={minimum}, max={maximum})")

        # The optional digest metadata is derived: a ledger whose table
        # simply lags behind (e.g. transactions written before the table
        # existed or by a writer that does not maintain it) opens exactly
        # as before, and the backfill extends and revalidates it.  What may
        # not open is a table that is non-contiguous, ahead of the flow, or
        # fully covering yet disagreeing with the committed state digest.
        self._digest_table = self._has_digest_table(self.conn)
        if self._digest_table:
            dcount, dmaximum = self.conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(seq),0) "
                "FROM flow_digest").fetchone()
            if dcount != dmaximum or dmaximum > stored_seq:
                raise LedgerError(
                    f"ledger digest metadata is inconsistent with the "
                    f"committed prefix 1..{stored_seq} (rows={dcount}, "
                    f"max={dmaximum})")
            if dmaximum == stored_seq and stored_seq:
                row = self.conn.execute(
                    "SELECT digest FROM flow_digest WHERE seq=?",
                    (stored_seq,)).fetchone()
                if row is None or row[0] != stored_digest:
                    raise LedgerError(
                        "ledger digest metadata does not commit to the "
                        "stored state digest")

        selection_sum = _selection_sum(self._contract_digest, state)
        if selection_sum != stored_sum:
            raise LedgerError(
                "current selection does not match the digest committed with "
                f"sequence {stored_seq}")

        assignment = {slot_id: self._candidates[slot_id][candidate_id]
                      for slot_id, candidate_id in state.items()}
        reference = evaluate_rules(self.contract, assignment)
        stated_rules = json.loads(meta["rules_after"])
        if stated_rules != reference:
            raise LedgerError(
                "stored rule state disagrees with independent evaluation of "
                "the current selection")

        if verify_history:
            current_ids = dict(initial)
            initial_digest = hashlib.sha256(
                _canon({"contract": self._contract_digest,
                        "initial": sorted(initial.items())})).hexdigest()
            if initial_digest != meta.get("initial_digest"):
                raise LedgerError("initial snapshot digest does not match")
            digest = initial_digest
            chain: Dict[int, str] = {}
            flow_rows = self.conn.execute(
                "SELECT seq, changes, newly_false, recovered FROM flow "
                "ORDER BY seq").fetchall()
            for index, (seq, changes_json, false_json, recovered_json) in \
                    enumerate(flow_rows, 1):
                if seq != index:
                    raise LedgerError(
                        f"ledger flow has a sequence gap at {index} "
                        f"(found {seq})")
                changes = _decode_changes(changes_json, self._slots,
                                          self._candidates, f"flow seq {seq}")
                newly_false = _decode_rule_list(false_json, self.contract,
                                                f"flow seq {seq}")
                recovered = _decode_rule_list(recovered_json, self.contract,
                                              f"flow seq {seq}")
                for slot_id, candidate_id in changes:
                    current_ids[slot_id] = candidate_id
                digest = _step_digest(digest, seq, _canon_changes(changes),
                                      newly_false, recovered)
                chain[seq] = digest
            if current_ids != state:
                raise LedgerError(
                    "stored selection is not the result of the recorded flow")
            if digest != stored_digest:
                raise LedgerError("ledger digest chain does not match")
            if self._digest_table:
                # Stored rows must be a prefix of the audited chain; a table
                # that merely lags behind is stale metadata, not corruption.
                for seq, stored in self.conn.execute(
                        "SELECT seq, digest FROM flow_digest"):
                    if chain.get(seq) != stored:
                        raise LedgerError(
                            "ledger digest metadata disagrees with the "
                            "audited digest chain")

        self.assignment = assignment
        self.tracker = RuleTracker(self.contract, assignment)
        if self.tracker.truths() != reference:
            raise LedgerError(
                "incremental rule indexes disagree with evaluate_rules on reopen")
        self.seq = stored_seq
        self._selection_sum = stored_sum
        self._state_digest = stored_digest

    # -- reading ---------------------------------------------------------
    @property
    def current_seq(self) -> int:
        return self.seq

    def selection(self) -> Dict[str, str]:
        return {slot_id: self.assignment[slot_id].candidate_id
                for slot_id in sorted(self.assignment)}

    def selected_rows(self) -> Dict[str, Dict[str, Any]]:
        return {slot_id: dict(self.assignment[slot_id].row)
                for slot_id in sorted(self.assignment)}

    def rules(self) -> Dict[str, bool]:
        assert self.tracker is not None
        return self.tracker.truths()

    def false_rules(self) -> List[str]:
        return sorted(rid for rid, value in self.rules().items() if not value)

    def flow(self) -> List[FlowEntry]:
        rows = self.conn.execute(
            "SELECT seq, changes, newly_false, recovered FROM flow ORDER BY seq"
        ).fetchall()
        return [FlowEntry(seq,
                          _decode_changes(changes, self._slots,
                                          self._candidates, f"flow seq {seq}"),
                          _decode_rule_list(false, self.contract,
                                            f"flow seq {seq}"),
                          _decode_rule_list(recovered, self.contract,
                                            f"flow seq {seq}"))
                for seq, changes, false, recovered in rows]

    def status(self) -> Dict[str, Any]:
        return {
            "contract_id": self.contract.contract_id,
            "seq": self.seq,
            "selection": self.selection(),
            "rules": self.rules(),
            "false_rules": self.false_rules(),
        }

    # -- writing ---------------------------------------------------------
    def apply(self, transaction: Transaction) -> ApplyResult:
        tracker = self.tracker
        assert tracker is not None
        changes = transaction.changes
        submitted = transaction.mapping

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if transaction.seq <= self.seq:
                return self._replay_duplicate(transaction)

            expected = self.seq + 1
            if transaction.seq != expected:
                raise InputError(
                    f"sequence gap: expected {expected}, got {transaction.seq}")

            # Cheap single-writer guard: the durable row must still agree with
            # the in-memory selection for every submitted slot.
            for slot_id in submitted:
                row = self.conn.execute(
                    "SELECT candidate_id FROM slot_state WHERE slot_id=?",
                    (slot_id,)).fetchone()
                if row is None or row[0] != self.assignment[slot_id].candidate_id:
                    raise LedgerError(
                        f"ledger state changed underneath this writer at slot "
                        f"{slot_id!r}; reopen the ledger")

            updates: List[Tuple[str, Optional[Candidate], Optional[Candidate]]] = []
            for slot_id, candidate_id in changes:
                old = self.assignment[slot_id]
                if old.candidate_id != candidate_id:
                    updates.append(
                        (slot_id, old, self._candidates[slot_id][candidate_id]))

            newly_false, recovered = tracker.apply(updates)
            ordered = tuple(sorted(submitted.items()))
            canonical = _canon_changes(ordered)

            new_sum = self._selection_sum
            for slot_id, old, new in updates:
                assert new is not None
                new_sum -= _slot_term(self._contract_digest, slot_id,
                                      old.candidate_id)
                new_sum += _slot_term(self._contract_digest, slot_id,
                                      new.candidate_id)
            new_sum %= _MOD
            digest = _step_digest(self._state_digest, transaction.seq,
                                  canonical, newly_false, recovered)
            rules_after = _canon_json(tracker.truths())

            for slot_id, _old, new in updates:
                assert new is not None
                self.conn.execute(
                    "UPDATE slot_state SET candidate_id=? WHERE slot_id=?",
                    (new.candidate_id, slot_id))
            self.conn.execute(
                "INSERT INTO flow(seq, changes, newly_false, recovered) "
                "VALUES (?,?,?,?)",
                (transaction.seq, _canon_json(canonical),
                 _canon_json(newly_false), _canon_json(recovered)))
            if self._digest_table:
                self.conn.execute(
                    "INSERT INTO flow_digest(seq, digest) VALUES (?,?)",
                    (transaction.seq, digest))
            self._write_meta({
                "seq": transaction.seq,
                "selection_sum": f"{new_sum:064x}",
                "state_digest": digest,
                "rules_after": rules_after,
            })
            if self._before_commit is not None:
                self._before_commit(self, transaction)
            self.conn.execute("COMMIT")
        except BaseException:
            try:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
            finally:
                self._reload_from_db()
            raise

        for slot_id, _old, new in updates:
            assert new is not None
            self.assignment[slot_id] = new
        self.seq = transaction.seq
        self._selection_sum = new_sum
        self._state_digest = digest
        return ApplyResult(
            seq=transaction.seq, status="applied",
            changes=tuple(sorted(submitted.items())),
            newly_false=tuple(newly_false), recovered=tuple(recovered))

    def _replay_duplicate(self, transaction: Transaction) -> ApplyResult:
        row = self.conn.execute(
            "SELECT seq, changes, newly_false, recovered FROM flow WHERE seq=?",
            (transaction.seq,)).fetchone()
        if row is None:
            raise LedgerError(
                f"sequence {transaction.seq} is not in the ledger's flow")
        stored = _decode_changes(row[1], self._slots, self._candidates,
                                 f"flow seq {row[0]}")
        if dict(stored) != transaction.mapping:
            raise InputError(
                f"sequence {transaction.seq} was already recorded with "
                f"different content: "
                f"{_describe_diff(dict(stored), transaction.mapping)}")
        self.conn.execute("COMMIT")
        return ApplyResult(
            seq=row[0], status="skipped",
            changes=tuple(sorted(stored)),
            newly_false=tuple(json.loads(row[2])),
            recovered=tuple(json.loads(row[3])))

    def _reload_from_db(self) -> None:
        meta = self._read_meta()
        state = dict(self.conn.execute(
            "SELECT slot_id, candidate_id FROM slot_state"))
        assignment = {slot_id: self._candidates[slot_id][candidate_id]
                      for slot_id, candidate_id in state.items()}
        self.assignment = assignment
        self.tracker = RuleTracker(self.contract, assignment)
        self.seq = int(meta["seq"])
        self._selection_sum = int(meta["selection_sum"], 16)
        self._state_digest = meta["state_digest"]
        self._digest_table = self._has_digest_table(self.conn)

    # -- additive digest metadata ----------------------------------------
    def backfill_flow_digests(self) -> int:
        """Add or extend the per-sequence digest metadata; rows added.

        This is the sanctioned one-time metadata upgrade for ledgers created
        before ``flow_digest`` existed: it recomputes the digest chain from
        the recorded flow (starting at the committed initial digest, or at
        the last stored digest when the table is partially filled) and
        stores one row per committed sequence.  Nothing is stored unless the
        recomputed chain reproduces the committed state digest, and the
        whole backfill is one atomic write.  Re-running it on an up-to-date
        ledger adds nothing and simply re-validates the chain.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_digest_table(self.conn)
            start = self.conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM flow_digest").fetchone()[0]
            initial = dict(self.conn.execute(
                "SELECT slot_id, candidate_id FROM initial_slot"))
            initial_digest = hashlib.sha256(
                _canon({"contract": self._contract_digest,
                        "initial": sorted(initial.items())})).hexdigest()
            if initial_digest != self._read_meta().get("initial_digest"):
                raise LedgerError(
                    "initial snapshot does not match the committed initial "
                    "digest")
            if start:
                row = self.conn.execute(
                    "SELECT digest FROM flow_digest WHERE seq=?",
                    (start,)).fetchone()
                if row is None:
                    raise LedgerError(
                        "ledger digest metadata is not contiguous")
                digest = row[0]
            else:
                digest = initial_digest
            rows = self.conn.execute(
                "SELECT seq, changes, newly_false, recovered FROM flow "
                "WHERE seq>? ORDER BY seq", (start,)).fetchall()
            added = 0
            for seq, changes_json, false_json, recovered_json in rows:
                if seq != start + added + 1:
                    raise LedgerError(
                        f"ledger flow has a sequence gap at "
                        f"{start + added + 1} (found {seq})")
                changes = _decode_changes(changes_json, self._slots,
                                          self._candidates, f"flow seq {seq}")
                newly_false = _decode_rule_list(false_json, self.contract,
                                                f"flow seq {seq}")
                recovered = _decode_rule_list(recovered_json, self.contract,
                                              f"flow seq {seq}")
                digest = _step_digest(digest, seq, _canon_changes(changes),
                                      newly_false, recovered)
                self.conn.execute(
                    "INSERT INTO flow_digest(seq, digest) VALUES (?,?)",
                    (seq, digest))
                added += 1
            if start + added != self.seq:
                raise LedgerError(
                    "ledger flow does not cover the committed sequence; "
                    "reopen the ledger")
            if digest != self._state_digest:
                raise LedgerError(
                    "recomputed digest chain does not match the committed "
                    "state digest")
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        self._digest_table = True
        return added

    # -- JSONL -----------------------------------------------------------
    def apply_payload(self, payload: Any,
                      where: str = "transaction") -> ApplyResult:
        return self.apply(parse_transaction(
            payload, self.contract, where, self._candidate_ids))

    def replay(self, lines: Iterable[str],
               source: str = "<input>") -> ReplayResult:
        """Consume JSONL lines, stopping at (and reporting) the first bad line.

        Already-recorded sequences are skipped when their slot->candidate
        mapping matches; a conflicting mapping, a gap or any invalid value
        stops the batch.  Every transaction accepted before the stop stays
        committed.
        """
        result = ReplayResult(results=[])
        for line_no, raw in enumerate(lines, 1):
            try:
                if not raw.strip():
                    raise InputError("empty line")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise InputError(f"invalid JSON: {exc.msg}") from None
                tx = parse_transaction(payload, self.contract,
                                       slot_index=self._candidate_ids)
                outcome = self.apply(tx)
            except LedgerError as exc:
                message = exc.reason if isinstance(exc, InputError) else str(exc)
                result.error = InputError(message, line_no, source)
                break
            result.results.append(outcome)
            if outcome.status == "applied":
                result.applied += 1
            else:
                result.skipped += 1
        return result

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class _immediate:
    """Context manager for one atomic write transaction."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.execute("COMMIT")
        elif self.conn.in_transaction:
            self.conn.execute("ROLLBACK")
        return False


# ---------------------------------------------------------------------------
# decoding stored rows


def _decode_changes(raw: str, slots, candidates, where: str) \
        -> Tuple[Tuple[str, str], ...]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"ledger {where}: malformed changes ({exc.msg})") from None
    if not isinstance(items, list):
        raise LedgerError(f"ledger {where}: changes must be a list")
    out: List[Tuple[str, str]] = []
    seen = set()
    for item in items:
        if (not isinstance(item, list) or len(item) != 2
                or not all(isinstance(v, str) for v in item)):
            raise LedgerError(f"ledger {where}: malformed change entry")
        slot_id, candidate_id = item
        if slot_id not in slots or candidate_id not in candidates[slot_id]:
            raise LedgerError(
                f"ledger {where}: change names undeclared slot/candidate "
                f"({slot_id!r}, {candidate_id!r})")
        if slot_id in seen:
            raise LedgerError(f"ledger {where}: duplicate slot in changes")
        seen.add(slot_id)
        out.append((slot_id, candidate_id))
    return tuple(out)


def _decode_rule_list(raw: str, contract: Contract,
                      where: str) -> Tuple[str, ...]:
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"ledger {where}: malformed rule list ({exc.msg})") from None
    known = {rule.id for rule in contract.rules}
    if not isinstance(items, list) or not all(isinstance(v, str) for v in items):
        raise LedgerError(f"ledger {where}: malformed rule list")
    if len(set(items)) != len(items):
        raise LedgerError(f"ledger {where}: duplicate rule id in a flow list")
    unknown = [rid for rid in items if rid not in known]
    if unknown:
        raise LedgerError(f"ledger {where}: unknown rule id(s) {unknown}")
    if list(items) != sorted(items):
        raise LedgerError(f"ledger {where}: rule list is not sorted by id")
    return tuple(items)


def _describe_diff(stored: Mapping[str, str], new: Mapping[str, str]) -> str:
    parts = []
    for slot_id in sorted(set(stored) | set(new)):
        if stored.get(slot_id) != new.get(slot_id):
            parts.append(
                f"{slot_id}: recorded {stored.get(slot_id)!r}, "
                f"resubmitted {new.get(slot_id)!r}")
    return "; ".join(parts) if parts else "slot sets differ"


def load_fixture(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as exc:
        raise LedgerError(f"cannot read fixture: {exc}") from None
    except json.JSONDecodeError as exc:
        raise LedgerError(
            f"invalid JSON in fixture {path}: {exc}") from None
    if not isinstance(doc, dict):
        raise LedgerError("fixture must be a JSON object")
    return doc


def init_ledger(db_path: str, contract: Contract,
                fixture: Mapping[str, Any],
                before_commit_hook=None) -> Ledger:
    return Ledger.initialize(db_path, contract, fixture, before_commit_hook)


def open_ledger(db_path: str, contract: Contract,
                verify_history: bool = False,
                before_commit_hook=None) -> Ledger:
    return Ledger.open(db_path, contract, verify_history, before_commit_hook)
