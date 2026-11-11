"""T5 retention guard across TWO real connections (8 Nov 2026): a writer of generated text about a person and a deleter of
that person on separate connections, each inside db.retention_guard. What is proven here is DB causality, not event order:
  * writer first: the deleter's guard entry waits (observer: pg_locks on PostgreSQL / no 'acquired' while the writer holds on
    SQLite), then SEES the writer's committed row and scrubs it - nothing about the person survives, including allocation
    names;
  * deleter first: the writer's identity check inside its guard sees the person gone and writes nothing;
  * PostgreSQL only: lock timeout -> RetentionLockTimeout with nothing held; a backend terminated inside a guard ->
    ConnectionLostMidOperation, nothing persisted, lazy re-open before the next outer guard.
Barriers and bounded deadlines, never bare sleeps. These are explicit-guard MECHANISM tests on real connections; the
integrated writer/deleter entry points (ai_queue, review_loop, staff delete) are covered separately once migrated.
Runs on SQLite everywhere; the PostgreSQL parts need MMAI_TEST_DATABASE_URL (disposable cluster only)."""
import json
import os
import threading
import time

import pytest

from server import db as dbm, enquiries
from server.db import RETENTION_LOCK_KEY, ConnectionLostMidOperation, RetentionGuardError, RetentionLockTimeout, retention_guard, row
from tests.test_data_and_api import appdb, client, good_answers  # noqa: F401  (fixtures re-used)
from tests.test_intro_profile import _plant_group, hdr

PG_URL = os.environ.get("MMAI_TEST_DATABASE_URL", "")
DEADLINE = 20.0          # upper bound for every wait; a hang is a failure, not a stall
NAME = "Wanda Waitress"  # unique needle for the no-resurrection scan


def _wait(event: threading.Event, what: str):
    assert event.wait(DEADLINE), f"timed out waiting for {what}"


def _submit(client):
    r = client.post("/api/public/enquiries", json=good_answers(first_name="Wanda", last_name="Waitress", email="wanda@example.com"), headers=hdr())
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _second():
    """A second app connection to the SAME database (SQLite: same file; PostgreSQL: a second PgConnection)."""
    return dbm.connect(os.environ["MMAI_DB"])


def _observer_conn():
    """Third connection used only for observation (pg_locks / plain reads); never guarded, never writes."""
    return _second()


def _mentions(c, needle):
    hits = []
    for t, col in (("group_briefs", "body_json"), ("review_rounds", "rationale"), ("review_rounds", "review_json"), ("groups", "intro_text"),
                   ("groups", "ai_rationale"), ("groups", "allocation_json"), ("group_apartments", "allocation_json"), ("groups", "discount_note"),
                   ("audit_log", "detail"), ("enquiries", "first_name")):
        if c.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} LIKE ?", (f"%{needle}%",)).fetchone()[0]:
            hits.append(f"{t}.{col}")
    return hits


def _pg_waiting_on_retention_lock(obs) -> int:
    """Number of backends currently WAITING for the retention advisory lock (bigint key = classid<<32 | objid)."""
    return obs.execute("SELECT COUNT(*) FROM pg_locks WHERE locktype='advisory' AND classid=? AND objid=? AND NOT granted",
                       (RETENTION_LOCK_KEY >> 32, RETENTION_LOCK_KEY & 0xFFFFFFFF)).fetchone()[0]


def _prove_waiting(obs, backend, acquired_event: threading.Event):
    """Observer proof that the second guard is blocked while the first holds the DB lock."""
    if backend == "postgres":
        deadline = time.monotonic() + DEADLINE
        while _pg_waiting_on_retention_lock(obs) < 1:
            assert time.monotonic() < deadline, "the second connection never showed up waiting for the advisory lock"
            time.sleep(0.02)
        assert not acquired_event.is_set()
    else:
        # SQLite has no lock table: the bounded negative check - the second guard has not acquired while the first holds
        assert not acquired_event.wait(0.5), "the second guard acquired while the first still held BEGIN IMMEDIATE"


