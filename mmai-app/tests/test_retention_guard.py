"""T5 retention guard (db.retention_guard, 8 Nov 2026): the one DB-enforced lock shared by every writer of generated text about
people and every deleter of people, so a deletion on ANOTHER connection can never commit between a writer's identity check and
its history insert. Unit coverage of the contract (SQLite real connections; PostgreSQL adapter with a fake session):
  * outer entry needs a clean connection (refused, pending work untouched); nested same-thread guards share the outer's
    transaction and commit once; an inner failure makes the step rollback-only;
  * c.commit()/c.rollback() inside a guard raise and release nothing; maybe_commit / audit / transaction compose;
  * SQLite: BEGIN IMMEDIATE at entry - a second connection's write waits until the step commits; deleter-first vs writer-first;
  * PostgreSQL adapter: no read auto-commit while guarded, lock statements first, no reconnect with pending work
    (ConnectionLostMidOperation; outcome unknown only when the loss is during commit), lazy re-open before the next outer guard.
Two-connection PostgreSQL behaviour is covered separately (tests/test_retention_two_connections.py, needs the cluster)."""
import os
import sqlite3
import threading

import pytest

from server import db as dbm
from server.db import (CommitInsideRetentionGuard, ConnectionLostMidOperation, DirtyConnectionAtGuardEntry, GuardRolledBack,
                       RetentionLockTimeout, audit, guarded, maybe_commit, retention_guard, transaction, whole_operation)


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """Two real SQLite connections on one WAL file (the shared app connection and a second connection, as in a tools/ script)."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = str(tmp_path / "guard.db")
    a = dbm.connect(path)
    b = dbm.connect(path)
    yield a, b
    a.close()
    b.close()


def test_whole_operation_is_the_guard_and_guarded_reports_depth(pair):
    a, _ = pair
    assert whole_operation is retention_guard
    assert guarded(a) is False
    with retention_guard(a, label="t"):
        assert guarded(a) is True
        with retention_guard(a):          # nested, same thread
            assert a._guard_depth == 2
        assert a._guard_depth == 1
    assert guarded(a) is False and a.in_transaction is False


def test_outer_entry_refuses_a_dirty_connection_and_leaves_the_pending_work_alone(pair):
    a, b = pair
    a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'pending')")   # uncommitted write
    assert a.in_transaction
    with pytest.raises(DirtyConnectionAtGuardEntry):
        with retention_guard(a, label="x"):
            pass
    assert a.in_transaction, "the guard must not commit or roll back work it does not own"
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='pending'").fetchone()[0] == 0
    a.commit()
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='pending'").fetchone()[0] == 1


def test_commit_and_rollback_inside_a_guard_raise_and_release_nothing(pair):
    """c.commit()/c.rollback() inside a guard raise, release no lock and commit nothing; the attempt also poisons the step
    (rollback-only, see test_sqlite_forbidden_control_caught_by_a_helper_still_rolls_the_whole_step_back)."""
    a, b = pair
    with pytest.raises(GuardRolledBack):
        with retention_guard(a):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'one')")
            with pytest.raises(CommitInsideRetentionGuard):
                a.commit()
            assert a.in_transaction and guarded(a)
            with pytest.raises(CommitInsideRetentionGuard):
                a.rollback()
            assert a.in_transaction and guarded(a)
            assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert not a.in_transaction and b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_maybe_commit_audit_and_transaction_compose_and_the_step_commits_once(pair):
    a, b = pair
    with retention_guard(a):
        a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'one')")
        maybe_commit(a)                                   # no-op inside the guard
        audit(a, "s", "two")                              # audit commits only outside guards
        with transaction(a):                              # nested-aware: no commit on exit
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'three')")
        assert a.in_transaction
        assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0, "nothing visible to another connection before the step commits"
    assert not a.in_transaction
    assert [r[0] for r in b.execute("SELECT action FROM audit_log ORDER BY id").fetchall()] == ["one", "two", "three"]


def test_exception_rolls_the_whole_step_back_and_is_not_masked(pair):
    a, b = pair
    with pytest.raises(KeyError):
        with retention_guard(a):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'lost')")
            raise KeyError("boom")
    assert not a.in_transaction and not guarded(a)
    assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_inner_failure_caught_by_the_outer_makes_the_step_rollback_only(pair):
    a, b = pair
    with pytest.raises(GuardRolledBack):
        with retention_guard(a, label="outer"):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'outer')")
            try:
                with retention_guard(a, label="inner"):
                    a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'inner')")
                    raise ValueError("inner failed")
            except ValueError:
                pass                                        # the outer carries on: it must NOT be allowed to commit half a step
            assert a._guard_rollback_only is True
    assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
    assert not guarded(a) and not a.in_transaction


def test_nested_transaction_failure_also_marks_rollback_only(pair):
    a, b = pair
    with pytest.raises(GuardRolledBack):
        with retention_guard(a):
            try:
                with transaction(a):
                    a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'x')")
                    raise RuntimeError("inner")
            except RuntimeError:
                pass
    assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_sqlite_second_connection_write_waits_until_the_guarded_step_commits(pair):
    """Writer-first: connection B's delete cannot commit while A's guarded step is open; it proceeds right after A commits."""
    a, b = pair
    a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'victim')")
    a.commit()
    entered, release, done = threading.Event(), threading.Event(), {}

    def deleter():
        entered.set()
        try:
            with retention_guard(b, label="deleter"):
                b.execute("DELETE FROM audit_log WHERE action='victim'")
                done["saw_history_row"] = b.execute("SELECT COUNT(*) FROM audit_log WHERE action='history'").fetchone()[0]
                b.execute("DELETE FROM audit_log WHERE action='history'")
        except Exception as e:  # pragma: no cover
            done["error"] = repr(e)

    with retention_guard(a, label="writer"):
        assert a.execute("SELECT COUNT(*) FROM audit_log WHERE action='victim'").fetchone()[0] == 1   # identity check
        t = threading.Thread(target=deleter, daemon=True)
        t.start()
        entered.wait(5)
        a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'history')")    # the write the check protects
        assert a.execute("SELECT COUNT(*) FROM audit_log WHERE action='victim'").fetchone()[0] == 1, "the deleter is still waiting"
    t.join(20)
    assert not t.is_alive() and "error" not in done, done
    # DB causality: the deleter's step saw the writer's committed history row and scrubbed it; nothing of either remains
    assert done["saw_history_row"] == 1
    assert a.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('victim', 'history')").fetchone()[0] == 0


def test_sqlite_deleter_first_the_writer_check_sees_the_person_gone(pair):
    a, b = pair
    a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'victim')")
    a.commit()
    with retention_guard(b, label="deleter"):
        b.execute("DELETE FROM audit_log WHERE action='victim'")
    with retention_guard(a, label="writer"):
        assert a.execute("SELECT COUNT(*) FROM audit_log WHERE action='victim'").fetchone()[0] == 0   # discard path: write nothing
    assert a.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_sqlite_busy_timeout_becomes_retention_lock_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = str(tmp_path / "busy.db")
    a = dbm.connect(path)
    b = sqlite3.connect(path, timeout=0.2, check_same_thread=False, factory=dbm.Connection)
    try:
        with retention_guard(a):
            with pytest.raises(RetentionLockTimeout):
                with retention_guard(b):
                    pass
            assert not guarded(b) and not b.in_transaction
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- PostgreSQL adapter with a fake psycopg session

class _FakeCursor:
    description = None
    rowcount = 0

    def __init__(self, sess):
        self.sess = sess

    def execute(self, sql, params=None):
        self.sess.statements.append((sql, params))
        if self.sess.fail_next:
            self.sess.fail_next = False
            raise self.sess.psycopg.OperationalError("server closed the connection unexpectedly")

    def executemany(self, sql, seq):
        self.execute(sql, list(seq))

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, psycopg):
        self.psycopg = psycopg
        self.statements, self.commits, self.rollbacks = [], 0, 0
        self.closed = False
        self.fail_next = False
        self.fail_commit = False

    def cursor(self):
        return _FakeCursor(self)

    isolation = "read committed"

    def execute(self, sql):
        self.statements.append((sql, None))
        if "transaction_isolation" in sql:
            if self.fail_next:
                self.fail_next = False
                raise self.psycopg.OperationalError("server closed the connection unexpectedly")
            return _FakeResult([(self.isolation,)])
        return None

    def commit(self):
        if self.fail_commit:
            self.fail_commit = False
            raise self.psycopg.OperationalError("connection lost during commit")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def pg(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    sessions = []

    def fake_connect(url, **kw):
        s = _FakeSession(psycopg)
        sessions.append(s)
        return s

    monkeypatch.setattr(psycopg, "connect", fake_connect)
    monkeypatch.setattr(dbm.PgConnection, "_pg_row_factory", None, raising=False)
    c = dbm.PgConnection("postgresql://fake/db")
    return c, sessions


def _sql(sess):
    return [s for s, _ in sess.statements if not s.startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))]


def test_pg_guard_takes_the_lock_first_and_never_autocommits_reads_while_held(pg):
    c, sessions = pg
    s = sessions[0]
    c.execute("SELECT 1")                       # idle read: auto-committed as before
    assert s.commits == 1
    with retention_guard(c, label="w"):
        c.execute("SELECT 1 FROM enquiries WHERE id=%s" if False else "SELECT 1 FROM enquiries WHERE id=?", (1,))   # identity check
        c.execute("INSERT INTO review_rounds(group_id) VALUES (?)", (1,))
        assert s.commits == 1, "no commit while the guard holds the transaction"
    assert s.commits == 2 and c._dirty is False and not guarded(c)
    stmts = _sql(s)
    i_lock = next(i for i, x in enumerate(stmts) if "pg_advisory_xact_lock" in x)
    i_cfg = next(i for i, x in enumerate(stmts) if "set_config('lock_timeout'" in x)
    i_chk = next(i for i, x in enumerate(stmts) if "FROM enquiries" in x)
    assert i_cfg < i_lock < i_chk, stmts


def test_pg_outer_entry_refuses_pending_write(pg):
    c, sessions = pg
    c.execute("UPDATE groups SET x=1")
    assert c._dirty
    with pytest.raises(DirtyConnectionAtGuardEntry):
        with retention_guard(c):
            pass
    assert c._dirty and sessions[0].commits == 0 and sessions[0].rollbacks == 0


def test_pg_connection_lost_inside_a_guard_fails_closed_and_reopens_only_on_the_next_idle_operation(pg):
    c, sessions = pg
    s = sessions[0]
    with pytest.raises(ConnectionLostMidOperation) as ei:
        with retention_guard(c, label="w"):
            c.execute("SELECT 1 FROM enquiries WHERE id=?", (1,))
            s.fail_next = True
            c.execute("INSERT INTO review_rounds(group_id) VALUES (?)", (1,))     # the server went away here
    assert ei.value.outcome_unknown is False
    assert s.closed and c._pg is None and not guarded(c) and c._hold_tx is False and c._dirty is False
    assert len(sessions) == 1, "no reconnect inside the guard"
    c.execute("SELECT 1")                       # next idle operation re-opens lazily
    assert len(sessions) == 2 and sessions[1].statements


def test_pg_connection_lost_during_commit_reports_unknown_outcome(pg):
    c, sessions = pg
    s = sessions[0]
    with pytest.raises(ConnectionLostMidOperation) as ei:
        with retention_guard(c, label="w"):
            c.execute("INSERT INTO review_rounds(group_id) VALUES (?)", (1,))
            s.fail_commit = True
    assert ei.value.outcome_unknown is True
    assert s.closed and c._pg is None and not guarded(c)
    # the next OUTER guard re-opens before taking ownership of the new transaction
    with retention_guard(c, label="again"):
        assert len(sessions) == 2 and any("pg_advisory_xact_lock" in x for x in _sql(sessions[1]))
    assert sessions[1].commits == 1


def test_pg_lock_timeout_is_reported_and_nothing_is_held(pg):
    c, sessions = pg
    psycopg = pytest.importorskip("psycopg")
    s = sessions[0]

    class _Cur(_FakeCursor):
        def execute(self, sql, params=None):
            self.sess.statements.append((sql, params))
            if "pg_advisory_xact_lock" in sql:
                raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    s.cursor = lambda: _Cur(s)
    with pytest.raises(RetentionLockTimeout):
        with retention_guard(c):
            pass
    assert not guarded(c) and c._hold_tx is False and s.rollbacks >= 1


def test_pg_nested_guards_commit_once(pg):
    c, sessions = pg
    s = sessions[0]
    with retention_guard(c, label="outer"):
        with retention_guard(c, label="inner"):
            c.execute("INSERT INTO group_briefs(group_id) VALUES (?)", (1,))
            maybe_commit(c)
        audit(c, "s", "x")
        assert s.commits == 0
    assert s.commits == 1 and [x for x in _sql(s) if "pg_advisory_xact_lock" in x].__len__() == 1


def test_unsupported_connections_are_rejected_not_silently_unguarded(tmp_path):
    bare = sqlite3.connect(str(tmp_path / "bare.db"))
    try:
        with pytest.raises(dbm.RetentionGuardError):
            with retention_guard(bare):
                pass
        with pytest.raises(dbm.RetentionGuardError):
            with retention_guard(object()):
                pass
    finally:
        bare.close()


def test_executescript_and_native_with_block_are_refused_inside_a_guard(pair):
    a, b = pair
    with pytest.raises(GuardRolledBack):                 # the refused attempts poison the step
        with retention_guard(a):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'x')")
            with pytest.raises(CommitInsideRetentionGuard):
                a.executescript("SELECT 1;")
            with pytest.raises(CommitInsideRetentionGuard):
                with a:                  # sqlite3's native block commits on exit: refused, the guard owns the transaction
                    a.execute("SELECT 1")
            assert a.in_transaction and guarded(a)
    assert not guarded(a) and not a.in_transaction
    assert b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_guard_events_committed_only_on_success_and_never_rolled_back_after_a_failed_commit(pg):
    c, sessions = pg
    seen = []
    dbm.GUARD_EVENTS.append(lambda ev, conn, label: seen.append((ev, label)))
    try:
        with retention_guard(c, label="ok"):
            c.execute("INSERT INTO t(x) VALUES (?)", (1,))
        assert [e for e, l in seen if l == "ok"] == ["entered", "acquired", "committed", "exit"]
        with pytest.raises(ConnectionLostMidOperation):
            with retention_guard(c, label="lost"):
                c.execute("INSERT INTO t(x) VALUES (?)", (1,))
                sessions[-1].fail_commit = True
        lost = [e for e, l in seen if l == "lost"]
        assert lost == ["entered", "acquired", "commit_failed", "exit"] and "rolled_back" not in lost and "committed" not in lost
    finally:
        dbm.GUARD_EVENTS.clear()


@pytest.mark.parametrize("err_name,expect", [("LockNotAvailable", RetentionLockTimeout), ("DeadlockDetected", None), ("SerializationFailure", None), ("QueryCanceled", None)])
def test_pg_sqlstate_errors_are_statement_errors_not_transport_loss(pg, err_name, expect):
    """55P03 / 40P01 / 40001 / 57014 subclass OperationalError but the session is intact: never ConnectionLostMidOperation, never a
    replay; the lock timeout keeps its own class, the others propagate as themselves (the SAVEPOINT undid the statement)."""
    c, sessions = pg
    psycopg = pytest.importorskip("psycopg")
    err_cls = getattr(psycopg.errors, err_name)
    s = sessions[0]

    class _Cur(_FakeCursor):
        def execute(self, sql, params=None):
            self.sess.statements.append((sql, params))
            if "pg_advisory_xact_lock" in sql or "INSERT" in sql:
                raise err_cls(err_name)

    s.cursor = lambda: _Cur(s)
    if expect is RetentionLockTimeout:
        with pytest.raises(RetentionLockTimeout):
            with retention_guard(c):
                pass
    else:
        s.cursor = lambda: _FakeCursor(s)
        with retention_guard(c):
            s.cursor = lambda: _Cur(s)
            with pytest.raises(err_cls):
                c.execute("INSERT INTO t(x) VALUES (?)", (1,))
            s.cursor = lambda: _FakeCursor(s)
    assert not s.closed and c._pg is s and len(sessions) == 1, "the session stays; nothing was treated as a connection loss"
    assert not guarded(c) and c._hold_tx is False


def test_pg_first_write_on_an_idle_connection_is_never_replayed_after_a_transport_loss(pg):
    """R4 boundary: '_dirty was False before execute' does not justify a replay once a WRITE has been dispatched; only a read-only
    statement on an idle connection is re-opened and replayed."""
    c, sessions = pg
    s = sessions[0]
    s.fail_next = True
    with pytest.raises(ConnectionLostMidOperation) as ei:
        c.execute("INSERT INTO audit_log(at) VALUES (?)", ("t",))
    assert ei.value.outcome_unknown is False and s.closed and c._pg is None and len(sessions) == 1
    c.execute("SELECT 1")                                   # idle again: lazy re-open
    assert len(sessions) == 2
    sessions[1].fail_next = True
    c.execute("SELECT 1")                                   # a read on an idle connection may be replayed on a fresh session
    assert len(sessions) == 3 and c._dirty is False


# ---------------------------------------------------------------- replay classification (R4): only plain reads are ever replayed

@pytest.mark.parametrize("sql,is_read", [
    ("SELECT id FROM enquiries WHERE party='With friends'", True),         # literal 'With' is not a CTE
    ("  select count(*) from audit_log", True),
    ("SHOW server_version", True),
    ("WITH d AS (DELETE FROM enquiries WHERE id=1 RETURNING id) SELECT * FROM d", False),
    ("WITH x AS (SELECT 1) SELECT * FROM x", False),                       # CTEs are outside the replayable scope, read-only or not
    ("EXPLAIN ANALYZE INSERT INTO t(x) VALUES (1)", False),
    ("EXPLAIN SELECT 1", False),
    ("SELECT id FROM enquiries WHERE id IN (1,2) ORDER BY id FOR UPDATE", False),
    ("SELECT setval('s', 10)", False), ("SELECT nextval('s')", False),
    ("SELECT set_config('lock_timeout', '1', true)", False), ("SELECT pg_advisory_xact_lock(1)", False),
    ("SELECT pg_terminate_backend(1)", False), ("SELECT pg_sleep(1)", False),
    ("INSERT INTO t(x) VALUES (1)", False), ("UPDATE t SET x=1", False), ("DELETE FROM t", False),
])
def test_pg_read_classification_is_conservative(sql, is_read):
    assert dbm.PgConnection._is_read(sql) is is_read


@pytest.mark.parametrize("sql", [
    "WITH d AS (DELETE FROM enquiries WHERE id=? RETURNING id) SELECT * FROM d",
    "EXPLAIN ANALYZE INSERT INTO audit_log(at) VALUES (?)",
    "SELECT setval('audit_log_id_seq', ?)",
    "SELECT id FROM enquiries WHERE id=? FOR UPDATE",
])
def test_pg_possibly_writing_statements_are_never_replayed_after_a_first_dispatch_loss(pg, sql):
    """A data-modifying CTE, EXPLAIN ANALYZE of a write, a side-effecting SELECT or a locking read dispatched on an otherwise idle
    connection: a transport loss after dispatch fails closed (no second session, no replay) exactly like a plain INSERT."""
    c, sessions = pg
    sessions[0].fail_next = True
    with pytest.raises(ConnectionLostMidOperation) as ei:
        c.execute(sql, (1,))
    assert ei.value.outcome_unknown is False and sessions[0].closed and c._pg is None and len(sessions) == 1
    assert c._dirty is False and c._idle()                   # flags reset: the next idle operation re-opens lazily
    c.execute("SELECT 1")
    assert len(sessions) == 2


def test_pg_possibly_writing_statement_marks_the_connection_dirty_and_is_not_autocommitted(pg):
    c, sessions = pg
    c.execute("WITH d AS (DELETE FROM enquiries WHERE id=? RETURNING id) SELECT * FROM d", (1,))
    assert c._dirty is True and sessions[0].commits == 0
    maybe_commit(c)
    assert c._dirty is False and sessions[0].commits == 1


# ---------------------------------------------------------------- forbidden transaction control poisons the step

def _try(fn):
    try:
        fn()
    except CommitInsideRetentionGuard:
        return True
    return False


@pytest.mark.parametrize("control", ["commit", "rollback", "with", "executescript"])
def test_sqlite_forbidden_control_caught_by_a_helper_still_rolls_the_whole_step_back(pair, control):
    """A helper that writes, attempts commit()/rollback()/`with c:`/executescript inside the guard, catches the error and returns
    cannot let the outer guard commit a partial step: the attempt poisons the step and the outer exit raises GuardRolledBack."""
    a, b = pair
    with pytest.raises(GuardRolledBack):
        with retention_guard(a, label="step"):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'partial')")
            if control == "commit":
                assert _try(a.commit)
            elif control == "rollback":
                assert _try(a.rollback)
            elif control == "executescript":
                assert _try(lambda: a.executescript("SELECT 1;"))
            else:
                def native():
                    with a:
                        a.execute("SELECT 1")
                assert _try(native)
            assert a._guard_rollback_only is True
    assert not guarded(a) and not a.in_transaction
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='partial'").fetchone()[0] == 0


def test_sqlite_constraint_error_inside_a_guard_does_not_poison_the_step(pair):
    """Expected SQL errors are undone by the statement and leave the step committable (distinct from forbidden control)."""
    a, b = pair
    with retention_guard(a, label="step"):
        a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'kept')")
        with pytest.raises(sqlite3.OperationalError):
            a.execute("INSERT INTO no_such_table(x) VALUES (1)")
        assert a._guard_rollback_only is False
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='kept'").fetchone()[0] == 1


@pytest.mark.parametrize("control", ["commit", "rollback", "executescript"])
def test_pg_forbidden_control_caught_inside_the_guard_rolls_the_step_back(pg, control):
    c, sessions = pg
    s = sessions[0]
    with pytest.raises(GuardRolledBack):
        with retention_guard(c, label="step"):
            c.execute("INSERT INTO t(x) VALUES (?)", (1,))
            fn = {"commit": c.commit, "rollback": c.rollback, "executescript": lambda: c.executescript("SELECT 1;")}[control]
            assert _try(fn)
    assert s.commits == 0 and s.rollbacks == 1 and c._dirty is False and not guarded(c)


# ---------------------------------------------------------------- observer hooks never change the guard's outcome

class _Abort(BaseException):
    pass


def _raise_on(events, exc):
    def hook(ev, conn, label):
        if ev in events:
            raise exc(ev)
    return hook


def test_observer_exceptions_on_entered_and_acquired_are_logged_and_the_step_proceeds(pair):
    a, b = pair
    dbm.GUARD_EVENTS.append(_raise_on({"entered", "acquired", "committed", "exit"}, RuntimeError))
    try:
        with retention_guard(a, label="ok"):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'observed')")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert not guarded(a) and not a.in_transaction and a._hold_tx is False
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='observed'").fetchone()[0] == 1


@pytest.mark.parametrize("event", ["entered", "acquired"])
def test_sqlite_observer_abort_at_entry_leaves_no_flags_transaction_or_lock_held(pair, event):
    """A BaseException from an entry observer propagates, but only after the guard has ended any transaction it opened, reset
    its flags and released the RLock: a second connection can write at once and the next guard on `a` works normally."""
    a, b = pair
    dbm.GUARD_EVENTS.append(_raise_on({event}, _Abort))
    try:
        with pytest.raises(_Abort):
            with retention_guard(a, label="abort"):
                pytest.fail("the body must not run")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert a._guard_depth == 0 and a._hold_tx is False and a._guard_rollback_only is False and not a.in_transaction
    assert a._mmai_lock.acquire(blocking=False); a._mmai_lock.release()
    b.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'after-abort')")   # no BEGIN IMMEDIATE left behind
    b.commit()
    with retention_guard(a, label="next"):
        a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'next')")
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action IN ('after-abort','next')").fetchone()[0] == 2


@pytest.mark.parametrize("event", ["entered", "acquired"])
def test_pg_observer_abort_at_entry_rolls_back_the_lock_transaction_and_resets_flags(pg, event):
    c, sessions = pg
    s = sessions[0]
    dbm.GUARD_EVENTS.append(_raise_on({event}, _Abort))
    try:
        with pytest.raises(_Abort):
            with retention_guard(c, label="abort"):
                pytest.fail("the body must not run")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert c._guard_depth == 0 and c._hold_tx is False and c._dirty is False and s.commits == 0
    assert s.rollbacks == 1, "the advisory-lock transaction opened at entry (or nothing, before it) is ended by a rollback"
    assert c._mmai_lock.acquire(blocking=False); c._mmai_lock.release()
    with retention_guard(c, label="next"):
        c.execute("INSERT INTO t(x) VALUES (?)", (1,))
    assert s.commits == 1


def test_observer_exception_during_rollback_events_does_not_mask_the_original_error(pair):
    a, b = pair
    dbm.GUARD_EVENTS.append(_raise_on({"rolled_back", "exit"}, RuntimeError))
    try:
        with pytest.raises(KeyError):
            with retention_guard(a, label="orig"):
                a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'gone')")
                raise KeyError("original")
    finally:
        dbm.GUARD_EVENTS.clear()
    assert not guarded(a) and not a.in_transaction
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='gone'").fetchone()[0] == 0


# ---------------------------------------------------------------- isolation level and direct transaction-control SQL

def test_pg_open_forces_read_committed_on_every_session(pg):
    import psycopg
    c, sessions = pg
    assert sessions[0].isolation_level == psycopg.IsolationLevel.READ_COMMITTED
    with retention_guard(c):
        pass
    stmts = _sql(sessions[0])
    assert "transaction_isolation" in stmts[0], "the isolation check is the FIRST statement of the guarded transaction"
    assert any("pg_advisory_xact_lock" in x for x in stmts)


def test_pg_guard_refuses_a_transaction_that_is_not_read_committed(pg):
    c, sessions = pg
    s = sessions[0]
    s.isolation = "repeatable read"
    with pytest.raises(dbm.RetentionGuardError, match="READ COMMITTED"):
        with retention_guard(c, label="rr"):
            pytest.fail("must not enter")
    assert not any("pg_advisory_xact_lock" in x for x in _sql(s)), "no lock is taken under an unsupported isolation level"
    assert s.rollbacks == 1 and s.commits == 0 and not guarded(c) and c._hold_tx is False and c._dirty is False
    s.isolation = "read committed"
    with retention_guard(c, label="ok"):
        c.execute("INSERT INTO t(x) VALUES (?)", (1,))
    assert s.commits == 1


def test_pg_connection_lost_at_guard_entry_is_reported_and_reopens_lazily(pg):
    c, sessions = pg
    sessions[0].fail_next = True                           # the isolation check itself hits a dead session
    with pytest.raises(ConnectionLostMidOperation) as ei:
        with retention_guard(c):
            pytest.fail("must not enter")
    assert ei.value.outcome_unknown is False and c._pg is None and not guarded(c) and len(sessions) == 1
    with retention_guard(c):                               # next outer entry re-opens
        pass
    assert len(sessions) == 2


@pytest.mark.parametrize("sql", ["COMMIT", "commit", " END", "ROLLBACK", "BEGIN", "BEGIN IMMEDIATE", "START TRANSACTION", "VACUUM", "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"])
def test_sqlite_direct_transaction_control_sql_inside_a_guard_is_refused_and_poisons_the_step(pair, sql):
    a, b = pair
    with pytest.raises(GuardRolledBack):
        with retention_guard(a):
            a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 's', 'partial')")
            with pytest.raises(CommitInsideRetentionGuard):
                a.execute(sql)
            assert a.in_transaction and guarded(a)
    assert not a.in_transaction and b.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_sqlite_begin_immediate_outside_a_guard_is_still_allowed(pair):
    a, _ = pair
    a.execute("BEGIN IMMEDIATE")                           # review_loop._begin_guarded's unguarded path
    assert a.in_transaction
    a.rollback()
    assert not a.in_transaction


@pytest.mark.parametrize("sql", ["COMMIT", "ROLLBACK", "BEGIN", "PREPARE TRANSACTION 'x'"])
def test_pg_direct_transaction_control_sql_inside_a_guard_is_refused_and_poisons_the_step(pg, sql):
    c, sessions = pg
    s = sessions[0]
    with pytest.raises(GuardRolledBack):
        with retention_guard(c):
            c.execute("INSERT INTO t(x) VALUES (?)", (1,))
            with pytest.raises(CommitInsideRetentionGuard):
                c.execute(sql)
    assert s.commits == 0 and s.rollbacks == 1 and not any(x.strip().upper().startswith(sql.split()[0].upper()) for x in _sql(s) if "SAVEPOINT" not in x)


def test_pg_connection_loss_caught_inside_the_guard_body_still_fails_the_step(pg):
    """A helper catches ConnectionLostMidOperation inside the outer body and carries on: the outer exit must NOT report a
    successful commit (the session is gone; _raw_commit on a closed session must not silently succeed). Nothing persisted or
    replayed; a later, separate guard re-opens normally."""
    c, sessions = pg
    seen = []
    dbm.GUARD_EVENTS.append(lambda ev, conn, label: seen.append((ev, label)))
    try:
        with pytest.raises(GuardRolledBack):
            with retention_guard(c, label="swallow"):
                c.execute("INSERT INTO t(x) VALUES (?)", (1,))
                sessions[0].fail_next = True
                try:
                    c.execute("INSERT INTO t(x) VALUES (?)", (2,))
                except ConnectionLostMidOperation:
                    pass                                   # the helper swallows it and returns "normally"
                assert c._guard_rollback_only is True and c._pg is None
        events = [e for e, l in seen if l == "swallow"]
        assert "committed" not in events and "commit_failed" not in events, events
        assert events[-1] == "exit" and len(sessions) == 1 and sessions[0].commits == 0
        assert not guarded(c) and c._hold_tx is False and c._dirty is False
        with retention_guard(c, label="next"):            # separate guard: lazy re-open, works
            c.execute("INSERT INTO t(x) VALUES (?)", (3,))
        assert len(sessions) == 2 and sessions[1].commits == 1
        assert [e for e, l in seen if l == "next"] == ["entered", "acquired", "committed", "exit"]
    finally:
        dbm.GUARD_EVENTS.clear()


def test_assert_not_guarded_is_about_the_calling_threads_guard_not_another_threads(pair):
    """Shared connection: the worker released its capture guard; a staff thread then holds its own guard while the worker is
    about to call the model. The worker holds no transaction or lock, so assert_not_guarded must pass for it; inside its own
    guard it must still reject; maybe_commit from the worker waits for the RLock and commits (never skips)."""
    a, b = pair
    staff_holding, release_staff, errors = threading.Event(), threading.Event(), []

    def staff():
        try:
            with retention_guard(a, label="staff edit"):
                a.execute("INSERT INTO audit_log(at, staff_member, action) VALUES ('t', 'staff', 'edit')")
                staff_holding.set()
                assert release_staff.wait(10)
        except BaseException as e:   # pragma: no cover
            errors.append(e)
            staff_holding.set()

    with retention_guard(a, label="capture"):              # worker's own guard: same thread -> rejected
        with pytest.raises(dbm.RetentionGuardError):
            dbm.assert_not_guarded(a, "model call")
        assert guarded(a) and dbm.guard_active(a)
    t = threading.Thread(target=staff, daemon=True)
    t.start()
    assert staff_holding.wait(10) and not errors
    assert dbm.guard_active(a) and not guarded(a), "another thread's guard is not this thread's"
    dbm.assert_not_guarded(a, "model call")                 # must NOT raise: the worker holds nothing
    done = []

    def worker_commit():
        maybe_commit(a)                                      # blocks on the RLock until the staff guard exits, then commits
        done.append(True)
    tw = threading.Thread(target=worker_commit, daemon=True)
    tw.start()
    assert not threading.Event().wait(0.2) and tw.is_alive(), "the worker's commit waits for the staff guard (bounded negative check)"
    release_staff.set()
    t.join(10); tw.join(10)
    assert not t.is_alive() and not tw.is_alive() and done == [True] and not errors
    assert b.execute("SELECT COUNT(*) FROM audit_log WHERE action='edit'").fetchone()[0] == 1
    assert a._guard_owner is None and not dbm.guard_active(a)


def test_assert_not_guarded(pair):
    a, _ = pair
    dbm.assert_not_guarded(a, "model call")
    with retention_guard(a):
        with pytest.raises(dbm.RetentionGuardError):
            dbm.assert_not_guarded(a, "model call")


def test_env_without_database_url_is_sqlite():
    assert not os.environ.get("DATABASE_URL") or dbm.is_postgres()
