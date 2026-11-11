"""Shared-connection concurrency (SQLite). Root cause of the PytestUnhandledThreadExceptionWarning seen once in
test_concurrent_identical_submissions_make_one_record (`_homebot_replay → homebot.get → summary`, `weekly_max` None):
Connection.execute() held the connection lock only while the statement was prepared and its first row stepped; the rows
fetched afterwards (fetchall / iteration) were stepped WITHOUT the lock, concurrently with other threads' statements and
commits on the same connection.

Two kinds of check, kept distinct:
  * deterministic mechanism checks (cursor type on every path; fetch waits for the connection lock) — these pin the fix;
  * a short stress regression on homebot.get under concurrent writers — evidence only (before the fix it produced ~250
    anomalies in 8 s: NULL-filled person rows, empty people lists, a known enquiry id "not found"; 0 after).
Every worker runs in a ThreadPoolExecutor and its result() is awaited, so worker exceptions fail the test directly."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from server import db as dbmod, homebot
from tests.test_homebot import body


def _sqlite_only(c):
    if getattr(c, "backend", "sqlite") != "sqlite":
        pytest.skip("LockedCursor is the SQLite shared-connection path; PgConnection serialises per statement itself")


def test_every_cursor_path_is_a_locked_cursor(db):
    _sqlite_only(db)
    assert isinstance(db.execute("SELECT 1"), dbmod.LockedCursor)
    assert isinstance(db.cursor(), dbmod.LockedCursor)
    assert isinstance(db.executemany("INSERT OR IGNORE INTO cities(name, active) VALUES (?, 1)", [("Melbourne",)]), dbmod.LockedCursor)
    cur = db.cursor().execute("SELECT name FROM cities WHERE name='Melbourne'")
    assert isinstance(cur, dbmod.LockedCursor) and cur.fetchone()["name"] == "Melbourne"


def test_fetch_and_iteration_wait_for_the_connection_lock(db):
    """Deterministic: each fetch style gets its OWN already-executed cursor; while another thread holds the connection lock,
    fetchone / fetchmany / fetchall / next() all block (a barrier shows every worker reached its fetch call), and all complete
    with the right rows once the lock is released."""
    _sqlite_only(db)
    db.executemany("INSERT OR IGNORE INTO cities(name, active) VALUES (?, 0)", [("Alpha",), ("Beta",), ("Gamma",)])
    db.commit()
    sql = "SELECT name FROM cities WHERE active=0 ORDER BY name"
    kinds = ("one", "many", "all", "next")
    cursors = {k: db.execute(sql) for k in kinds}  # executed (first row stepped) BEFORE the lock is taken by the holder
    held, release = threading.Event(), threading.Event()
    at_fetch = threading.Barrier(len(kinds) + 1, timeout=5)
    done_at = {}

    def holder():
        with db._mmai_lock:
            held.set()
            release.wait(5)

    def fetcher(kind):
        cur = cursors[kind]
        at_fetch.wait()  # all four workers are about to call into the cursor
        if kind == "one":
            out = cur.fetchone()["name"]
        elif kind == "many":
            out = [r["name"] for r in cur.fetchmany(2)]
        elif kind == "all":
            out = [r["name"] for r in cur.fetchall()]
        else:
            out = next(cur)["name"]
        done_at[kind] = time.monotonic()
        return out

    with ThreadPoolExecutor(max_workers=len(kinds) + 1) as ex:
        h = ex.submit(holder)
        assert held.wait(5)
        fs = {k: ex.submit(fetcher, k) for k in kinds}
        at_fetch.wait()                       # every worker has reached its fetch; they can only be inside the lock wait now
        time.sleep(0.2)
        assert not done_at, f"fetch completed while another thread held the connection lock: {done_at}"
        t_release = time.monotonic()
        release.set()
        results = {k: f.result(timeout=5) for k, f in fs.items()}
        h.result(timeout=5)
    assert results == {"one": "Alpha", "many": ["Alpha", "Beta"], "all": ["Alpha", "Beta", "Gamma"], "next": "Alpha"}
    assert set(done_at) == set(kinds) and all(t >= t_release for t in done_at.values())


def test_concurrent_reads_of_a_homebot_detail_stay_intact(db):
    """Stress regression (evidence, not a mechanism proof): readers of one saved HomeBot detail under concurrent writers on the
    same connection never see NULL `weekly_max`, a wrong people count or a missing enquiry. Worker exceptions propagate."""
    _sqlite_only(db)
    eid = homebot.create(db, body(), is_test=False, submission_key="race-detail-key-0001")
    stop = time.monotonic() + 2.0

    def reader():
        bad, n = [], 0
        while time.monotonic() < stop:
            n += 1
            e = homebot.get(db, eid)  # KeyError "not found" was one of the observed anomalies: let it propagate
            if len(e["people"]) != 3 or any(p["weekly_max"] is None for p in e["people"]):
                bad.append(e["people"])
        return n, bad

    def writer(wid):
        n = 0
        while time.monotonic() < stop:
            n += 1
            homebot.create(db, body(email=f"w{wid}-{n}@example.com"), is_test=True, submission_key=f"race-w{wid}-{n:012d}")
            db.execute("UPDATE homebot_enquiries SET updated_at=? WHERE id=?", (dbmod.now_iso(), eid))
            db.commit()
        return n

    with ThreadPoolExecutor(max_workers=6) as ex:
        readers = [ex.submit(reader) for _ in range(4)]
        writers = [ex.submit(writer, i) for i in range(2)]
        read_results = [f.result(timeout=30) for f in readers]
        writes = [f.result(timeout=30) for f in writers]
    assert sum(n for n, _ in read_results) > 0 and sum(writes) > 0, "the stress did not actually run"
    assert all(not bad for _, bad in read_results), [bad[:2] for _, bad in read_results if bad]
    assert db.execute("SELECT COUNT(*) FROM homebot_people WHERE enquiry_id=?", (eid,)).fetchone()[0] == 3