def _plant_allocation(c, gid, eid):
    alloc = {"rooms": [{"room": 1, "setup": "1 king or 2 singles", "room_cost": 575.0,
                        "occupants": [{"person_id": str(eid), "name": NAME, "bed": "king", "share": 575.0}]}]}
    c.execute("UPDATE groups SET allocation_json=? WHERE id=?", (json.dumps(alloc), gid))
    c.commit()


@pytest.fixture
def two(client):
    a = appdb(client)
    b = _second()
    obs = _observer_conn()
    eid = _submit(client)
    gid = _plant_group(a, eid)
    _plant_allocation(a, gid, eid)
    acquired = {}
    hook = lambda ev, conn, label: acquired.setdefault(label, threading.Event()).set() if ev == "acquired" else None
    dbm.GUARD_EVENTS.append(hook)
    yield a, b, obs, eid, gid, acquired
    dbm.GUARD_EVENTS.clear()
    b.close()
    obs.close()


def test_writer_first_the_deleter_waits_then_sees_and_scrubs_the_committed_row(two):
    a, b, obs, eid, gid, acquired = two
    acquired["deleter"] = threading.Event()
    w_holding, release_w, errors = threading.Event(), threading.Event(), []
    seen_at_entry = []

    def writer():
        try:
            with retention_guard(a, label="writer"):
                assert row(a.execute("SELECT id FROM enquiries WHERE id=?", (eid,))) is not None      # identity check
                a.execute("INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (gid, 0, "approve", 8.0, f"{NAME} keeps a tidy kitchen", json.dumps({"findings": [NAME]}), 1, "h", dbm.now_iso()))
                w_holding.set()
                _wait(release_w, "release of the writer")
        except BaseException as e:   # pragma: no cover - surfaced by the assertion below
            errors.append(e)
            w_holding.set()

    def deleter():
        try:
            with retention_guard(b, label="deleter"):
                seen_at_entry.append(b.execute("SELECT COUNT(*) FROM review_rounds WHERE rationale LIKE ?", (f"%{NAME}%",)).fetchone()[0])
                enquiries.forget_generated_text(b, [eid], include_leased=True)
                b.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
                b.execute("DELETE FROM enquiries WHERE id=?", (eid,))
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    _wait(w_holding, "the writer to hold the guard")
    assert not errors, errors
    td = threading.Thread(target=deleter, daemon=True)
    td.start()
    _prove_waiting(obs, getattr(a, "backend", "sqlite"), acquired["deleter"])
    assert obs.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (gid,)).fetchone()[0] == 0, "nothing visible before the writer commits"
    release_w.set()
    tw.join(DEADLINE)
    td.join(DEADLINE)
    assert not tw.is_alive() and not td.is_alive(), "a guarded step did not finish in time"
    assert not errors, errors
    # DB causality: the deleter entered AFTER the writer's commit (it saw the row), then removed it
    assert seen_at_entry == [1], seen_at_entry
    assert row(obs.execute("SELECT id FROM enquiries WHERE id=?", (eid,))) is None
    assert _mentions(obs, NAME) == [], _mentions(obs, NAME)
    alloc = json.loads(row(obs.execute("SELECT allocation_json FROM groups WHERE id=?", (gid,)))["allocation_json"])
    assert alloc["rooms"][0]["occupants"][0]["name"] == "Former member" and alloc["rooms"][0]["occupants"][0]["share"] == 575.0
    assert not dbm.guarded(a) and not dbm.guarded(b)


