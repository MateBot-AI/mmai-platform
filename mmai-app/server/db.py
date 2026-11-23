"""Database layer: SQLite by default, PostgreSQL when DATABASE_URL is set (T17 production hosting, managed Postgres).

The whole app talks to one connection object with the sqlite3 interface (execute / fetchone / fetchall / commit / rollback), so
the request handlers, the T8 scheduler and the AI queue need no changes. `PgConnection` gives a psycopg (v3) connection that
same interface and translates the handful of SQLite idioms the code uses:
  ?                    -> %s                        INSERT OR IGNORE      -> INSERT ... ON CONFLICT DO NOTHING
  last_insert_rowid()  -> lastval()                 INSERT OR REPLACE     -> INSERT ... ON CONFLICT (unique key) DO UPDATE
  AUTOINCREMENT / REAL -> IDENTITY / DOUBLE PRECISION   ORDER BY ... DESC -> DESC NULLS LAST (SQLite sorts NULLs last on DESC)
  PRAGMA table_info    -> information_schema        Python bool / date    -> int / ISO text (columns are INTEGER / TEXT)
Every statement runs inside a SAVEPOINT, so a failed statement does not poison the open transaction (SQLite behaviour).
DDL stays portable: only TEXT / INTEGER / REAL columns, ISO-8601 dates as TEXT, JSON lists as TEXT. T1: holds several thousand enquiries.

Environment: DATABASE_URL=postgresql://user:pass@host:port/db?sslmode=require (DigitalOcean managed Postgres); when unset,
MMAI_DB (SQLite file path, default server/data/mmai.db) is used, as before, so local runs and the tests keep working.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timezone

from .apartment_features import KEYS as _FEATURE_KEYS

DB_PATH = os.environ.get("MMAI_DB", os.path.join(os.path.dirname(__file__), "data", "mmai.db"))
_lock = threading.RLock()
_conn = None


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def is_postgres() -> bool:
    return database_url().startswith(("postgres://", "postgresql://"))


def backend_name() -> str:
    return "postgres" if is_postgres() else "sqlite"


# ---------------------------------------------------------------------------
# T5 retention guard (8 Nov 2026): one DB-enforced lock shared by every writer of generated text about people and every
# deleter / scrubber of people, so a deletion can never commit between a writer's identity check and its history insert on
# ANOTHER connection (root's two-connection PostgreSQL probe). Protects cooperating application connections (web process,
# scheduler, AI queue, tools using db.connect, tests); raw maintenance SQL bypasses it and must stop the workers or use it.
# ---------------------------------------------------------------------------
RETENTION_LOCK_KEY = 0x4D4D41495435          # one global bigint advisory key ("MMAIT5"); PostgreSQL only
RETENTION_LOCK_TIMEOUT_MS = 10_000           # PG lock_timeout for the acquisition; SQLite uses the connection's busy timeout
GUARD_EVENTS: list = []                      # test hook: callables (event, c, label); events entered/acquired/committed/rolled_back/exit
_log = logging.getLogger("mmai.db")


class RetentionGuardError(RuntimeError):
    """Base: never carries row data or model text."""


class DirtyConnectionAtGuardEntry(RetentionGuardError):
    """R1: a write was pending when an OUTER guard was entered; the pending work is left untouched for its owner."""


class CommitInsideRetentionGuard(RetentionGuardError):
    """R3: c.commit()/c.rollback() called while a guard is active; the DB lock is not released."""


class ConnectionLostMidOperation(RetentionGuardError):
    """R4: the server connection was lost with pending work or an active guard; nothing is replayed on a new session.
    `outcome_unknown` is True when the loss happened during COMMIT: the step may or may not have been saved, so the caller
    reconciles from the current database state in a FRESH guarded operation (never assumes "nothing written")."""

    def __init__(self, msg: str, outcome_unknown: bool = False):
        super().__init__(msg)
        self.outcome_unknown = outcome_unknown


class RetentionLockTimeout(RetentionGuardError):
    """The DB lock was not obtained within the timeout; nothing was written."""


class GuardRolledBack(RetentionGuardError):
    """The outer guard found the step rollback-only (an inner guard failed and the failure was caught): nothing was saved."""


def _emit(event: str, c, label: str) -> None:
    """Observer hooks are informational: an ordinary exception in a hook is logged and never changes the guard's outcome or masks
    the error being propagated. A BaseException (KeyboardInterrupt, a test abort) propagates; the guard cleans up first."""
    for fn in list(GUARD_EVENTS):
        try:
            fn(event, c, label)
        except Exception as e:
            _log.warning("retention guard observer failed on %s (%s): %s", event, label, type(e).__name__)


# Direct SQL transaction control (`c.execute("COMMIT")` etc.) would bypass the commit discipline exactly like c.commit(); refused
# and poisoning while a guard is active. ROLLBACK TO / SAVEPOINT / RELEASE are not matched (statement savepoints are the
# adapter's own business and never end the transaction). Inventory (8 Nov 2026): the only direct transaction-control SQL in
# server/ outside db.py is review_loop._begin_guarded's SQLite `BEGIN IMMEDIATE`, issued only when no transaction is open
# (never inside a guard, which has already begun one). tools/ scripts run on their own connections, outside any guard.
_TX_CONTROL = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK(?!\s+TO\b)|PREPARE\s+TRANSACTION|SET\s+TRANSACTION|VACUUM)\b", re.I)


def _refuse_tx_control_sql(c, sql) -> None:
    if getattr(c, "_guard_depth", 0) and isinstance(sql, str) and _TX_CONTROL.match(sql):
        raise _forbidden_control(c, f"direct transaction control SQL ({sql.strip().split()[0].upper()})")


def _forbidden_control(c, what: str) -> CommitInsideRetentionGuard:
    """A forbidden transaction-control attempt inside a guard POISONS the step (rollback-only) before raising, so a helper that
    catches the error after writing cannot let the outer guard commit a partial step. Expected SQL errors (constraint violations)
    are different: they are undone by the statement savepoint and leave the step committable."""
    c._guard_rollback_only = True
    return CommitInsideRetentionGuard(f"{what} inside retention guard")


def guarded(c) -> bool:
    """True while the CALLING thread holds a retention guard on this connection. On the shared connection another thread may be
    inside its own guard (it owns the RLock for the duration); that is not this thread's guard, so this thread is free to do
    model/network work and `maybe_commit` must wait for the RLock and commit as usual rather than skip."""
    return int(getattr(c, "_guard_depth", 0) or 0) > 0 and getattr(c, "_guard_owner", None) == threading.get_ident()


def guard_active(c) -> bool:
    """True while ANY thread holds a retention guard on this connection (call under the RLock to act on it)."""
    return int(getattr(c, "_guard_depth", 0) or 0) > 0


def assert_not_guarded(c, what: str) -> None:
    """No model, SMTP or network work inside a guard held by the calling thread: the DB lock would be held across a slow call.
    A guard held by ANOTHER thread on the shared connection is irrelevant here (it never turns an unrelated staff edit into a
    synthetic AI outage)."""
    if guarded(c):
        raise RetentionGuardError(f"{what} inside retention guard")


def maybe_commit(c) -> None:
    """Commit unless a guard is active (then the outer guard commits the whole step once)."""
    if not guarded(c):
        c.commit()


class retention_guard:
    """Context manager (synchronous only; never across an await).
    OUTER entry: takes the connection's RLock, requires a clean connection (no pending write - otherwise refuses, in production
    too, without committing or rolling back the unknown work), then takes the DB lock first in the transaction: PostgreSQL
    `pg_advisory_xact_lock(RETENTION_LOCK_KEY)` under a bounded lock_timeout, SQLite `BEGIN IMMEDIATE`. While active, reads are
    never auto-committed, c.commit()/c.rollback() raise, and a lost server connection raises instead of reconnecting.
    NESTED entry (same thread): no statement; the outer owns the transaction. An inner exception marks the step rollback-only.
    OUTER exit: commit on success (releases the lock) or rollback on exception / rollback-only; flags reset in `finally`.
    Forbidden inside a guard (they would bypass the commit discipline): c.commit(), c.rollback(), `executescript`, and the
    native `with connection:` block. Each raises CommitInsideRetentionGuard AND poisons the step (rollback-only), so a helper
    that catches the error cannot let the outer guard commit a partial step. A second connection object of the same thread is
    not covered by the RLock (cross-connection safety comes from the DB lock). Observer hooks (GUARD_EVENTS) are informational:
    an ordinary exception in a hook is logged; a BaseException propagates after the guard has cleaned up (see _emit)."""

    def __init__(self, c, label: str = ""):
        self.c = c
        self.label = label or "operation"
        self._outer = False

    def __enter__(self):
        c = self.c
        lock = getattr(c, "_mmai_lock", None)
        if lock is None or not hasattr(c, "_raw_commit"):
            # fail closed: only the app's own connection classes (db.Connection / db.PgConnection) carry the lock and the raw
            # commit/rollback hooks the guard relies on; a bare sqlite3 connection or a recording double is never guarded silently
            raise RetentionGuardError(f"retention guard needs a db.Connection or db.PgConnection, got {type(c).__name__}")
        lock.acquire()
        try:
            if guarded(c):
                c._guard_depth += 1
                return c
            backend = getattr(c, "backend", "sqlite")
            if backend == "postgres":
                c._ensure_open()   # a session lost earlier is re-opened here, BEFORE the guard takes ownership of the transaction
            dirty = bool(getattr(c, "_dirty", False)) if backend == "postgres" else bool(getattr(c, "in_transaction", False))
            if dirty:
                _log.warning("retention guard refused: a write was pending at entry (%s)", self.label)
                raise DirtyConnectionAtGuardEntry(f"pending write at guard entry ({self.label})")
            self._outer = True
            c._guard_depth = 1
            c._guard_owner = threading.get_ident()
            c._hold_tx = True
            c._guard_rollback_only = False
            try:
                _emit("entered", c, self.label)
                if backend == "postgres":
                    c._acquire_retention_lock()
                else:
                    try:
                        c._raw_execute("BEGIN IMMEDIATE")
                    except sqlite3.OperationalError as e:
                        if "locked" in str(e).lower() or "busy" in str(e).lower():
                            raise RetentionLockTimeout(f"database busy ({self.label})") from e
                        raise
                _emit("acquired", c, self.label)
            except BaseException:
                # whatever failed between taking ownership and returning (lock acquisition, an observer abort): end any
                # transaction this entry opened so no DB lock stays held, reset the flags, release the RLock (outer handler) and
                # re-raise the ORIGINAL error - a rollback failure here is logged, never substituted for it
                try:
                    c._raw_rollback()
                except Exception as re:
                    _log.warning("retention guard entry cleanup rollback failed (%s): %s", self.label, type(re).__name__)
                c._guard_depth, c._hold_tx, c._guard_rollback_only, c._guard_owner = 0, False, False, None
                self._outer = False
                raise
            return c
        except BaseException:
            lock.release()
            raise

    def __exit__(self, et, ev, tb):
        c = self.c
        lock = c._mmai_lock
        try:
            if not self._outer:
                if et is not None:
                    c._guard_rollback_only = True
                c._guard_depth = max(0, int(getattr(c, "_guard_depth", 1)) - 1)
                return False
            try:
                if et is not None or getattr(c, "_guard_rollback_only", False):
                    try:
                        c._raw_rollback()
                    except Exception as re:
                        _log.warning("retention guard rollback failed (%s): %s", self.label, type(re).__name__)
                        _emit("rollback_failed", c, self.label)   # outcome unknown; never reported as a clean rollback
                        if et is None:
                            raise
                    else:
                        _emit("rolled_back", c, self.label)        # only when the rollback itself succeeded
                    if et is None:
                        raise GuardRolledBack(f"step rolled back: an inner guarded operation failed or forbidden transaction control was attempted ({self.label})")
                    return False   # the original exception propagates unmasked
                try:
                    c._raw_commit()
                except BaseException:
                    # a failed commit must not leave an open transaction or a held lock behind the cleared flags; the session is
                    # already closed when the loss was mid-commit. The outcome is UNKNOWN (never 'rolled_back'): the caller
                    # reconciles from the database in a fresh guarded step.
                    try:
                        c._raw_rollback()
                    except Exception:
                        pass
                    _emit("commit_failed", c, self.label)
                    raise
                _emit("committed", c, self.label)   # only after a successful commit
                return False
            finally:
                c._guard_depth, c._hold_tx, c._guard_rollback_only, c._guard_owner = 0, False, False, None
                self._outer = False
                _emit("exit", c, self.label)
        finally:
            lock.release()


whole_operation = retention_guard   # every former whole_operation site is a T5 check+write or cleanup+delete pair


class bounded_write:
    """ONE short, owner-fenced write (a lease renewal / heartbeat) that must neither wait long nor touch anyone else's work.
    Synchronous only. Compared with `transaction`, which commits whatever the shared connection had pending:
    - the connection's RLock is taken with a bound (another thread holding it longer -> RetentionLockTimeout, nothing run);
    - a pending write another caller left on the connection is refused WITHOUT committing or rolling it back
      (DirtyConnectionAtGuardEntry; the owner of that work decides its fate);
    - the database lock wait is bounded for this statement only: PostgreSQL `SET LOCAL lock_timeout` (transaction-scoped),
      SQLite `PRAGMA busy_timeout` for the block (restored after); a wait past the bound -> RetentionLockTimeout, rolled back;
    - commits on a clean exit, rolls back on any exception; connection flags are never changed.
    Inside an active retention guard of the same thread it is NESTED: no statement of its own, no commit; the guard owns the
    transaction and an exception marks the step rollback-only (as `transaction` does). It is not a substitute for the guard:
    multi-step business writes keep using retention_guard."""

    def __init__(self, c, label: str = "", timeout_ms: int | None = None):
        self.c = c
        self.label = label or "bounded write"
        t = int(timeout_ms if timeout_ms is not None else RETENTION_LOCK_TIMEOUT_MS)
        if t <= 0:
            # a non-positive bound is not "no wait": PostgreSQL lock_timeout 0 DISABLES the limit; refused rather than clamped silently
            raise ValueError("bounded write needs a positive timeout in milliseconds")
        self.timeout_ms = t
        self._nested = False
        self._held = False
        self._busy_before = None
        self._own_tx = False     # True once THIS entry has started its own transaction (PG SET LOCAL); never set on a dirty connection

    def __enter__(self):
        c = self.c
        lock = getattr(c, "_mmai_lock", None)
        if lock is None or not hasattr(c, "_raw_commit"):
            raise RetentionGuardError(f"bounded write needs a db.Connection or db.PgConnection, got {type(c).__name__}")
        if not lock.acquire(timeout=max(0.001, self.timeout_ms / 1000.0)):
            raise RetentionLockTimeout(f"connection busy ({self.label})")
        self._held = True
        try:
            if guarded(c):
                self._nested = True
                return c
            backend = getattr(c, "backend", "sqlite")
            if backend == "postgres":
                c._ensure_open()
            dirty = bool(getattr(c, "_dirty", False)) if backend == "postgres" else bool(getattr(c, "in_transaction", False))
            if dirty:
                _log.warning("bounded write refused: a write was pending at entry (%s)", self.label)
                raise DirtyConnectionAtGuardEntry(f"pending write at bounded write entry ({self.label})")
            if backend == "postgres":
                self._own_tx = True
                c.execute(f"SET LOCAL lock_timeout = '{self.timeout_ms}ms'")   # opens the transaction; scoped to it
            else:
                self._busy_before = c._raw_execute("PRAGMA busy_timeout").fetchone()[0]
                c._raw_execute(f"PRAGMA busy_timeout = {self.timeout_ms}")
            return c
        except BaseException:
            if self._own_tx:
                # only the transaction THIS entry opened is undone (the connection was verified clean just before); a dirty-at-entry
                # refusal never reaches here, so another caller's pending work is never rolled back by this path
                try:
                    c._raw_rollback()
                except Exception:   # noqa: BLE001
                    _log.warning("bounded write entry cleanup: rollback_failed (%s)", self.label)
                self._own_tx = False
            self._release()
            raise

    def _release(self):
        if self._busy_before is not None:
            try:
                self.c._raw_execute(f"PRAGMA busy_timeout = {int(self._busy_before)}")
            except Exception:   # noqa: BLE001
                pass
            self._busy_before = None
        if self._held:
            self._held = False
            self.c._mmai_lock.release()

    def __exit__(self, et, ev, tb):
        c = self.c
        try:
            if self._nested:
                if et is not None:
                    c._guard_rollback_only = True
                return False
            if et is None:
                try:
                    c.commit()
                except BaseException:
                    try:
                        c._raw_rollback()
                    except Exception:   # noqa: BLE001
                        pass
                    raise
                return False
            try:
                c._raw_rollback()
            except Exception:   # noqa: BLE001
                _log.warning("bounded write: rollback_failed (%s)", self.label)   # closed token only; never the error's class or text
            if _is_lock_wait_error(ev):
                raise RetentionLockTimeout(f"database lock not obtained in time ({self.label})") from ev
            return False
        finally:
            self._release()


def _is_lock_wait_error(e: BaseException) -> bool:
    """A bounded wait that ran out: PostgreSQL lock_timeout (LockNotAvailable, SQLSTATE 55P03) or SQLite 'database is locked/busy'."""
    if isinstance(e, sqlite3.OperationalError):
        m = str(e).lower()
        return "locked" in m or "busy" in m
    try:
        import psycopg
        return isinstance(e, psycopg.errors.LockNotAvailable)
    except Exception:   # noqa: BLE001
        return False


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LockedCursor(sqlite3.Cursor):
    """Cursor whose row fetching also holds the connection lock. `Connection.execute` only steps the first row under the
    lock; `fetchall()` / iteration step the rest afterwards. On the shared connection another thread's statement or
    commit in that gap made fetches return empty, partial or NULL-filled rows (seen as `weekly_max` None in a HomeBot
    detail during concurrent identical submissions, and as a known enquiry id "not found"). Fetching under the same
    re-entrant lock closes the gap; a thread never holds the lock across a request, so nothing waits long."""

    def execute(self, *a, **kw):
        with self.connection._mmai_lock:
            return super().execute(*a, **kw)

    def executemany(self, *a, **kw):
        with self.connection._mmai_lock:
            return super().executemany(*a, **kw)

    def fetchone(self):
        with self.connection._mmai_lock:
            return super().fetchone()

    def fetchmany(self, *a, **kw):
        with self.connection._mmai_lock:
            return super().fetchmany(*a, **kw)

    def fetchall(self):
        with self.connection._mmai_lock:
            return super().fetchall()

    def __next__(self):
        with self.connection._mmai_lock:
            return super().__next__()

    # Scope: the app reaches cursors only through Connection.execute/executemany; the one executescript call (init_schema)
    # goes through the locked Connection.executescript. cursor().executescript has no caller and is deliberately not wrapped.


class Connection(sqlite3.Connection):
    """sqlite3 connection shared by the request handlers, the T8 scheduler and the background matching thread.
    Python's sqlite3 issues an implicit BEGIN on the first write of a transaction after checking that none is open; two
    threads doing that at once hit "cannot start a transaction within a transaction". One re-entrant lock around every
    statement, every row fetch (LockedCursor) and commit/rollback removes that race (statements themselves are serialised
    by SQLite already)."""

    backend = "sqlite"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._mmai_lock = threading.RLock()
        self._guard_depth = 0
        self._guard_owner = None        # thread ident of the guard holder (see guarded())
        self._hold_tx = False
        self._guard_rollback_only = False

    def cursor(self, factory=LockedCursor):
        return super().cursor(factory)

    # The C implementation of Connection.execute() builds a plain cursor itself, so the LockedCursor is created explicitly.
    def execute(self, *a, **kw):
        with self._mmai_lock:
            if a:
                _refuse_tx_control_sql(self, a[0])
            return self.cursor().execute(*a, **kw)

    def _raw_execute(self, sql: str):
        """Guard-internal: the entry statement (BEGIN IMMEDIATE) runs with the guard flags already set."""
        return super().execute(sql)

    def executemany(self, *a, **kw):
        with self._mmai_lock:
            return self.cursor().executemany(*a, **kw)

    def executescript(self, *a, **kw):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "executescript (it commits implicitly)")
            return super().executescript(*a, **kw)

    def commit(self):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "commit")
            return super().commit()

    def rollback(self):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "rollback")
            return super().rollback()

    def _raw_commit(self):
        return super().commit()

    def _raw_rollback(self):
        return super().rollback()

    # sqlite3's native `with connection:` commits/rolls back at C level, bypassing the Python overrides above: refused while guarded
    def __enter__(self):
        if self._guard_depth:
            raise _forbidden_control(self, "native with-block (it commits implicitly)")
        return super().__enter__()

    def __exit__(self, et, ev, tb):
        if self._guard_depth:
            raise _forbidden_control(self, "native with-block (it commits implicitly)")
        return super().__exit__(et, ev, tb)


# ---------------------------------------------------------------------------
# PostgreSQL adapter (psycopg v3)
# ---------------------------------------------------------------------------
# Unique keys used by INSERT OR REPLACE / OR IGNORE statements (the upsert needs an explicit conflict target on Postgres).
UNIQUE_KEYS = {
    "group_apartments": ("group_id", "apartment_id"), "group_members": ("group_id", "enquiry_id"),
    "member_rejections": ("rejected_enquiry_id", "other_enquiry_id"), "settings": ("key",), "cities": ("name",),
    "mate_links": ("token",), "form_drafts": ("token",), "sessions": ("token",), "staff_users": ("username",), "email_links": ("token",),
    "enquiry_submissions": ("submit_key",),
}
_SQL_CACHE: dict[tuple[str, bool], str] = {}
_RE_REPLACE = re.compile(r"^\s*INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)
_RE_IGNORE = re.compile(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+", re.I)
_RE_DESC = re.compile(r"\bDESC\b(?!\s+NULLS)", re.I)


def translate_sql(sql: str, has_params: bool) -> str:
    """Rewrite the SQLite-flavoured SQL used in the app for PostgreSQL. Cached per statement text."""
    key = (sql, has_params)
    out = _SQL_CACHE.get(key)
    if out is not None:
        return out
    s = sql
    m = _RE_REPLACE.match(s)
    if m:
        table, cols = m.group(1), [c.strip() for c in m.group(2).split(",")]
        keycols = UNIQUE_KEYS.get(table.lower(), ("id",))
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in keycols)
        s = s[:m.start()] + f"INSERT INTO {table}({m.group(2)})" + s[m.end():]
        s = s.rstrip().rstrip(";") + f" ON CONFLICT ({', '.join(keycols)}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
    elif _RE_IGNORE.match(s):
        s = _RE_IGNORE.sub("INSERT INTO ", s).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    s = s.replace("last_insert_rowid()", "lastval()")
    s = _RE_DESC.sub("DESC NULLS LAST", s)
    if has_params:
        s = s.replace("%", "%%").replace("?", "%s")
    _SQL_CACHE[key] = s
    return s


def translate_ddl(ddl: str) -> str:
    s = ddl.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY")
    s = re.sub(r"\bREAL\b", "DOUBLE PRECISION", s)
    return s


def _adapt_params(params):
    if params is None:
        return None
    out = []
    for v in params:
        if isinstance(v, bool):
            v = int(v)
        elif isinstance(v, (datetime, date)):
            v = v.isoformat()
        out.append(v)
    return out


class PgRow:
    """Row with sqlite3.Row semantics: r[0], r["col"], keys(), iteration over values, dict(r)."""
    __slots__ = ("_vals", "_cols", "_idx")

    def __init__(self, vals, cols, idx):
        self._vals, self._cols, self._idx = vals, cols, idx

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._vals[k]
        return self._vals[self._idx[k]]

    def __contains__(self, k):
        return k in self._idx

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def keys(self):
        return list(self._cols)

    def get(self, k, default=None):
        i = self._idx.get(k)
        return default if i is None else self._vals[i]

    def __eq__(self, other):
        if isinstance(other, PgRow):
            return self._cols == other._cols and tuple(self._vals) == tuple(other._vals)
        return tuple(self._vals) == tuple(other) if isinstance(other, (tuple, list)) else NotImplemented

    def __hash__(self):
        return hash((self._cols, tuple(self._vals)))

    def __repr__(self):
        return f"PgRow({dict(zip(self._cols, self._vals))!r})"


def _pg_row_factory(cursor):
    desc = cursor.description
    cols = tuple(d.name for d in desc) if desc else ()
    idx = {c: i for i, c in enumerate(cols)}
    return lambda vals: PgRow(vals, cols, idx)


class PgCursor:
    """Result holder: psycopg client-side cursors fetch everything at execute time, so reading later needs no lock."""
    __slots__ = ("_rows", "_pos", "rowcount", "description", "lastrowid")

    def __init__(self, cur):
        self.description = cur.description
        self.rowcount = cur.rowcount
        self.lastrowid = None
        self._rows = cur.fetchall() if cur.description else []
        self._pos = 0

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        r = self._rows[self._pos]
        self._pos += 1
        return r

    def fetchall(self):
        r = self._rows[self._pos:]
        self._pos = len(self._rows)
        return r

    def fetchmany(self, n=1):
        r = self._rows[self._pos:self._pos + n]
        self._pos += len(r)
        return r

    def __iter__(self):
        while self._pos < len(self._rows):
            yield self.fetchone()

    def close(self):
        pass


def _is_connection_loss(e: BaseException) -> bool:
    """psycopg raises OperationalError both for a dead connection (no SQLSTATE, or class 08 / operator intervention 57P0x) and for
    statement-level conditions such as a lock timeout (55P03). Only the former means the session and its transaction are gone."""
    state = getattr(e, "sqlstate", None)
    return not state or state.startswith("08") or state in ("57P01", "57P02", "57P03")


class PgConnection:
    """psycopg (v3) connection with the sqlite3.Connection interface used by the app. One shared instance, serialised by a
    re-entrant lock (psycopg connections are not safe for concurrent use). Statements run inside a SAVEPOINT so a failed
    statement leaves the surrounding transaction usable, exactly as SQLite behaves. Reconnects once if the server went away."""

    backend = "postgres"

    def __init__(self, url: str):
        self._url = url
        self._mmai_lock = threading.RLock()
        self._pg = None
        self._dirty = False  # a write has run since the last commit/rollback
        self._guard_depth = 0           # retention guard nesting (see retention_guard)
        self._guard_owner = None        # thread ident of the guard holder (see guarded())
        self._hold_tx = False           # while True no read is auto-committed and no reconnect is attempted
        self._guard_rollback_only = False
        self._open()

    def _open(self):
        import psycopg
        self._pg = psycopg.connect(self._url, autocommit=False, row_factory=_pg_row_factory, application_name="matebot-ai",
                                   keepalives=1, keepalives_idle=60, keepalives_interval=20, keepalives_count=3)
        # Every transaction is begun as READ COMMITTED explicitly (psycopg issues BEGIN ISOLATION LEVEL ...), whatever the
        # role/database default_transaction_isolation says. The retention guard depends on it: a REPEATABLE READ snapshot
        # taken before waiting for the advisory lock would not see a deletion committed by the other connection while waiting.
        self._pg.isolation_level = psycopg.IsolationLevel.READ_COMMITTED

    def _reconnect(self):
        try:
            if self._pg is not None:
                self._pg.close()
        except Exception:
            pass
        self._open()

    def _idle(self) -> bool:
        """No pending write, no active guard: the only state in which a lost connection may be re-opened and the statement replayed."""
        return not self._dirty and not self._hold_tx and not self._guard_depth

    @property
    def in_transaction(self) -> bool:
        """sqlite3-compatible: True while the server session has a transaction open (psycopg's real status, not our flags), so
        tests that assert `not db.in_transaction` after a guarded step hold on both backends. Closed/unknown session: False."""
        try:
            from psycopg.pq import TransactionStatus
            return self._pg is not None and not self._pg.closed and self._pg.info.transaction_status != TransactionStatus.IDLE
        except Exception:   # pragma: no cover - a torn-down session reports no transaction
            return False

    def _lost(self, e: BaseException, during_commit: bool = False) -> ConnectionLostMidOperation:
        """R4: the session (its transaction, writes and locks) is gone while work was pending: close it, reset the adapter flags
        (the guard's own finally resets the depth) and fail closed. The NEXT idle operation (or the next OUTER guard entry)
        re-opens lazily. A loss during COMMIT leaves the outcome unknown; the exception says so."""
        try:
            if self._pg is not None:
                self._pg.close()
        except Exception:
            pass
        self._pg = None
        self._dirty = False
        self._hold_tx = False
        if self._guard_depth:
            # the guarded step is over whatever the body does next: a helper that catches this error must not let the outer
            # guard report a successful (empty) commit
            self._guard_rollback_only = True
        _log.warning("database connection lost during a %s: %s", "commit (outcome unknown)" if during_commit else "pending operation", type(e).__name__)
        return ConnectionLostMidOperation("connection lost during commit: outcome unknown" if during_commit else "connection lost during a pending operation",
                                          outcome_unknown=during_commit)

    def _ensure_open(self) -> None:
        """Lazy re-open after a loss, allowed only while idle (called by _run and by the guard BEFORE it takes ownership)."""
        if (self._pg is None or self._pg.closed) and self._idle():
            self._open()

    def _acquire_retention_lock(self) -> None:
        import psycopg
        try:
            # First statement of the guarded transaction: verify the isolation level BEFORE any snapshot-producing statement.
            # Identity checks after the lock must see rows as they are AFTER the other connection's commit; only READ COMMITTED
            # (a fresh snapshot per statement) gives that. Anything else is refused - never worked around.
            level = self._pg.execute("SELECT current_setting('transaction_isolation')").fetchone()
            level = (level[0] if level else None)
            if level is None or str(level).lower() != "read committed":
                raise RetentionGuardError(f"retention guard needs READ COMMITTED, the transaction is {level!r}")
            self.execute("SELECT set_config('lock_timeout', ?, true)", (str(RETENTION_LOCK_TIMEOUT_MS),))
            self.execute("SELECT pg_advisory_xact_lock(?)", (RETENTION_LOCK_KEY,))
        except psycopg.errors.LockNotAvailable as e:
            try:
                self._raw_rollback()
            except Exception:
                pass
            raise RetentionLockTimeout("retention lock not obtained in time") from e
        except psycopg.OperationalError as e:
            if _is_connection_loss(e):
                raise self._lost(e) from e            # session gone at entry: nothing held; the next idle operation re-opens
            raise

    def _run(self, sql: str, params, many: bool = False):
        import psycopg
        pq = translate_sql(sql, bool(params))
        is_read = self._is_read(sql)
        for attempt in (1, 2):
            try:
                if self._pg is None or self._pg.closed:
                    if not self._idle():
                        raise self._lost(psycopg.OperationalError("connection closed"))
                    if self._pg is None:
                        self._ensure_open()    # lazy re-open after a loss, only when idle
                    else:
                        raise psycopg.OperationalError("connection closed")
                if not is_read:
                    self._dirty = True         # write in flight BEFORE dispatch: a transport loss from here on is never replayed (R4)
                cur = self._pg.cursor()
                self._pg.execute("SAVEPOINT mmai_sp")  # separate cursor: keeps `cur`'s description / rowcount intact
                try:
                    if many:
                        cur.executemany(pq, [_adapt_params(p) for p in params])
                    else:
                        cur.execute(pq, _adapt_params(params) if params else None)
                except Exception:
                    self._pg.execute("ROLLBACK TO SAVEPOINT mmai_sp")
                    self._pg.execute("RELEASE SAVEPOINT mmai_sp")
                    raise
                self._pg.execute("RELEASE SAVEPOINT mmai_sp")
                return cur
            except psycopg.OperationalError as e:
                if not _is_connection_loss(e):
                    raise                              # a statement-level OperationalError (e.g. lock timeout): the SAVEPOINT already undid it
                if not self._idle():
                    raise self._lost(e) from e        # never reconnect/replay with pending work, a write in flight, or inside a guard
                # only a demonstrably read-only statement on an idle connection is re-opened and replayed
                if attempt == 2 or (self._pg is not None and not self._pg.closed and "closed" not in str(e).lower()
                                    and "terminat" not in str(e).lower() and "server" not in str(e).lower()):
                    raise
                self._reconnect()

    # Replay / auto-commit classification (conservative). A statement counts as a READ only when it starts with SELECT or SHOW
    # AND contains none of the tokens below. Everything else - including WITH (a CTE may INSERT/UPDATE/DELETE), EXPLAIN (ANALYZE
    # executes the statement), SELECT ... FOR UPDATE/SHARE and SELECTs calling side-effecting functions - is handled as a WRITE:
    # the connection is marked dirty before dispatch, a transport loss after dispatch is never replayed, and the caller
    # commits/rolls back it like any write. This is not proof of read-only SQL: it is the supported app-query scope (plain
    # SELECTs over tables with built-in pure functions). A false positive (a token inside a string literal) only makes the
    # statement conservatively a write.
    _READ_HEAD = re.compile(r"^\s*(SELECT|SHOW)\b", re.I)
    _NOT_READ = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|INTO|CREATE|ALTER|DROP|TRUNCATE|COPY|LOCK|CALL|DO|GRANT|REVOKE|"
                           r"FOR\s+(NO\s+KEY\s+)?UPDATE|FOR\s+(KEY\s+)?SHARE|set_config|setval|nextval|pg_(try_)?advisory\w*|"
                           r"pg_terminate_backend|pg_cancel_backend|pg_sleep\w*|pg_notify|pg_reload_conf|pg_switch_wal|"
                           r"pg_create_\w+|pg_drop_\w+|pg_replication\w*|lo_\w+|dblink\w*|txid_current\w*|pg_current_xact_id\w*)\b", re.I)

    @classmethod
    def _is_read(cls, sql: str) -> bool:
        return bool(cls._READ_HEAD.match(sql)) and not cls._NOT_READ.search(sql)

    def execute(self, sql: str, params=()):
        with self._mmai_lock:
            _refuse_tx_control_sql(self, sql)
            cur = self._run(sql, tuple(params) if params else ())
            res = PgCursor(cur)  # rows are copied out so later fetches need no lock
            cur.close()
            if self._is_read(sql):
                # Pure reads with no pending write: close the implicit transaction so the connection is never left
                # "idle in transaction" (which would block schema changes and hold vacuum back on the managed database).
                # Never while a retention guard holds the transaction (the advisory lock lives in it).
                if not self._dirty and not self._hold_tx and not self._guard_depth:
                    self._pg.commit()
            else:
                self._dirty = True
            return res

    def executemany(self, sql: str, seq):
        with self._mmai_lock:
            cur = self._run(sql, [tuple(p) for p in seq], many=True)
            res = PgCursor(cur)
            cur.close()
            self._dirty = True
            return res

    def executescript(self, script: str):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "executescript (it commits implicitly)")
            if self._pg is None:
                self._open()
            self._pg.execute(translate_ddl(script))
            self._pg.commit()
            self._dirty = False

    def commit(self):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "commit")
            self._raw_commit()

    def rollback(self):
        with self._mmai_lock:
            if self._guard_depth:
                raise _forbidden_control(self, "rollback")
            self._raw_rollback()

    def _raw_commit(self):
        import psycopg
        if self._pg is None or self._pg.closed:
            if self._dirty or self._hold_tx:
                raise self._lost(psycopg.OperationalError("connection closed before commit"))
            self._dirty = False
            return
        try:
            self._pg.commit()
        except psycopg.OperationalError as e:
            if _is_connection_loss(e):
                raise self._lost(e, during_commit=True) from e
            try:
                self._pg.rollback()
            except Exception:
                pass
            self._dirty = False
            raise
        except Exception:
            # the server refused the commit (e.g. a deferred constraint): the transaction is over on the server side; make sure
            # nothing stays open or locked before the error reaches the caller
            try:
                self._pg.rollback()
            except Exception:
                pass
            self._dirty = False
            raise
        self._dirty = False

    def _raw_rollback(self):
        import psycopg
        if self._pg is None or self._pg.closed:
            self._dirty = False   # nothing to roll back: the session died with its transaction
            return
        try:
            self._pg.rollback()
        except psycopg.OperationalError as e:
            raise self._lost(e) from e
        self._dirty = False

    def close(self):
        with self._mmai_lock:
            if self._pg is not None:
                self._pg.close()

    def table_columns(self, table: str) -> set[str]:
        return {r[0] for r in self.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchall()}

    def table_names(self) -> list[str]:
        return [r[0] for r in self.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE' ORDER BY table_name").fetchall()]


def table_columns(c, table: str) -> set[str]:
    if getattr(c, "backend", "sqlite") == "postgres":
        return c.table_columns(table)
    return {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}


def table_names(c) -> list[str]:
    if getattr(c, "backend", "sqlite") == "postgres":
        return c.table_names()
    return [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]


def connect(path: str | None = None):
    """Shared connection. SQLite: `path` (or MMAI_DB) opens that file; a given path returns a separate connection (tests).
    PostgreSQL (DATABASE_URL set): `path` is ignored; a given path returns a separate connection to the same database."""
    global _conn
    with _lock:
        if _conn is None or path:
            if is_postgres():
                c = PgConnection(database_url())
                init_schema(c)
            else:
                p = path or os.environ.get("MMAI_DB") or DB_PATH
                os.makedirs(os.path.dirname(p), exist_ok=True)
                c = sqlite3.connect(p, check_same_thread=False, timeout=15, factory=Connection)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA foreign_keys=ON")
                init_schema(c)
            if path:
                return c
            _conn = c
        return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS cities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS apartments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  city TEXT NOT NULL DEFAULT 'Melbourne',
  address TEXT NOT NULL,            -- normalised display address
  address_raw TEXT NOT NULL,        -- original text from ALM's list (never lost)
  suburb TEXT,
  type_code TEXT NOT NULL,          -- A14
  bathrooms INTEGER NOT NULL,       -- from type code (A14)
  rent_12m REAL NOT NULL,           -- A2(d)
  rent_6m REAL NOT NULL,            -- A2(f)
  available_from TEXT,              -- A2(e), ISO date; NULL = not yet available
  available_raw TEXT,               -- original cell text when it was not a date
  min_lease_months REAL NOT NULL DEFAULT 2,  -- A2(h), A9, A15(d)
  bedroom1 TEXT, bedroom2 TEXT, bedroom3 TEXT,  -- A2(g), A4(d)
  bills_included INTEGER NOT NULL DEFAULT 0,   -- A2(i)
  lat REAL, lon REAL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enquiries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  is_test INTEGER NOT NULL DEFAULT 0,
  channel TEXT NOT NULL,            -- 7.1 (I4)
  campaign TEXT,                    -- 7.2
  staff_member TEXT,                -- 7.3
  created_at TEXT NOT NULL,         -- 7.4 (I10)
  last_contact_at TEXT NOT NULL,    -- T5 clock
  status TEXT NOT NULL DEFAULT 'New',  -- 7.5
  staff_notes TEXT,                 -- 7.6
  email_link_id INTEGER,
  first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL,
  phone_code TEXT, phone TEXT, contact_method TEXT,
  dob TEXT NOT NULL,                -- 1.6
  sex TEXT NOT NULL, sex_other TEXT,  -- 1.7 (I8)
  birth_country_raw TEXT, birth_country TEXT, birth_country_confirmed INTEGER NOT NULL DEFAULT 0,  -- 1.8 (I15, M7)
  live_country_raw TEXT, live_country TEXT,  -- 1.9
  languages TEXT,                   -- 1.10 JSON list (M9)
  city TEXT NOT NULL DEFAULT 'Melbourne',  -- 2.1
  move_in TEXT NOT NULL, move_in_flex TEXT NOT NULL,   -- 2.2, 2.3
  move_out TEXT NOT NULL, move_out_flex TEXT NOT NULL, -- 2.4, 2.5
  budget_option TEXT NOT NULL, budget_guide TEXT NOT NULL, budget_max REAL NOT NULL,  -- 3.1 (I14)
  apt_sizes TEXT NOT NULL,          -- 3.2 JSON list
  mate_ideal INTEGER, mate_max INTEGER NOT NULL,  -- 3.3, 3.4
  rooms TEXT NOT NULL,              -- 3.5 JSON list
  bunk_ok INTEGER NOT NULL,         -- 3.6 (M3)
  room_first TEXT,                  -- 3.7
  party TEXT NOT NULL, party_size INTEGER,  -- 3.8, 3.9
  doing TEXT, school TEXT, school_other TEXT, course TEXT, level TEXT, course_start TEXT, course_end TEXT, work TEXT,
  areas TEXT, travel TEXT,          -- 4.6, 4.7
  sex_pref TEXT NOT NULL,           -- 5.1
  age_pref TEXT,                    -- 5.2 JSON list
  own_region TEXT,                  -- 5.3
  avoid_answer TEXT NOT NULL, avoid_regions TEXT, avoid_countries TEXT,  -- 5.4
  smoke TEXT NOT NULL, smoke_ok TEXT NOT NULL,  -- 5.5, 5.6
  routine TEXT, tidy TEXT, social TEXT, guests TEXT, diet TEXT,
  else_text TEXT, intro TEXT,       -- 5.12, 5.13
  source TEXT,                      -- 6.1 (I10)
  consent_store INTEGER NOT NULL, consent_share INTEGER NOT NULL, consent_age INTEGER NOT NULL, consent_marketing INTEGER NOT NULL DEFAULT 0,
  guardian_name TEXT, guardian_relationship TEXT, guardian_email TEXT, guardian_phone TEXT,  -- 6.5 (I11)
  answers_json TEXT NOT NULL        -- full raw submission
);
CREATE INDEX IF NOT EXISTS ix_enq_status ON enquiries(status);
CREATE INDEX IF NOT EXISTS ix_enq_movein ON enquiries(move_in);
CREATE INDEX IF NOT EXISTS ix_enq_test ON enquiries(is_test);
CREATE TABLE IF NOT EXISTS party_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  name TEXT NOT NULL, email TEXT NOT NULL,  -- I16(a)
  sex TEXT, sex_other TEXT, dob TEXT,        -- I16(b) (nullable: rows saved before 26 Oct 2026 have none)
  needs_guardian INTEGER NOT NULL DEFAULT 0, -- I11(b)/I16(c): under 18 at enquiry, 18 by move-in -> staff collect guardian details later
  birth_country_raw TEXT, birth_country TEXT, birth_country_confirmed INTEGER NOT NULL DEFAULT 0  -- I16(b) country of birth per person (I15, M10 to M12)
);
CREATE TABLE IF NOT EXISTS match_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  apartment_id INTEGER REFERENCES apartments(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL, finished_at TEXT,
  staff_member TEXT,
  candidates_considered INTEGER, groups_passing INTEGER,
  ai_used INTEGER NOT NULL DEFAULT 0, ai_error TEXT,
  summary_json TEXT
);
CREATE TABLE IF NOT EXISTS groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  match_run_id INTEGER REFERENCES match_runs(id) ON DELETE SET NULL,
  -- 9 Nov 2026 (stock lifecycle): the current offer. NULL = held with no current offer (A3a delete, withdrawal or lease left no
  -- valid listed apartment). An apartment delete sets it NULL instead of erasing the group and its history (was NOT NULL CASCADE;
  -- existing databases are rebuilt once in _migrate_groups_apartment_fk).
  apartment_id INTEGER REFERENCES apartments(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'Proposed',  -- Proposed / Introduced / Group confirmed / Leased / Dissolved
  lease_start TEXT, lease_end TEXT, rate_kind TEXT, exit_window TEXT,
  weekly_rent REAL, extra_bed REAL, total_weekly REAL,
  allocation_json TEXT,
  rule_score REAL, ai_rating REAL, ai_rationale TEXT, ai_model TEXT,
  ranking_basis TEXT,               -- 'AI' or 'Rules only (AI unavailable)'
  intro_text TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  weekly_share REAL, room INTEGER, bed TEXT, arrangement TEXT,
  UNIQUE(group_id, enquiry_id)
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, staff_member TEXT, action TEXT NOT NULL, entity TEXT, entity_id TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS staff_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
  pw_salt TEXT NOT NULL, pw_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'staff'
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES staff_users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS email_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, staff_member TEXT, recipient_email TEXT, recipient_name TEXT,
  used_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT
);
-- GP6: every matched group's ranked apartment list (one row per group x suitable apartment), 26 Oct 2026
CREATE TABLE IF NOT EXISTS group_apartments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  apartment_id INTEGER NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,            -- 1 = best suited (AI order when available, else rule order)
  rule_rank INTEGER,                -- order by the rule-based preference score
  rule_score REAL,
  ai_reason TEXT,                   -- GP6(a): the AI's reason this apartment suits the group
  lease_start TEXT, lease_end TEXT, rate_kind TEXT, exit_window TEXT,
  weekly_rent REAL, extra_bed REAL, total_weekly REAL,   -- B5 whole weekly rent
  allocation_json TEXT,             -- rooms, beds and per-room shares (A12(e)) at this apartment
  UNIQUE(group_id, apartment_id)
);
CREATE INDEX IF NOT EXISTS ix_ga_group ON group_apartments(group_id);
CREATE INDEX IF NOT EXISTS ix_ga_apt ON group_apartments(apartment_id);
-- GP7: members rejected by staff on behalf of a group; the AI never re-proposes the rejected person with those who rejected them
CREATE TABLE IF NOT EXISTS member_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER,                     -- group the rejection was made on (may be dissolved later; no FK so history survives)
  rejected_enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  other_enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,  -- a member who stayed in the group
  at TEXT NOT NULL, staff_member TEXT,
  UNIQUE(rejected_enquiry_id, other_enquiry_id)
);
-- T8: one row per hourly scheduler check (ran / deferred / skipped / failed)
CREATE TABLE IF NOT EXISTS scheduler_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  checked_at TEXT NOT NULL, dataset TEXT NOT NULL,
  outcome TEXT NOT NULL,                -- ran / deferred / skipped / failed
  reason TEXT, new_enquiries INTEGER, run_id INTEGER, next_check_at TEXT, trigger_kind TEXT  -- hourly / startup / manual
);
-- 27 Oct 2026 MateBot AI upgrade (additive only)
-- GP11: "Bring a mate" share links. One token per enquirer; friends who submit via the token are linked to enquiry_id.
CREATE TABLE IF NOT EXISTS mate_links (
  token TEXT PRIMARY KEY, enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0
);
-- I19(f): save-and-finish-later drafts of the public form. Token is server-side; expires in line with T5 retention.
CREATE TABLE IF NOT EXISTS form_drafts (
  token TEXT PRIMARY KEY, answers_json TEXT, step INTEGER, email TEXT, is_test INTEGER NOT NULL DEFAULT 0,
  mate_token TEXT, email_token TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
-- GP11(f): a friend who fits a group that has already been shared with its members waits for staff approval here.
CREATE TABLE IF NOT EXISTS mate_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  root_enquiry_id INTEGER NOT NULL, friend_enquiry_id INTEGER NOT NULL,
  replace_json TEXT,          -- enquiry ids that would leave the group to make room (may be empty)
  apartment_id INTEGER,       -- apartment the new group fits
  status TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / declined / superseded
  note TEXT, created_at TEXT NOT NULL, decided_at TEXT, decided_by TEXT, is_test INTEGER NOT NULL DEFAULT 0
);
-- GP9(e) (28 Oct 2026): a person barred on their own while the people who enquired with them on the same form carried on.
-- Their details are kept here (T5 retention applies), so staff can see who was barred and why; the form no longer lists them.
CREATE TABLE IF NOT EXISTS barred_people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  name TEXT NOT NULL, email TEXT, sex TEXT, dob TEXT, birth_country TEXT,
  was_lead INTEGER NOT NULL DEFAULT 0,   -- 1 = they were the person who filled in the form; the first other person took over the enquiry
  reason TEXT NOT NULL, barred_at TEXT NOT NULL, barred_by TEXT,
  lifted_at TEXT, lifted_by TEXT          -- set if staff later lift the bar (they rejoin by a new form or staff entry; the record is kept)
);
-- 31 Oct 2026: submission reliability. One row per form submission attempt key (an opaque random token made by the form,
-- never personal data). The row is reserved before the enquiry is written, gets enquiry_id in the SAME transaction as the
-- enquiry row, and keeps the response so a retry (double click, lost reply, reload) replays the same result instead of
-- making a second enquiry. `scope` keeps public and staff keys apart. `fingerprint` is a hash of the answers, so the same
-- key with different answers after a committed success is refused (already submitted) rather than overwriting anything.
CREATE TABLE IF NOT EXISTS enquiry_submissions (
  submit_key TEXT PRIMARY KEY, scope TEXT NOT NULL, fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending (reserved, nothing written) / saved (enquiry committed) / done (side effects and response kept) / deleted (tombstone: enquiry removed, response cleared)
  enquiry_id INTEGER, response_json TEXT, is_test INTEGER NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  owner TEXT                                -- fencing token of the request that currently holds a pending reservation (a stale takeover changes it)
);
-- 5 Nov 2026, I19(q) automatic customer receipts. One row per enquiry and kind (UNIQUE): a replayed submission cannot queue a
-- second receipt. No message body is stored: the email is rendered from the saved enquiry at send time, so deleting the enquiry
-- (ON DELETE CASCADE) removes the queued receipt and nothing is left to send. Status: queued / sending / sent / dryrun / held /
-- failed / cancelled. Nothing is sent unless MMAI_MAIL_MODE is set (see server/receipts.py).
CREATE TABLE IF NOT EXISTS email_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  to_email TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, message_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_attempt_at TEXT, sent_at TEXT,
  UNIQUE(enquiry_id, kind)
);
-- 5 Nov 2026, H19 internal reservations attention alerts (server/alerts.py). Separate from the customer receipt outbox. One row
-- per event_id (UNIQUE): raising the same event twice never queues a second alert. Rows hold only a fixed event kind, a numeric
-- record reference, the fixed internal recipient as queued and delivery state: no customer answers, names or free text. No FK to
-- the record (an alert carries no record data). Status: queued / sending / sent / dryrun / held / failed. Nothing is sent
-- unless MMAI_ALERT_MODE is set; no event producer is wired yet (prepared, not live).
CREATE TABLE IF NOT EXISTS internal_alert_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  record_type TEXT NOT NULL,
  record_id INTEGER NOT NULL,
  to_email TEXT NOT NULL,
  is_test INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, message_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_attempt_at TEXT, sent_at TEXT
);
-- 9 Nov 2026 stock lifecycle (server/stock_lifecycle.py). One row per transition that can make an apartment stop being a valid offer
-- for groups that listed it: leased / withdrawn (available_from cleared) / changed (material fields) / deleted (A3a) / replaced
-- (list replaced) / relisted. `apartment_id` is a plain integer (no FK) and `address` a snapshot, so the row outlives an A3a
-- delete; `generation` is apartments.lease_generation after the transition. The row id is the alert revision (collision-free,
-- never reused): one deduplicated H19 alert per (group, transition).
CREATE TABLE IF NOT EXISTS stock_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  apartment_id INTEGER NOT NULL,
  address TEXT NOT NULL,
  cause TEXT NOT NULL,
  generation INTEGER NOT NULL,
  changed_fields TEXT,
  created_at TEXT NOT NULL, created_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_st_apartment ON stock_transitions(apartment_id, id);
-- The durable per-group follow-up work of a transition, written in the SAME transaction as the inventory change and BEFORE any
-- option row or apartment row is removed (group id and the role the apartment had are captured here, not derived later). Each
-- row is completed in its own retention-guard step (reconcile + alert + refresh marking + done, atomic). pending / done / superseded.
CREATE TABLE IF NOT EXISTS offer_reconcile_work (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transition_id INTEGER NOT NULL REFERENCES stock_transitions(id) ON DELETE RESTRICT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  outcome TEXT,
  legacy_event_id TEXT,
  created_at TEXT NOT NULL, done_at TEXT,
  UNIQUE(transition_id, group_id)
);
CREATE INDEX IF NOT EXISTS ix_orw_pending ON offer_reconcile_work(status, transition_id);
CREATE INDEX IF NOT EXISTS ix_orw_group ON offer_reconcile_work(group_id, status);
-- 5 Nov 2026, H14 HomeBot apartment enquiries (server/homebot.py). Deliberately a SEPARATE table from `enquiries`: HomeBot asks no
-- sex, age, nationality or lifestyle questions, so no placeholder values exist, and the MateBot matcher, hourly scheduler, AI
-- queue, groups and Bring-a-mate code only ever read `enquiries`, so a HomeBot enquiry can never enter housemate matching or any
-- screening by any build, old or new. Status (HomeBot only): New / Staff reviewing / Options sent / Viewing arranged / Leased /
-- Closed. `review_flags` is a JSON list of plain-language staff flags. `answers_json` is the validated submission.
CREATE TABLE IF NOT EXISTS homebot_enquiries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  is_test INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'homebot',
  channel TEXT NOT NULL DEFAULT 'HomeBot form',
  status TEXT NOT NULL DEFAULT 'New',
  staff_member TEXT, staff_notes TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_contact_at TEXT NOT NULL,
  submission_key TEXT,
  first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT NOT NULL,
  phone_code TEXT, phone TEXT NOT NULL, contact_pref TEXT NOT NULL,
  party_mode TEXT NOT NULL,            -- complete / more_declined (party still growing; the party finds its own people)
  headcount_now INTEGER NOT NULL, headcount_target INTEGER NOT NULL,
  move_in TEXT NOT NULL, move_in_flex TEXT NOT NULL, move_in_earliest TEXT, move_in_latest TEXT,
  move_out TEXT NOT NULL, move_out_flex TEXT NOT NULL, move_out_earliest TEXT, move_out_latest TEXT,
  budget_total_weekly INTEGER NOT NULL, budget_types TEXT NOT NULL,
  bedrooms TEXT NOT NULL, bathrooms TEXT NOT NULL, area TEXT NOT NULL,
  transport TEXT NOT NULL, max_travel TEXT NOT NULL, extras TEXT NOT NULL, apartment_notes TEXT,
  bunk_condition TEXT,
  viewing TEXT NOT NULL, heard TEXT,
  consent_contact INTEGER NOT NULL, consent_lease INTEGER NOT NULL, consent_adults INTEGER NOT NULL,
  review_flags TEXT NOT NULL,
  answers_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_hb_submission_key ON homebot_enquiries(submission_key);
CREATE INDEX IF NOT EXISTS ix_hb_test ON homebot_enquiries(is_test);
CREATE TABLE IF NOT EXISTS homebot_people (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,           -- 1 = the person who completed the form
  name TEXT,                           -- optional for positions 2+
  sleep TEXT NOT NULL,                 -- per-person bed choice (H21)
  weekly_max INTEGER NOT NULL,         -- this person's weekly maximum, dollars (H9)
  doing TEXT NOT NULL, institution TEXT, institution_other TEXT, place TEXT, primary_destination TEXT
);
CREATE INDEX IF NOT EXISTS ix_hbp_enquiry ON homebot_people(enquiry_id);
-- Original HomeBot customer copy. Personal snapshot is owned by its enquiry and
-- removed with it; never shares MateBot ids or the MateBot email_outbox.
CREATE TABLE IF NOT EXISTS homebot_email_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, to_email TEXT NOT NULL, snapshot_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, message_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, next_attempt_at TEXT, sent_at TEXT,
  UNIQUE(enquiry_id, kind)
);
-- 8 Nov 2026 (T6 review workflow): append-only history. One row per AI review round of a group and one row per brief written for it
-- (per person and whole group). Rows are not updated by the application and are deleted only by T5 retention / authorised deletion
-- (enquiries.forget_generated_text); the group row carries the current state.
CREATE TABLE IF NOT EXISTS review_rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  verdict TEXT,
  rating REAL,
  rationale TEXT,
  replace_refs_json TEXT,
  review_json TEXT,
  valid INTEGER NOT NULL DEFAULT 0,
  invalid_reason TEXT,
  model TEXT,
  rule_version TEXT,
  evidence_hash TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_review_rounds_group ON review_rounds(group_id);
CREATE TABLE IF NOT EXISTS group_briefs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  round INTEGER NOT NULL,
  kind TEXT NOT NULL,
  person_ref TEXT,
  enquiry_id INTEGER,
  party_member_id INTEGER,
  body_json TEXT,
  status TEXT NOT NULL,
  error TEXT,
  model TEXT,
  rule_version TEXT,
  evidence_hash TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_group_briefs_group ON group_briefs(group_id);
-- 9 Nov 2026 (GAP-T4c v2, server/notes_facts.py): validated practical facts extracted from a person's own free text. Staff/audit
-- provenance per item (span + offsets into the named field, polarity, attribution, validator outcome); removed with the enquiry
-- (cascade + explicit delete in forget_generated_text). The SELECTION packet never reads this table: it reads the neutral
-- enquiries.notes_packet_json projection (category / strength / typed value / basis / source field only).
CREATE TABLE IF NOT EXISTS note_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  field TEXT NOT NULL,
  category TEXT,
  polarity TEXT,
  strength TEXT,
  subject TEXT,
  span TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  value_json TEXT,
  attribution TEXT,
  validator_status TEXT NOT NULL,
  rejection_code TEXT,
  model TEXT,
  catalogue_version TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_note_facts_enquiry ON note_facts(enquiry_id);
-- extraction cache: one row PER OWNER (enquiry, cascade-deleted with the person and purged by forget_generated_text and by any
-- edit of the person's free text), keyed by the hash of the exact input tuple (fields sent + party size + model + prompt/catalogue
-- version). It holds ONLY the validator-ACCEPTED items (verbatim spans of the owner's own text) and the residual flag - never the
-- raw model reply, never rejected model prose. Another enquiry with the identical tuple re-runs the validator on these items
-- (per-person revalidation) instead of calling the model again; written only inside the guarded apply step.
CREATE TABLE IF NOT EXISTS note_extraction_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  input_key TEXT NOT NULL,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  accepted_json TEXT NOT NULL,
  residual INTEGER NOT NULL DEFAULT 0,
  model TEXT,
  catalogue_version TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(input_key, enquiry_id)
);
CREATE INDEX IF NOT EXISTS ix_note_cache_key ON note_extraction_cache(input_key);
-- 9 Nov 2026, T3(a) H2 HomeBot integrated matching (server/homebot_shortlist.py). HomeBot-only tables; nothing here is read by
-- the MateBot matcher, scheduler, AI queue or groups. Every row belongs to one HomeBot enquiry and is removed with it
-- (homebot_shortlist.forget: explicit deletes, plus ON DELETE CASCADE where the backend enforces it). No contact details are
-- stored in any of these tables: basis_json/result_json hold matching inputs and outcomes only.
-- Immutable requirement basis per enquiry and scenario ('current' members now / 'final_size' with a confirmed planning budget).
CREATE TABLE IF NOT EXISTS homebot_requirement_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  scenario TEXT NOT NULL,
  version INTEGER NOT NULL,
  basis_json TEXT NOT NULL,
  basis_hash TEXT NOT NULL,
  planning_id INTEGER,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL,
  change_note TEXT,
  UNIQUE(enquiry_id, scenario, version)
);
-- H24 staff-confirmed planning budget for the party's final size, with the basis (party mode, head counts, stated budget) it
-- was confirmed against; superseded_at is set when confirmed again or when that basis no longer applies.
CREATE TABLE IF NOT EXISTS homebot_planning (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  target_size INTEGER NOT NULL,
  budget_total INTEGER NOT NULL,
  basis_json TEXT NOT NULL,
  basis_hash TEXT NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  superseded_at TEXT, superseded_reason TEXT
);
-- One durable job per enquiry and scenario. desired_generation rises with every trigger; claimed_generation is fixed for the
-- running claim; owner is the fencing token checked on every write. Status: queued / running / done / failed / cancelled.
CREATE TABLE IF NOT EXISTS homebot_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  scenario TEXT NOT NULL,
  status TEXT NOT NULL,
  reason TEXT,
  desired_generation INTEGER NOT NULL DEFAULT 1,
  claimed_generation INTEGER NOT NULL DEFAULT 0,
  owner TEXT, locked_at TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  error_kind TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(enquiry_id, scenario)
);
-- Append-only shortlist runs. Status: complete / unresolved_partial (computation finished; some date ranges too wide to
-- enumerate) / superseded (basis moved during the run; never current) / failed. review_state stays 'unreviewed' until the
-- grounded AI review (H7/H14, lead's slice) records a result for this id. Exactly one is_current=1 per enquiry and scenario.
CREATE TABLE IF NOT EXISTS homebot_shortlists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  scenario TEXT NOT NULL,
  job_id INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  requirement_version_id INTEGER NOT NULL,
  status TEXT NOT NULL,
  review_state TEXT NOT NULL DEFAULT 'unreviewed',
  review_enqueued_at TEXT,                          -- when the grounded AI review (H7/H14 queue) accepted this shortlist; NULL = not yet handed over
  review_enqueue_attempts INTEGER NOT NULL DEFAULT 0,  -- bounded hand-over attempts (durable retry by the worker; staff may reset = retry)
  today TEXT NOT NULL,
  inventory_hash TEXT NOT NULL,
  basis_hash TEXT NOT NULL,
  algorithm_version TEXT NOT NULL,
  result_json TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  listed_ids TEXT NOT NULL,
  fourth_option_id INTEGER,
  unresolved_ids TEXT NOT NULL,
  is_test INTEGER NOT NULL DEFAULT 0,
  is_current INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(job_id, generation, basis_hash)
);
CREATE INDEX IF NOT EXISTS ix_hb_shortlists_current ON homebot_shortlists(enquiry_id, scenario, is_current);
-- H25/GP14 customer rejections: immutable offered basis; status active / reopened; every reopening or narrow re-offer
-- allowance is an event row (never an update of the rejection's basis).
CREATE TABLE IF NOT EXISTS homebot_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  apartment_id INTEGER NOT NULL,
  building_key TEXT NOT NULL,
  shortlist_id INTEGER,
  requirement_version_id INTEGER,
  offered_basis_json TEXT NOT NULL,
  customer_reason TEXT,
  recorded_by TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  reopened_at TEXT, reopened_by TEXT, reopen_route TEXT
);
CREATE TABLE IF NOT EXISTS homebot_rejection_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rejection_id INTEGER NOT NULL REFERENCES homebot_rejections(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL,
  event TEXT NOT NULL,
  apartment_id INTEGER,
  staff TEXT NOT NULL,
  at TEXT NOT NULL,
  note TEXT,
  change_basis_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_hb_rejections_enquiry ON homebot_rejections(enquiry_id, status);

-- GP14 (MateBot): a room/bed enquirer's "not this apartment" / "not this building", recorded by staff, owned by the enquiry (survives
-- regrouping), no customer message, no people-rejection row. Immutable history: offered_basis_json is what was shown (group option
-- + material fields) and is never turned back into a current offer. status active|reopened. Mirrors homebot_rejections.
CREATE TABLE IF NOT EXISTS apartment_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  scope TEXT NOT NULL,
  apartment_id INTEGER REFERENCES apartments(id) ON DELETE SET NULL,
  building_key TEXT NOT NULL,
  group_id INTEGER,
  basis_kind TEXT NOT NULL,
  offered_basis_json TEXT NOT NULL,
  customer_reason TEXT,
  recorded_by TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  reopened_at TEXT, reopened_by TEXT, reopen_route TEXT
);
CREATE INDEX IF NOT EXISTS ix_apt_rejections_enquiry ON apartment_rejections(enquiry_id, status);
CREATE TABLE IF NOT EXISTS apartment_rejection_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rejection_id INTEGER NOT NULL REFERENCES apartment_rejections(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL,
  event TEXT NOT NULL,
  apartment_id INTEGER,
  staff TEXT NOT NULL,
  at TEXT NOT NULL,
  note TEXT,
  change_basis_json TEXT
);
-- A10: one staff-authorised exact booking. Monetary totals are canonical decimal TEXT, not an invented tariff.
CREATE TABLE IF NOT EXISTS exit_exceptions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot TEXT NOT NULL,
  is_test INTEGER,
  homebot_enquiry_id INTEGER REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
  scenario TEXT, membership_hash TEXT,
  apartment_id INTEGER NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
  move_in TEXT NOT NULL, move_out TEXT NOT NULL, requirement_version TEXT,
  context_ref TEXT NOT NULL, type_code TEXT NOT NULL, people INTEGER NOT NULL,
  normal_weekly_rent TEXT NOT NULL, whole_apartment_max TEXT NOT NULL,
  exception_total_weekly_rent TEXT NOT NULL, normal_total_weekly_rent TEXT,
  approved_by TEXT NOT NULL, approved_at TEXT NOT NULL,
  agreement_recorded_by TEXT, agreement_recorded_at TEXT, agreement_scope TEXT, agreement_total TEXT,
  status TEXT NOT NULL, withdrawal_reason TEXT, evaluation_json TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK ((bot='matebot' AND group_id IS NOT NULL AND homebot_enquiry_id IS NULL)
      OR (bot='homebot' AND group_id IS NULL AND homebot_enquiry_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_exit_exceptions_live_ctx ON exit_exceptions(context_ref) WHERE status <> 'withdrawn';
CREATE INDEX IF NOT EXISTS ix_exit_exceptions_group ON exit_exceptions(group_id, status);
CREATE INDEX IF NOT EXISTS ix_exit_exceptions_hb ON exit_exceptions(homebot_enquiry_id, status);
-- Immutable original member ownership, including people who later leave the group.
CREATE TABLE IF NOT EXISTS exit_exception_members (
  exception_id INTEGER NOT NULL REFERENCES exit_exceptions(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL,
  PRIMARY KEY(exception_id, enquiry_id)
);
CREATE INDEX IF NOT EXISTS ix_exit_exception_members_person ON exit_exception_members(enquiry_id);
-- H7/H14 grounded AI review of a HomeBot shortlist (lead slice, server/homebot_review.py; PROPOSED DDL, integration builder owns this file).
-- One row per (shortlist, basis fingerprint); state closed pending|running|reviewed|invalid|failed|stale; error holds a closed sentence only.
CREATE TABLE IF NOT EXISTS homebot_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shortlist_id INTEGER NOT NULL REFERENCES homebot_shortlists(id) ON DELETE CASCADE,
  enquiry_id INTEGER NOT NULL REFERENCES homebot_enquiries(id) ON DELETE CASCADE,
  state TEXT NOT NULL,
  version TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  owner TEXT,
  claimed_at TEXT,
  verdict TEXT,
  review_json TEXT,
  error TEXT,
  model TEXT,
  rule_version TEXT,
  contract TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_at TEXT,
  UNIQUE(shortlist_id, version)
);
CREATE INDEX IF NOT EXISTS ix_homebot_reviews_state ON homebot_reviews(state);
-- 9 Nov 2026, GP13 individual group responses (server/identities.py, server/group_responses.py). Additive; every row is reached by
-- retention (enquiries.forget_generated_text -> group_responses.forget) as well as by the FK cascades.
-- Server-issued opaque identity per real person: one per party_members ROW (so two companions sharing an email are two people) and
-- one lead per enquiry. name_norm/email_norm are kept only to detect an identity edit (conservative reset) and are personal data:
-- retention deletes the rows. A retired identity keeps its uid so historical versions stay readable until the person is forgotten.
CREATE TABLE IF NOT EXISTS people_identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  person_uid TEXT NOT NULL UNIQUE,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                       -- lead | member
  party_member_id INTEGER,                  -- the party_members row (NULL for the lead)
  name_norm TEXT, email_norm TEXT,
  created_at TEXT NOT NULL,
  retired_at TEXT, retired_reason TEXT,     -- left | barred | identity_edit | enquiry_deleted
  superseded_by_uid TEXT
);
CREATE INDEX IF NOT EXISTS ix_people_identities_enquiry ON people_identities(enquiry_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_people_identities_lead ON people_identities(enquiry_id) WHERE role='lead' AND retired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_people_identities_member ON people_identities(party_member_id) WHERE party_member_id IS NOT NULL AND retired_at IS NULL;
-- Immutable membership versions: the set of people (person_uids) a group had; the 24 h review clock is anchored on THIS version's
-- staff Introduced action (introduced_at), never on draft creation. Exactly one current version per group (partial unique index).
CREATE TABLE IF NOT EXISTS group_membership_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  membership_hash TEXT NOT NULL,
  members_json TEXT NOT NULL,               -- [{person_uid, enquiry_id, role, display_name}] only
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL, created_by TEXT,
  introduced_at TEXT, introduced_by TEXT,
  anchor_kind TEXT,                         -- closed: 'introduction' | 'remaining_group' (GP10(b)); NULL until anchored
  disclosure_hash TEXT,                     -- remaining_group only: digest of the full context staff recorded as explained
  superseded_at TEXT,
  UNIQUE(group_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gmv_current ON group_membership_versions(group_id) WHERE superseded_at IS NULL;
-- One ACTIVE link per person per version (rotation inserts a new row and sets revoked_at on the old one; nothing is deleted).
CREATE TABLE IF NOT EXISTS group_response_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version_id INTEGER NOT NULL REFERENCES group_membership_versions(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  person_uid TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  is_test INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, created_by TEXT,
  expires_at TEXT NOT NULL,
  revoked_at TEXT, revoked_reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_grl_active ON group_response_links(version_id, person_uid) WHERE revoked_at IS NULL;
-- Append-only response history; the current response of a person on a version is the latest row with superseded_at IS NULL.
CREATE TABLE IF NOT EXISTS group_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  link_id INTEGER NOT NULL REFERENCES group_response_links(id) ON DELETE CASCADE,
  version_id INTEGER NOT NULL REFERENCES group_membership_versions(id) ON DELETE CASCADE,
  group_id INTEGER NOT NULL,
  enquiry_id INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
  person_uid TEXT NOT NULL,
  response TEXT NOT NULL,                   -- accept | decline | change_request
  note TEXT,
  responded_at TEXT NOT NULL,
  superseded_at TEXT,
  reviewed_at TEXT, reviewed_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_group_responses_version ON group_responses(version_id, person_uid);
-- 10 Nov 2026 (T3(d) C3, register A13(a)): append-only history of recorded apartment access facts with their source and scope.
-- The CURRENT value also sits on apartments.step_free_access (1 yes / 0 no / NULL unknown) with its provenance columns; the
-- history row that is current has superseded_at IS NULL. Never inferred from an address, a building, a lift or another apartment.
CREATE TABLE IF NOT EXISTS apartment_access_facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  apartment_id INTEGER NOT NULL REFERENCES apartments(id) ON DELETE CASCADE,
  fact TEXT NOT NULL,                       -- 'step_free_access'
  value INTEGER,                            -- 1 yes / 0 no / NULL unknown
  status TEXT NOT NULL,                     -- confirmed | not_step_free | unknown | excluded_from_confirmation
  source TEXT,                              -- who confirmed it and when (e.g. "Bill, 10 November 2026" / a staff site check)
  scope TEXT,                               -- what the confirmation covered (e.g. "street to apartment door")
  note TEXT,                                -- staff note (never sent to the AI)
  recorded_by TEXT,
  recorded_at TEXT NOT NULL,
  superseded_at TEXT,
  address_at_record TEXT                    -- the apartment's address when recorded: the fact is bound to THIS apartment, not to a reused row
);
CREATE INDEX IF NOT EXISTS ix_apartment_access_facts_current ON apartment_access_facts(apartment_id, fact, superseded_at);
"""


# Additive migrations only (never drop, rename or retype): (table, column, DDL fragment)
MIGRATIONS = [
    # 11 Nov 2026 G-SL2: the building identity of the transition apartment, captured WITH pending follow-up work so a follow-up that
    # runs after the apartment row is deleted can answer "did a member decline this building?" from the members' records (no inference);
    # '' = captured, no building identity; NULL = not captured / cleared when the transition's last pending row closed. Never backfilled.
    ("stock_transitions", "building_key", "TEXT"),
    # 11 Nov 2026 GP14 (MateBot): the enquiry's current apartment/building exclusions, kept in step with apartment_rejections in the same
    # write so every path that builds a rules.Unit from the enquiry row (SELECT *) carries them - see apartment_rejections.refresh_exclusions
    ("enquiries", "apartment_exclusions_json", "TEXT"),
    # 27 Oct 2026 MateBot AI upgrade: GP11 linked friends
    ("enquiries", "linked_to_enquiry_id", "INTEGER"),      # the enquirer whose "Bring a mate" link this person used (root of the keep-together party)
    ("enquiries", "mate_note", "TEXT"),                     # plain-language note shown to staff about the latest mate action
    ("party_members", "sex", "TEXT"),
    ("party_members", "sex_other", "TEXT"),
    ("party_members", "dob", "TEXT"),
    ("party_members", "needs_guardian", "INTEGER NOT NULL DEFAULT 0"),
    ("party_members", "birth_country_raw", "TEXT"),                          # I16(b), 26 Oct 2026
    ("party_members", "birth_country", "TEXT"),
    ("party_members", "birth_country_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    # 26 Oct 2026, second update: group-first matching (T6, GP6) and remembered sign-in (T7)
    ("groups", "member_key", "TEXT"),                       # sorted enquiry ids, e.g. "12,45,77": the same people = the same group
    ("groups", "is_primary", "INTEGER NOT NULL DEFAULT 1"),  # 1 = the enquiries' best group (shown to staff); 0 = alternative grouping
    ("groups", "ai_status", "TEXT"),                         # ok / failed / skipped
    ("groups", "run_kind", "TEXT"),                          # full / apartment / replacement
    ("match_runs", "kind", "TEXT"),                          # full / apartment
    ("match_runs", "status", "TEXT"),                        # running / done / failed / interrupted
    ("match_runs", "dataset", "TEXT"),
    ("match_runs", "progress_json", "TEXT"),
    ("match_runs", "pairs_compared", "INTEGER"),
    ("match_runs", "groups_total", "INTEGER"),
    ("match_runs", "ai_calls", "INTEGER"),
    ("match_runs", "ai_failed", "INTEGER"),
    ("match_runs", "duration_s", "REAL"),
    ("sessions", "kind", "TEXT"),                            # T7: 'remembered' (30-day device session) or 'short'
    ("sessions", "visitor_hash", "TEXT"),                    # T7 fallback: sha256 of the hosting proxy's X-Visitor-Id
    ("sessions", "last_seen_at", "TEXT"),
    ("sessions", "user_agent", "TEXT"),
    # 26 Oct 2026, fourth update: I16(f) couples stated by the enquirer (3.10), GP8 leased apartments, GP7/GP8 group flags
    ("enquiries", "couples_answer", "TEXT"),                 # 3.10: 'No' / 'Yes' for "With friends"; NULL = not asked (saved before 3.10 existed, or not a friends party)
    ("enquiries", "couples_json", "TEXT"),                   # 3.10: JSON list of pairs of person refs, "lead" or "m1".."m6" (= party_members in id order), e.g. [["lead","m1"],["m2","m3"]]
    ("apartments", "leased_at", "TEXT"),                     # GP8: set when reservations marks the apartment Leased; NULL = not leased
    ("apartments", "leased_group_id", "INTEGER"),            # GP8: the group that leased it (if leased through MMAI)
    ("apartments", "leased_by", "TEXT"),
    ("groups", "rerank_note", "TEXT"),                       # GP8: set when this group's apartments were re-ranked because an apartment was leased
    ("groups", "reranked_at", "TEXT"),
    ("groups", "replaced_from_group_id", "INTEGER"),         # GP7/GP3: this group replaced members of that group
    ("groups", "rejected_json", "TEXT"),                     # GP7: enquiry ids rejected to make this group
    # 26 Oct 2026, fifth update: T8 same-second race. Highest enquiry id (AUTOINCREMENT, never reused) in the data set when the run read
    # its pool; the scheduler counts "new applicants" as pool enquiries with id > this, instead of created_at > started_at (1 s precision).
    ("match_runs", "pool_watermark_id", "INTEGER"),
    # 26 Oct 2026, sixth update: GP9(c) staff bar from matching (whole enquiry = whole party) and GP10 groups awaiting a replacement
    ("enquiries", "barred_at", "TEXT"),                     # GP9(c): set while the person (and their party) is barred from every run and every replacement; NULL = not barred
    ("enquiries", "barred_by", "TEXT"),
    ("enquiries", "barred_reason", "TEXT"),                 # required when barring
    ("enquiries", "barred_person", "TEXT"),                 # GP9(e), 28 Oct 2026: the one person on this form who is barred, when the rest of the party chose to leave with them (NULL = the enquirer)
    ("groups", "awaiting_note", "TEXT"),                     # GP10: why this group is awaiting a replacement (set when a rejection found no rule-passing refill)
    ("groups", "awaiting_since", "TEXT"),
    ("groups", "awaiting_people_missing", "INTEGER"),        # GP10: people the group is short of (empty beds, GP5 exception)
    ("groups", "discount_note", "TEXT"),                     # GP10(b): staff's free-text note about any discount offered; prices are never changed by MMAI
    ("groups", "replacement_json", "TEXT"),                  # GP10: rule-passing replacement found by a later run, for staff to accept or decline (never applied automatically)
    ("groups", "replacement_found_at", "TEXT"),
    ("groups", "replacement_checked_at", "TEXT"),            # GP10: last run that looked for a replacement for this group
    # 26 Oct 2026, seventh update: T6 AI timing. Hourly runs form groups with the rules only; the AI queue assesses every new or changed
    # group before it is shown to reservations as ready. `ai_status` (existing column) now carries the queue state: pending / running / ok / failed.
    ("groups", "ai_queued_at", "TEXT"),                      # T6: when the group was (last) queued for AI assessment
    ("groups", "ai_assessed_at", "TEXT"),                    # T6: when the AI last assessed this group (members as they are now)
    ("groups", "ai_attempts", "INTEGER"),                    # T6: AI calls made for this group since it was last queued (retries counted)
    ("groups", "ai_error", "TEXT"),                          # T6: last AI error for this group (cleared on success)
    ("groups", "ai_priority", "INTEGER"),                    # T6: 0 = member change / replacement, 1 = new proposed group, 2 = alternative grouping, 3 = refresh (GP8(b))
    ("groups", "ai_refresh", "INTEGER"),                     # T6/GP8(b): 1 while a 'ready' group waits for a background refresh of its apartment ranking (members unchanged)
    ("groups", "awaiting_reason", "TEXT"),                   # GP9(d)/GP10: 'rejection' or 'bar' (why the group lost a member)
    # 26 Oct 2026, eighth update (T10): why a group carries a note, as data rather than as a register code inside the note text.
    # 'lease' (re-ranked after an apartment was leased, GP8), 'rejection' (GP7), 'bar' (GP9), 'replacement' (GP10 accepted), 'dissolved' (staff)
    ("groups", "flag_kind", "TEXT"),
    # 27 Oct 2026: individual staff sign-ins for published staff testing. 1 = sign-in closed (the shared account is kept for audit history).
    ("staff_users", "disabled", "INTEGER NOT NULL DEFAULT 0"),
    ("staff_users", "created_at", "TEXT"),
    ("staff_users", "pw_changed_at", "TEXT"),
    # 31 Oct 2026 submission reliability: a saved draft remembers the form's submission key so a retry after resuming replays, never duplicates
    ("form_drafts", "submit_key", "TEXT"),
    ("form_drafts", "origin", "TEXT"),   # H5 hand-off (9 Nov 2026): 'homebot' when the saved form was started from HomeBot's hand-off screen
    # 31 Oct 2026 review hardening: the submission key is written on the enquiry row itself in the same INSERT, with a UNIQUE index
    # (ux_enq_submission_key, created in init_schema), so the database refuses a second enquiry for one key whatever the
    # application code does (stalled original request, crash, concurrency). NULL for enquiries made without a key (staff tools,
    # seed data, legacy clients): NULLs never collide.
    ("enquiries", "submission_key", "TEXT"),
    ("enquiry_submissions", "owner", "TEXT"),
    # 8 Nov 2026 (I8 / I11(d)): sex, date of birth, age and sex preferences are no longer collected or used. The legacy columns
    # stay (never dropped; new rows store '' = not collected in the NOT NULL ones). The ONLY source of adult eligibility is the
    # enquirer's explicit declaration that EVERY occupant will be 18 or older on the move-in date, recorded with what it covered.
    ("enquiries", "adults_confirmed", "INTEGER NOT NULL DEFAULT 0"),   # 0 for every pre-existing row: unconfirmed, staff to confirm (never backfilled)
    ("enquiries", "adults_confirmed_by", "TEXT"),                      # 'enquirer' or 'staff:<name>'
    ("enquiries", "adults_confirmed_at", "TEXT"),
    ("enquiries", "adults_confirmed_move_in", "TEXT"),                 # the move-in date the declaration was made for (an earlier date needs a renewed declaration)
    ("enquiries", "adults_confirmed_party", "TEXT"),                   # fingerprint of the party members it covered (a changed party needs a renewed declaration)
    # 8 Nov 2026: apartment and building features (Bill's list of 20, server/apartment_features.py). Per apartment each feature is
    # NULL = unknown (the default until someone records it), 1 = yes, 0 = no; the car-parking charge is free text, never derived.
    *[("apartments", f"feat_{k}", "INTEGER") for k in _FEATURE_KEYS],
    ("apartments", "parking_charge", "TEXT"),
    ("enquiries", "features", "TEXT"),                                 # 3.11: JSON list of wanted feature labels (optional; wishes, not filters)
    ("enquiries", "features_top", "TEXT"),                             # 3.12: JSON list, up to three of those that matter most (optional)
    ("homebot_enquiries", "extras_top", "TEXT"),                       # HomeBot: up to three of the ticked extras that matter most (optional)
    # 8 Nov 2026 (T6 review workflow): current review state on the group row; history lives in review_rounds / group_briefs (append-only).
    ("groups", "rule_version", "TEXT"),                                # rule register version the current review/briefs were made under
    ("groups", "evidence_hash", "TEXT"),                               # hash of the evidence packet reviewed (a changed packet makes the review stale)
    ("groups", "evidence_json", "TEXT"),                               # the packet itself (refs and facts only, no identity or contact details)
    ("groups", "review_verdict", "TEXT"),                              # 'approve' | 'revise' | 'hold'
    ("groups", "held_reason", "TEXT"),                                 # plain-language reason shown to staff while held
    ("groups", "brief_status", "TEXT"),                                # 'pending' | 'ok' | 'failed' | 'stale' (per-person AND group brief; anything but ok = not staff-ready)
    ("groups", "revision_of", "INTEGER"),                              # group this one revised (bounded revision chain)
    ("groups", "revision_round", "INTEGER"),
    ("groups", "ready_at", "TEXT"),                                    # when the group last met the full readiness predicate
    # 8 Nov 2026 (Bill: sex and current age restored SOLELY for introductions, no role in matching). Fresh, optional answers per
    # person (server/intro_profile.py); never populated from the retired sex/dob columns, never read by matching/eligibility/AI review.
    ("enquiries", "intro_sex", "TEXT"),                                # Female / Male / Another term / Prefer not to say
    ("enquiries", "intro_sex_other", "TEXT"),                          # the person's own term when "Another term"
    ("enquiries", "intro_age", "INTEGER"),                             # current age in whole years as stated (not a date of birth)
    ("party_members", "intro_sex", "TEXT"),
    ("party_members", "intro_sex_other", "TEXT"),
    ("party_members", "intro_age", "INTEGER"),
    # 23 Nov 2026 (Bill): the culture / language-interest question replaces "countries to avoid" (5.4). culture_interest is the one
    # required answer; the three lists are optional JSON lists (at most three choices in all). Older rows keep these NULL (no backfill).
    ("enquiries", "culture_interest", "TEXT"),
    ("enquiries", "culture_languages", "TEXT"),
    ("enquiries", "culture_regions", "TEXT"),
    ("enquiries", "culture_countries", "TEXT"),
    # 8 Nov 2026 (T5/T6 history): immutable ref map captured by the writer BEFORE each AI call - JSON {"U1": {"enquiry_id": 12,
    # "party_member_id": null}, "U1-m1": {...}} (metadata only, never in a prompt). History display names people from THIS map, never
    # from the group's current members; T5 deletion finds a person's earlier rounds through it. NULL on legacy rows = "unavailable".
    ("review_rounds", "refs_json", "TEXT"),
    ("group_briefs", "refs_json", "TEXT"),
    # 9 Nov 2026 (GP13 / GP10(b)): how a membership version's 24 h clock was anchored - closed kinds 'introduction' (the staff Introduced
    # action) or 'remaining_group' (staff recorded the remaining group + empty space as explained to everyone); the latter also keeps the
    # digest of the full context shown. An ordinary introduction never authorises the awaiting-replacement exception.
    ("group_membership_versions", "anchor_kind", "TEXT"),
    ("group_membership_versions", "disclosure_hash", "TEXT"),
    # 9 Nov 2026 (GAP-T4c v2): practical-notes extraction state on the enquiry row. notes_status NULL/'pending' = not yet read (a group
    # with such a member is not reviewed: fail closed), 'none' = the person wrote nothing, 'ok' = validated, 'failed' = the bounded
    # extraction call failed (staff retry re-queues it). notes_packet_json is the NEUTRAL projection the packet reads (part of the
    # compatibility evidence, so an edited note makes the review stale); notes_input_key is the cache key of the text it was made from.
    ("enquiries", "notes_status", "TEXT"),
    ("enquiries", "notes_packet_json", "TEXT"),
    ("enquiries", "notes_input_key", "TEXT"),
    ("enquiries", "notes_residual", "INTEGER"),                        # staff-only 'unreviewed note' marker (1 = something practical outside the catalogue)
    ("enquiries", "notes_updated_at", "TEXT"),
    ("enquiries", "lead_answers_unverified_at", "TEXT"),   # GP9(e) takeover: the row's INDIVIDUAL answers were supplied by the former enquirer; set until staff confirm them with the current one
    # 9 Nov 2026 (H19 lease alert, fifth repair): durable count of lease claims on the apartment. Every claim adds 1 (the compare-and-swap
    # in app._claim_apartment and matching.mark_apartment_leased's legacy path); a relist leaves it. The alert raised for a group
    # when a previously suggested apartment is leased is keyed on (group, apartment, this number): a retry of the same lease's rerank
    # finds the same key (no duplicate), the next lease of the same apartment after a relist gets a new one (never suppressed).
    ("apartments", "lease_generation", "INTEGER NOT NULL DEFAULT 0"),
    # 9 Nov 2026 (stock lifecycle): display-only stamp, set when an Introduced (or later) group's current offer changed (pointer
    # moved, went NULL, or its material facts were rewritten) since the introduction. Cleared only by the explicit audited staff
    # acknowledgement of the current introduction and offer (app.py). Never touches the membership version or the 24 h clock.
    ("groups", "offer_changed_since_intro_at", "TEXT"),
    # 10 Nov 2026 (T3(d) C3, A13(a)): the CURRENT recorded step-free fact of the apartment and its provenance (history in
    # apartment_access_facts). 1 = completely step-free route confirmed, 0 = recorded as not step-free, NULL = not recorded.
    ("apartments", "step_free_access", "INTEGER"),
    ("apartments", "step_free_source", "TEXT"),
    ("apartments", "step_free_recorded_by", "TEXT"),
    ("apartments", "step_free_recorded_at", "TEXT"),
    # 10 Nov 2026 (T3(d) C4): bounded re-extraction of a member's notes across passes with backoff (never a silent hot retry)
    ("enquiries", "notes_attempts", "INTEGER"),             # failed extraction rounds for the current input tuple
    ("enquiries", "notes_next_retry_at", "TEXT"),           # scheduled time of the next automatic attempt (NULL = due now / not scheduled)
    # A10 total-only offer identity. Old ordinary offers remain NULL/NULL.
    # 16 Nov 2026 (H31 / H28(c) / GP6(d)): the optional travel choices question (JSON list of Walk / Bicycle / Public transport).
    # NULL = no answer: every enquiry saved before the question existed, and any that leaves it blank. Never inferred.
    ("enquiries", "travel_modes", "TEXT"),
    ("group_apartments", "exception_ref", "TEXT"),
    ("group_apartments", "exception_total", "TEXT"),
    ("groups", "exception_ref", "TEXT"),
    ("groups", "exception_total", "TEXT"),
    ("apartments", "step_free_record_id", "INTEGER"),       # id of the current apartment_access_facts row (fact generation; part of review evidence)
    ("apartments", "step_free_address", "TEXT"),            # address when recorded: a changed/reused apartment row never inherits the record
    ("apartment_access_facts", "address_at_record", "TEXT"),
    # A10 immutable dataset provenance. Legacy rows stay unknown, never guessed/backfilled.
    ("exit_exceptions", "is_test", "INTEGER"),
]

# Indexes that depend on migrated columns (CREATE TABLE in SCHEMA cannot carry them for databases created earlier). Additive only.
MIGRATION_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_enq_submission_key ON enquiries(submission_key)",
]


MIGRATION_LOCK_KEY = 7_311_902   # PostgreSQL session-level advisory lock: one migrator at a time across processes


def _pg_schema_guard(c) -> None:
    """A wrong-encoded database made the information_schema column lookup return nothing (9 Nov 2026 incident: a SQL_ASCII database
    led `migrate` to re-add an existing column). Fail loudly before any DDL instead of half-applying."""
    enc = c.execute("SHOW server_encoding").fetchone()[0]
    if str(enc).upper().replace("-", "") != "UTF8":
        raise RuntimeError(f"database encoding {enc}: MateBot AI requires a UTF8 database; migration refused")
    if "apartments" in set(c.table_names()) and not c.table_columns("apartments"):
        raise RuntimeError("schema introspection returned no columns for an existing table; migration refused")


def migrate(c):
    pg = getattr(c, "backend", "sqlite") == "postgres"
    if pg:
        _pg_schema_guard(c)
    for table, col, ddl in MIGRATIONS:
        have = table_columns(c, table)
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {translate_ddl(ddl) if pg else ddl}")
    for ddl in MIGRATION_INDEXES:
        c.execute(ddl)
    c.commit()
    _migrate_groups_apartment_fk(c)
    from . import stock_lifecycle
    stock_lifecycle.bootstrap(c)


def _groups_fk_state(c) -> tuple[bool, str | None]:
    """(apartment_id NOT NULL?, ON DELETE action of its foreign key) as the database has it now."""
    if getattr(c, "backend", "sqlite") == "postgres":
        nn = c.execute("SELECT attnotnull FROM pg_attribute WHERE attrelid='groups'::regclass AND attname='apartment_id' AND NOT attisdropped").fetchone()
        con = c.execute("""SELECT con.conname, pg_get_constraintdef(con.oid) FROM pg_constraint con
                           WHERE con.conrelid='groups'::regclass AND con.contype='f'
                             AND (SELECT attname FROM pg_attribute WHERE attrelid=con.conrelid AND attnum=con.conkey[1])='apartment_id'""").fetchone()
        action = None
        if con:
            d = str(con[1]).upper()
            action = "SET NULL" if "ON DELETE SET NULL" in d else "CASCADE" if "ON DELETE CASCADE" in d else "NO ACTION"
        return bool(nn and nn[0]), action
    info = {r[1]: r for r in c.execute("PRAGMA table_info(groups)").fetchall()}
    fks = [r for r in c.execute("PRAGMA foreign_key_list(groups)").fetchall() if r[3] == "apartment_id"]
    return bool(info["apartment_id"][3]), (str(fks[0][6]).upper() if fks else None)


def _migrate_groups_apartment_fk(c) -> bool:
    """9 Nov 2026 stock lifecycle: groups.apartment_id NOT NULL ... ON DELETE CASCADE -> nullable ... ON DELETE SET NULL, once.
    PostgreSQL alters in place (constraint name introspected). SQLite rebuilds the table from its LIVE definition (every migrated
    column, defaults, the match_run_id FK and other constraints kept; the apartment_id definition alone rewritten), with foreign
    keys OFF around the rebuild so no child row is touched, then indexes/triggers recreated, the AUTOINCREMENT high-water mark
    restored and `PRAGMA foreign_key_check` run; any failure rolls the whole rebuild back. Returns True when it changed something."""
    notnull, action = _groups_fk_state(c)
    if not notnull and action == "SET NULL":
        return False
    if getattr(c, "backend", "sqlite") == "postgres":
        if notnull:
            c.execute("ALTER TABLE groups ALTER COLUMN apartment_id DROP NOT NULL")
        con = c.execute("""SELECT con.conname FROM pg_constraint con WHERE con.conrelid='groups'::regclass AND con.contype='f'
                           AND (SELECT attname FROM pg_attribute WHERE attrelid=con.conrelid AND attnum=con.conkey[1])='apartment_id'""").fetchone()
        if con:
            c.execute(f'ALTER TABLE groups DROP CONSTRAINT "{con[0]}"')
        c.execute("ALTER TABLE groups ADD CONSTRAINT groups_apartment_id_fkey FOREIGN KEY (apartment_id) REFERENCES apartments(id) ON DELETE SET NULL")
        c.commit()
        return True
    return _sqlite_rebuild_groups(c)


_CHILD_TABLES_OF_GROUPS = ("group_members", "group_apartments", "review_rounds", "group_briefs", "group_membership_versions",
                           "group_response_links", "group_responses", "offer_reconcile_work")


def _sqlite_rebuild_groups(c) -> bool:
    if getattr(c, "_guard_depth", 0) or c.in_transaction:
        raise RuntimeError("groups table rebuild needs an idle connection (no guard, no open transaction)")
    live = c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='groups'").fetchone()
    if not live:
        return False
    ddl = live[0]
    pat = re.compile(r"apartment_id\s+INTEGER\s+NOT\s+NULL\s+REFERENCES\s+apartments\s*\(\s*id\s*\)\s+ON\s+DELETE\s+CASCADE", re.I)
    if len(pat.findall(ddl)) != 1:
        raise RuntimeError("groups.apartment_id definition not recognised; rebuild refused")
    new_ddl = pat.sub("apartment_id INTEGER REFERENCES apartments(id) ON DELETE SET NULL", ddl)
    new_ddl = re.sub(r"CREATE TABLE\s+(IF NOT EXISTS\s+)?[\"`]?groups[\"`]?", "CREATE TABLE groups_new", new_ddl, count=1, flags=re.I)
    cols = [r[1] for r in c.execute("PRAGMA table_info(groups)").fetchall()]
    old_info = [tuple(r) for r in c.execute("PRAGMA table_info(groups)").fetchall()]
    old_fks = sorted(tuple(r)[2:] for r in c.execute("PRAGMA foreign_key_list(groups)").fetchall())
    extras = [r[0] for r in c.execute("SELECT sql FROM sqlite_master WHERE tbl_name='groups' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()]
    n_extras = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE tbl_name='groups' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchone()[0]
    seq_row = c.execute("SELECT seq FROM sqlite_sequence WHERE name='groups'").fetchone()
    old_seq = int(seq_row[0]) if seq_row else 0
    existing_children = [t for t in _CHILD_TABLES_OF_GROUPS if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()]
    child_counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in existing_children}
    n_rows = c.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    fk_was_on = bool(c.execute("PRAGMA foreign_keys").fetchone()[0])
    c.execute("PRAGMA foreign_keys=OFF")        # must happen outside a transaction (a no-op inside one)
    try:
        c._raw_execute("BEGIN IMMEDIATE")
        try:
            c.execute(new_ddl)
            new_info = [tuple(r) for r in c.execute("PRAGMA table_info(groups_new)").fetchall()]
            if [x[1:] for x in new_info if x[1] != "apartment_id"] != [x[1:] for x in old_info if x[1] != "apartment_id"]:
                raise RuntimeError("rebuilt groups table differs beyond apartment_id; rebuild refused")
            new_fks = sorted(tuple(r)[2:] for r in c.execute("PRAGMA foreign_key_list(groups_new)").fetchall())
            if [f for f in new_fks if f[1] != "apartment_id"] != [f for f in old_fks if f[1] != "apartment_id"]:
                raise RuntimeError("rebuilt groups table lost a foreign key; rebuild refused")
            collist = ", ".join(cols)
            c.execute(f"INSERT INTO groups_new({collist}) SELECT {collist} FROM groups")
            if c.execute("SELECT COUNT(*) FROM groups_new").fetchone()[0] != n_rows:
                raise RuntimeError("row count mismatch during groups rebuild")
            c.execute("DROP TABLE groups")
            c.execute("ALTER TABLE groups_new RENAME TO groups")
            for sql in extras:
                c.execute(sql)
            if c.execute("SELECT COUNT(*) FROM sqlite_master WHERE tbl_name='groups' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchone()[0] != n_extras:
                raise RuntimeError("index/trigger count mismatch after groups rebuild")
            max_id = c.execute("SELECT COALESCE(MAX(id),0) FROM groups").fetchone()[0]
            target = max(old_seq, int(max_id))
            if c.execute("UPDATE sqlite_sequence SET seq=? WHERE name='groups'", (target,)).rowcount == 0:
                c.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('groups', ?)", (target,))
            seqs = c.execute("SELECT seq FROM sqlite_sequence WHERE name='groups'").fetchall()
            if len(seqs) != 1 or int(seqs[0][0]) < target:
                raise RuntimeError("sqlite_sequence high-water mark not restored")
            for t, n in child_counts.items():
                if c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] != n:
                    raise RuntimeError(f"child rows of {t} changed during groups rebuild")
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("foreign key check failed after groups rebuild")
            notnull, action = _groups_fk_state(c)
            if notnull or action != "SET NULL":
                raise RuntimeError("groups.apartment_id still not nullable / SET NULL after rebuild")
            c.commit()
        except BaseException:
            c.rollback()
            raise
    finally:
        if fk_was_on:
            c.execute("PRAGMA foreign_keys=ON")
    _log.info("groups table rebuilt: apartment_id nullable, ON DELETE SET NULL")
    return True


def init_schema(c):
    pg = getattr(c, "backend", "sqlite") == "postgres"
    if pg:
        c.execute("SELECT pg_advisory_lock(?)", (MIGRATION_LOCK_KEY,))
        c.commit()
    try:
        c.executescript(SCHEMA)  # PgConnection.executescript translates AUTOINCREMENT / REAL
        migrate(c)
        c.execute("INSERT OR IGNORE INTO cities(name, active) VALUES ('Melbourne', 1)")  # C1
        c.commit()
    finally:
        if pg:
            try:
                c.execute("SELECT pg_advisory_unlock(?)", (MIGRATION_LOCK_KEY,))
                c.commit()
            except Exception:  # noqa: BLE001 - a lost session releases the lock by itself
                pass


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def row(cur):
    r = cur.fetchone()
    return dict(r) if r else None


def j(v):
    return json.dumps(v) if v is not None else None


def pj(s, default=None):
    if s is None or s == "":
        return default if default is not None else []
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else []


def audit(c, staff: str | None, action: str, entity: str | None = None, entity_id=None, detail: str | None = None):
    c.execute("INSERT INTO audit_log(at, staff_member, action, entity, entity_id, detail) VALUES (?,?,?,?,?,?)",
              (now_iso(), staff, action, entity, str(entity_id) if entity_id is not None else None, detail))
    maybe_commit(c)  # never leave a write transaction open (it would lock the file against other connections); inside a retention guard the step commits once


def is_unique_violation(exc: BaseException) -> bool:
    """True when `exc` is the database refusing a duplicate key (SQLite IntegrityError / PostgreSQL UniqueViolation)."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc).upper()
    try:
        import psycopg
        return isinstance(exc, psycopg.errors.UniqueViolation)
    except Exception:
        return False


class transaction:
    """Hold the shared connection's lock for a whole multi-statement write, not just per statement, so no other thread (the T8
    scheduler, the AI queue, another request) can slip a commit or rollback in between the statements and commit half of it.
    Commits on a clean exit, rolls back on any exception. Re-entrant (RLock), so nested use and audit() inside are fine.
    Synchronous only: never hold it across an await. Statements run by other threads wait until the block ends.
    Note: a write another thread had left uncommitted when the block started is committed with it (shared connection, as before).
    Inside an active retention guard it is a NESTED block: no commit on exit (the outer guard commits the step once); an
    exception marks the step rollback-only and propagates."""

    def __init__(self, c):
        self.c = c
        self._nested = False

    def __enter__(self):
        self.c._mmai_lock.acquire()
        self._nested = guarded(self.c)
        return self.c

    def __exit__(self, et, ev, tb):
        try:
            if self._nested:
                if et is not None:
                    self.c._guard_rollback_only = True
                return False
            if et is None:
                self.c.commit()
            else:
                try:
                    self.c.rollback()
                except Exception:
                    pass
        finally:
            self.c._mmai_lock.release()
        return False