def test_deleter_first_the_writer_identity_check_sees_the_person_gone_and_writes_nothing(two):
    a, b, obs, eid, gid, acquired = two
    acquired["writer"] = threading.Event()
    d_holding, release_d, errors, outcome = threading.Event(), threading.Event(), [], []

    def deleter():
        try:
            with retention_guard(b, label="deleter"):
                enquiries.forget_generated_text(b, [eid], include_leased=True)
                b.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
                b.execute("DELETE FROM enquiries WHERE id=?", (eid,))
                d_holding.set()
                _wait(release_d, "release of the deleter")
        except BaseException as e:   # pragma: no cover
            errors.append(e)
            d_holding.set()

    def writer():
        try:
            with retention_guard(a, label="writer"):
                if row(a.execute("SELECT id FROM enquiries WHERE id=?", (eid,))) is None:
                    outcome.append("gone")
                    return                               # nothing written; the guard commits an empty step
                a.execute("INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                          (gid, 0, "approve", 8.0, f"{NAME} resurrected", "{}", 1, "h", dbm.now_iso()))
                outcome.append("written")
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    td = threading.Thread(target=deleter, daemon=True)
    td.start()
    _wait(d_holding, "the deleter to hold the guard")
    assert not errors, errors
    assert row(obs.execute("SELECT id FROM enquiries WHERE id=?", (eid,))) is not None, "the deletion is not visible before commit"
    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    _prove_waiting(obs, getattr(a, "backend", "sqlite"), acquired["writer"])
    release_d.set()
    td.join(DEADLINE)
    tw.join(DEADLINE)
    assert not td.is_alive() and not tw.is_alive()
    assert not errors, errors
    assert outcome == ["gone"]
    assert obs.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (gid,)).fetchone()[0] == 0
    assert _mentions(obs, NAME) == []


# ---------------------------------------------------------------- PostgreSQL only

pg_only = pytest.mark.skipif(not PG_URL, reason="set MMAI_TEST_DATABASE_URL=postgresql://... (disposable cluster) to run")


@pg_only
def test_pg_lock_timeout_is_reported_and_the_second_connection_holds_nothing(two, monkeypatch):
    a, b, obs, eid, gid, acquired = two
    assert a.backend == "postgres"
    monkeypatch.setattr(dbm, "RETENTION_LOCK_TIMEOUT_MS", 300)
    w_holding, release_w = threading.Event(), threading.Event()

    def writer():
        with retention_guard(a, label="writer"):
            w_holding.set()
            _wait(release_w, "release of the writer")

    tw = threading.Thread(target=writer, daemon=True)
    tw.start()
    _wait(w_holding, "the writer to hold the guard")
    t0 = time.monotonic()
    with pytest.raises(RetentionLockTimeout):
        with retention_guard(b, label="late"):
            pytest.fail("must not enter")
    assert time.monotonic() - t0 < DEADLINE
    assert not dbm.guarded(b) and b._dirty is False and b._hold_tx is False
    assert b._pg.info.transaction_status == 0, "the failed acquisition left no open transaction (IDLE)"   # psycopg TransactionStatus.IDLE
    assert _pg_waiting_on_retention_lock(obs) == 0
    release_w.set()
    tw.join(DEADLINE)
    assert not tw.is_alive()
    with retention_guard(b, label="after"):               # the connection is fully usable afterwards
        b.execute("UPDATE groups SET updated_at=? WHERE id=?", (dbm.now_iso(), gid))


@pg_only
def test_pg_backend_terminated_inside_a_guard_fails_closed_and_reopens_only_for_the_next_outer_guard(two):
    a, b, obs, eid, gid, acquired = two
    pid = a.execute("SELECT pg_backend_pid()").fetchone()[0]
    with pytest.raises(ConnectionLostMidOperation) as ei:
        with retention_guard(a, label="writer"):
            a.execute("INSERT INTO review_rounds(group_id, round, verdict, rating, rationale, review_json, valid, evidence_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (gid, 0, "approve", 8.0, f"{NAME} half written", "{}", 1, "h", dbm.now_iso()))
            assert obs.execute("SELECT pg_terminate_backend(?)", (pid,)).fetchone()[0] is True
            # the next statement on the terminated session fails (57P01 admin shutdown or a closed socket): fail closed, no replay
            a.execute("SELECT 1")
    assert ei.value.outcome_unknown is False
    assert a._pg is None and not dbm.guarded(a) and a._hold_tx is False and a._dirty is False
    assert obs.execute("SELECT COUNT(*) FROM review_rounds WHERE group_id=?", (gid,)).fetchone()[0] == 0, "the half-written step died with the session"
    assert _pg_waiting_on_retention_lock(obs) == 0
    with retention_guard(a, label="next"):                 # lazy re-open happens at the next OUTER guard entry
        assert a._pg is not None and a.execute("SELECT pg_backend_pid()").fetchone()[0] != pid
        a.execute("UPDATE groups SET updated_at=? WHERE id=?", (dbm.now_iso(), gid))
    assert a._dirty is False and row(obs.execute("SELECT updated_at FROM groups WHERE id=?", (gid,))) is not None


@pg_only
def test_pg_dirty_outer_entry_is_refused_in_the_real_adapter_and_the_pending_work_is_left_alone(two):
    a, b, obs, eid, gid, acquired = two
    b.execute("UPDATE groups SET discount_note='pending' WHERE id=?", (gid,))   # uncommitted write on b
    with pytest.raises(dbm.DirtyConnectionAtGuardEntry):
        with retention_guard(b, label="x"):
            pytest.fail("must not enter")
    assert b._dirty is True, "the guard neither committed nor rolled back work it does not own"
    assert row(obs.execute("SELECT discount_note FROM groups WHERE id=?", (gid,)))["discount_note"] is None
    b.commit()
    assert row(obs.execute("SELECT discount_note FROM groups WHERE id=?", (gid,)))["discount_note"] == "pending"


# ---------------------------------------------------------------- isolation: non-default REPEATABLE READ input

# libpq `options`: spaces inside a value are escaped with a backslash (%5C), the separator space is %20
RR_URL = PG_URL + ("&" if "?" in PG_URL else "?") + "options=-c%20default_transaction_isolation%3Drepeatable%5C%20read"


def _delete_person(c, eid):
    enquiries.forget_generated_text(c, [eid], include_leased=True)
    c.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
    c.execute("DELETE FROM enquiries WHERE id=?", (eid,))


@pg_only
def test_pg_repeatable_read_default_is_a_real_input_and_the_adapter_still_begins_read_committed():
    import psycopg
    with psycopg.connect(RR_URL, autocommit=False) as raw:
        assert raw.execute("SHOW default_transaction_isolation").fetchone()[0] == "repeatable read"
        assert raw.execute("SELECT current_setting('transaction_isolation')").fetchone()[0] == "repeatable read", "an unforced session really runs REPEATABLE READ"
    c = dbm.PgConnection(RR_URL)
    try:
        with retention_guard(c, label="probe"):
            assert c.execute("SELECT current_setting('transaction_isolation')").fetchone()[0] == "read committed"
    finally:
        c.close()


@pg_only
def test_pg_writer_with_repeatable_read_default_sees_the_deletion_committed_while_it_waited(two):
    """Deleter (a) holds the lock with the person deleted but uncommitted; the writer (b, session default REPEATABLE READ)
    enters its guard, waits, and its post-lock identity check must see the person GONE (current state), not a pre-wait snapshot."""
    a, _b, obs, eid, gid, acquired = two
    b = dbm.PgConnection(RR_URL)
    acquired["writer"] = threading.Event()
    d_holding, release_d, errors, outcome = threading.Event(), threading.Event(), [], []
    try:
        def deleter():
            try:
                with retention_guard(a, label="deleter"):
                    _delete_person(a, eid)
                    d_holding.set()
                    _wait(release_d, "release of the deleter")
            except BaseException as e:   # pragma: no cover
                errors.append(e)
                d_holding.set()

        def writer():
            try:
                with retention_guard(b, label="writer"):
                    outcome.append(b.execute("SELECT current_setting('transaction_isolation')").fetchone()[0])
                    outcome.append("gone" if row(b.execute("SELECT id FROM enquiries WHERE id=?", (eid,))) is None else "present")
            except BaseException as e:   # pragma: no cover
                errors.append(e)

        td = threading.Thread(target=deleter, daemon=True); td.start()
        _wait(d_holding, "the deleter to hold the guard")
        assert not errors, errors
        tw = threading.Thread(target=writer, daemon=True); tw.start()
        _prove_waiting(obs, "postgres", acquired["writer"])
        release_d.set()
        td.join(DEADLINE); tw.join(DEADLINE)
        assert not td.is_alive() and not tw.is_alive()
        assert not errors, errors
        assert outcome == ["read committed", "gone"], outcome
    finally:
        release_d.set()
        b.close()


@pg_only
def test_pg_guard_refuses_when_the_session_isolation_cannot_be_forced(two):
    """A misconfigured adapter (isolation not forced) on a REPEATABLE READ default: the guard refuses at entry, holds nothing."""
    a, _b, obs, eid, gid, acquired = two
    b = dbm.PgConnection(RR_URL)
    try:
        b._pg.isolation_level = None                      # simulate the forcing being lost
        with pytest.raises(RetentionGuardError, match="READ COMMITTED"):
            with retention_guard(b, label="rr"):
                pytest.fail("must not enter")
        assert not dbm.guarded(b) and b._hold_tx is False and b._dirty is False
        assert b._pg.info.transaction_status == 0, "nothing left open"
        assert _pg_waiting_on_retention_lock(obs) == 0
        assert obs.execute("SELECT COUNT(*) FROM pg_locks WHERE locktype='advisory' AND classid=? AND objid=?",
                           (RETENTION_LOCK_KEY >> 32, RETENTION_LOCK_KEY & 0xFFFFFFFF)).fetchone()[0] == 0, "no advisory lock held"
    finally:
        b.close()


# ---------------------------------------------------------------- D (8 Nov 2026): one apartment, two staff, two connections
def test_two_connections_cannot_both_lease_the_same_apartment(two, client):
    """The REAL claim helper (app._set_group_status) on two connections for two groups that both want the same apartment. The first
    holds its "group status" guard (claim written, not committed); the second waits for the DB lock (observer proof), then its
    claim `UPDATE ... WHERE leased_at IS NULL` matches nothing and it is refused with 409: one leased_group_id, the other group
    untouched (status, members), no second claim audit. Causality is proven from the DB (the second saw the first's committed
    claim), not from event order."""
    from fastapi import HTTPException
    from server import app as application
    a, b, obs, eid, gid, acquired = two
    assert application.db is a
    aid = row(a.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))["apartment_id"]
    r = client.post("/api/public/enquiries", json=good_answers(first_name="Second", last_name="Claimant", email="second-claimant@example.com"), headers=hdr())
    assert r.status_code == 201, r.text
    eid2 = r.json()["id"]
    a.execute("INSERT INTO groups(apartment_id, status, member_key, created_at, updated_at) VALUES (?, 'Proposed', ?, ?, ?)", (aid, str(eid2), dbm.now_iso(), dbm.now_iso()))
    gid2 = row(a.execute("SELECT id FROM groups WHERE member_key=?", (str(eid2),)))["id"]
    a.execute("INSERT INTO group_members(group_id, enquiry_id, weekly_share) VALUES (?, ?, 575.0)", (gid2, eid2))
    a.commit()

    first_holding, release_first, second_acquired = threading.Event(), threading.Event(), threading.Event()
    held = []

    def hook(ev, conn, label):
        if ev != "acquired" or label != "group status":
            return
        if conn is a and not held:          # the first claimant: hold the guard (claim written below, uncommitted) until released
            held.append(True)
            first_holding.set()
            _wait(release_first, "release of the first claimant")
        elif conn is b:
            second_acquired.set()
    dbm.GUARD_EVENTS.append(hook)
    outcome, errors = {}, []

    def first():
        try:
            outcome["first"] = application._set_group_status(gid, "Leased", "anna", conn=a)
        except BaseException as e:   # pragma: no cover - surfaced below
            errors.append(("first", e))
            first_holding.set()

    def second():
        try:
            outcome["second"] = application._set_group_status(gid2, "Leased", "bill", conn=b)
        except HTTPException as e:
            outcome["second_refusal"] = e
        except BaseException as e:   # pragma: no cover
            errors.append(("second", e))

    # the hook fires at acquisition, BEFORE the first claimant's reads and writes; so hold it there, and let the observer check
    # that nothing is visible yet while the second is provably waiting
    t1 = threading.Thread(target=first, daemon=True)
    t1.start()
    _wait(first_holding, "the first claimant to hold the guard")
    assert not errors, errors
    t2 = threading.Thread(target=second, daemon=True)
    t2.start()
    _prove_waiting(obs, getattr(a, "backend", "sqlite"), second_acquired)
    assert row(obs.execute("SELECT leased_at FROM apartments WHERE id=?", (aid,)))["leased_at"] is None, "nothing visible before the first commits"
    release_first.set()
    t1.join(DEADLINE)
    t2.join(DEADLINE)
    assert not t1.is_alive() and not t2.is_alive(), "a claim did not finish in time"
    assert not errors, errors
    assert second_acquired.is_set()
    rep = outcome["first"]
    assert rep and rep["leased_group_id"] == gid
    refusal = outcome.get("second_refusal")
    # Stock lifecycle (9 Nov 2026): BEFORE, the second claimant always still pointed at the apartment and its CAS `UPDATE ... WHERE
    # leased_at IS NULL` matched nothing ("already marked Leased"). AFTER, the first lease's COMMITTED follow-up (one guard step per
    # other group) may acquire the lock before the second claimant does and take the leased apartment off that group, which is then
    # held with no current offer; its lease is refused in the guard as "no apartment to lease". A third interleaving (seen on
    # PostgreSQL in the frozen-source run): the first lease's transaction 1 has committed its pending work row for the second group
    # but the follow-up step has not run yet when the second claimant enters its guard - the explicit pending-stock-work gate refuses
    # the lease ("still being re-checked after a stock change"). All three are a 409 refusal from the re-read inside the guard; the
    # invariant proven below is unchanged: one claim, the other group's status / members untouched.
    assert refusal is not None and refusal.status_code == 409 and ("already marked Leased" in refusal.detail or "no apartment to lease" in refusal.detail
                                                                   or "re-checked after a stock change" in refusal.detail), outcome
    apt = row(obs.execute("SELECT leased_at, leased_group_id, leased_by FROM apartments WHERE id=?", (aid,)))
    assert apt["leased_group_id"] == gid and apt["leased_by"] == "anna" and apt["leased_at"] == rep["leased_at"]
    assert row(obs.execute("SELECT status FROM groups WHERE id=?", (gid,)))["status"] == "Leased"
    assert row(obs.execute("SELECT status FROM groups WHERE id=?", (gid2,)))["status"] == "Proposed", "the refused group is untouched"
    assert row(obs.execute("SELECT status FROM enquiries WHERE id=?", (eid2,)))["status"] == "New"
    assert row(obs.execute("SELECT status FROM enquiries WHERE id=?", (eid,)))["status"] == "Leased"
    claims = [r["detail"] for r in dbm.rows(obs.execute("SELECT detail FROM audit_log WHERE action='group_status' AND detail LIKE '%claimed apartment%'"))]
    assert len(claims) == 1 and f"claimed apartment {aid}" in claims[0], claims
    assert obs.execute("SELECT COUNT(*) FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(aid),)).fetchone()[0] == 1
    assert not dbm.guarded(a) and not dbm.guarded(b)
