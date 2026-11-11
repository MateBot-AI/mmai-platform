"""G-SL2 §7 (11 Nov 2026): the staff test-data clear is ONE retention-guard step that captures, counts, refuses before any write when
a test flag contradicts ownership (mixed REAL/TEST group, REAL-flagged link/request/key/alert attached to a TEST record or the
reverse), deletes TEST rows only through the one group-deletion helper, tombstones exactly the captured keys of both bots, and
writes one audit row with actor and counts in the same transaction. REAL rows - including REAL-flagged orphans whose enquiry is
gone - are never deleted or tombstoned; they are counted. Not a launch-readiness claim."""
from __future__ import annotations

import hashlib

import pytest

from server import db as dbm
from server import homebot, matching, seed, submissions
from server import stock_lifecycle as sl
from server.db import RetentionGuardError, RetentionLockTimeout, now_iso, retention_guard, row, rows
from server.enquiries import create_enquiry
from tests.test_data_and_api import good_answers
from tests.test_homebot import K, body, client, post  # noqa: F401
from tests.test_launch_neutral import _apt, _enq

STAFF = "Anna Chan"
TABLES = ("enquiries", "groups", "group_members", "group_apartments", "offer_reconcile_work", "stock_transitions", "mate_links",
          "mate_requests", "form_drafts", "enquiry_submissions", "homebot_enquiries", "homebot_people", "homebot_email_outbox", "internal_alert_outbox", "audit_log")


def _snapshot(db, tables=TABLES):
    out = {}
    for t in tables:
        cols = sorted(dbm.table_columns(db, t))
        out[t] = [tuple(r[c] for c in cols) for r in rows(db.execute(f"SELECT * FROM {t} ORDER BY 1"))]
    return out


def _digest(db, table, where="1=1", args=()):
    cols = sorted(dbm.table_columns(db, table))
    data = [tuple(str(r[c]) for c in cols) for r in rows(db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY 1", args))]
    return hashlib.sha256(repr(data).encode()).hexdigest(), len(data)


def _real_enq(db, name, email):
    return create_enquiry(db, good_answers(first_name=name, email=email), "Phone", staff=STAFF, use_ai_countries=False, is_test=False)["id"]


def _key(db, name, eid, *, is_test, homebot_scope=False):
    scope = submissions.HOMEBOT_SCOPE if homebot_scope else "enquiry"
    sub = submissions.reserve(db, K(name), scope, "fp", is_test)
    submissions.mark_saved(db, sub, eid)
    submissions.finish(db, K(name), eid, {"id": eid})
    return K(name)


def _link(db, token, eid, is_test):
    db.execute("INSERT INTO mate_links(token, enquiry_id, created_at, expires_at, is_test) VALUES (?,?,?,?,?)", (token, eid, now_iso(), "2099-01-01", is_test))
    db.commit()


def _request(db, gid, root, friend, is_test):
    db.execute("INSERT INTO mate_requests(group_id, root_enquiry_id, friend_enquiry_id, created_at, is_test) VALUES (?,?,?,?,?)", (gid, root, friend, now_iso(), is_test))
    db.commit()


def _alert(db, record_type, rid, is_test):
    db.execute("""INSERT INTO internal_alert_outbox(event_id, kind, record_type, record_id, to_email, is_test, status, created_at, updated_at)
                  VALUES (?,?,?,?,?,?, 'held', ?, ?)""", (f"gsl2:{record_type}:{rid}:{is_test}:{now_iso()}", "test_kind", record_type, rid, "ops@example.invalid", is_test, now_iso(), now_iso()))
    db.commit()


def _hb(c, name, is_test):
    r = post(c, body(is_test=is_test, email=f"{name}@example.com"), name)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _groups_of(db, eid):
    return [r["group_id"] for r in rows(db.execute("SELECT group_id FROM group_members WHERE enquiry_id=? ORDER BY group_id", (eid,)))]


def _audit(db, action):
    return rows(db.execute("SELECT staff_member, action, entity, entity_id, detail FROM audit_log WHERE action=? ORDER BY id", (action,)))


@pytest.fixture
def world(client):
    """REAL and TEST data side by side, built through product paths: one REAL group out of a real-dataset run, one TEST group out of a
    test-dataset run (both listing the same apartment), a pending stock work row for each on ONE shared transition (key captured),
    keys for both bots in both flags, a TEST HomeBot enquiry and a REAL one, REAL orphan rows whose enquiries are gone."""
    db = client.db
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    db.execute("UPDATE apartments SET available_from=NULL WHERE id<>?", (a["id"],)); db.commit()
    real = [_real_enq(db, f"Real{i}", f"real{i}@example.invalid") for i in range(2)]
    test = [_enq(db, f"Test{i}", f"test{i}@example.invalid")["id"] for i in range(2)]
    matching.full_run(db, "tester", dataset="real", use_ai=False)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    rg = _groups_of(db, real[0]); tg = _groups_of(db, test[0])
    assert rg and tg and set(rg).isdisjoint(tg), (rg, tg)
    with retention_guard(db, label="t"):
        tr = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    tid = tr["transition_id"]
    assert set(tr["group_ids"]) >= set(rg) | set(tg) and row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (tid,)))["building_key"]
    hb_test = _hb(client, "hb-test-0001", True)
    hb_real = _hb(client, "hb-real-0001", False)
    _key(db, "mb-test-key-1", test[0], is_test=True)
    _key(db, "mb-real-key-1", real[0], is_test=False)
    _link(db, "tok-test", test[1], 1); _link(db, "tok-real", real[1], 0)
    db.execute("INSERT INTO form_drafts(token, answers_json, step, email, is_test, created_at, updated_at, expires_at) VALUES ('draft-test', '{}', 1, 't@example.invalid', 1, ?, ?, '2099-01-01')", (now_iso(), now_iso()))
    db.execute("INSERT INTO form_drafts(token, answers_json, step, email, is_test, created_at, updated_at, expires_at) VALUES ('draft-real', '{}', 1, 'r@example.invalid', 0, ?, ?, '2099-01-01')", (now_iso(), now_iso()))
    db.commit()
    # REAL orphans: rows whose REAL enquiry is gone (deleted through the staff route), in both scopes
    gone = _real_enq(db, "Gone", "gone@example.invalid")
    _key(db, "mb-real-orphan-1", gone, is_test=False)
    _link(db, "tok-orphan", gone, 0)
    _request(db, rg[0], gone, real[0], 0)
    db.execute("UPDATE enquiry_submissions SET enquiry_id=? WHERE submit_key=?", (gone, K("mb-real-orphan-1"))); db.commit()
    db.execute("DELETE FROM enquiries WHERE id=?", (gone,)); db.commit()                 # the link cascades; the key, request stay as orphans
    db.execute("UPDATE enquiry_submissions SET status='done' WHERE submit_key=?", (K("mb-real-orphan-1"),))
    db.execute("INSERT INTO enquiry_submissions(submit_key, scope, fingerprint, status, enquiry_id, is_test, attempts, created_at, updated_at) VALUES (?,?,?,'done',?,0,1,?,?)",
               (K("hb-real-orphan-1"), submissions.HOMEBOT_SCOPE, "fp", 999999, now_iso(), now_iso()))
    _alert(db, "homebot_enquiry", 999999, 0)
    db.commit()
    return dict(db=db, client=client, a=a, real=real, test=test, rg=rg, tg=tg, tid=tid, hb_test=hb_test, hb_real=hb_real)


def _real_digests(db, w):
    real_ids = tuple(w["real"])
    return {
        "enquiries": _digest(db, "enquiries", "is_test=0"),
        "groups": _digest(db, "groups", "id IN (%s)" % ",".join("?" * len(w["rg"])), tuple(w["rg"])),
        "group_members": _digest(db, "group_members", "group_id IN (%s)" % ",".join("?" * len(w["rg"])), tuple(w["rg"])),
        "work": _digest(db, "offer_reconcile_work", "group_id IN (%s)" % ",".join("?" * len(w["rg"])), tuple(w["rg"])),
        "links": _digest(db, "mate_links", "is_test=0"),
        "requests": _digest(db, "mate_requests", "is_test=0"),
        "keys": _digest(db, "enquiry_submissions", "is_test=0"),
        "alerts": _digest(db, "internal_alert_outbox", "is_test=0"),
        "hb": _digest(db, "homebot_enquiries", "is_test=0"),
        "hb_receipts": _digest(db, "homebot_email_outbox", "enquiry_id IN (SELECT id FROM homebot_enquiries WHERE is_test=0)"),
        "drafts": _digest(db, "form_drafts", "is_test=0"),
    }


# ---------------------------------------------------------------------------------------------------------- T1 / T2 / U1 / U2 / U6

def test_clear_removes_test_rows_only_tombstones_exactly_the_captured_keys_and_audits_once_in_the_same_step(world):
    db, w = world["db"], world
    before = _real_digests(db, w)
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (w["tid"],)))["building_key"]
    n_audit = db.execute("SELECT COUNT(*) FROM audit_log WHERE action='delete_test_data'").fetchone()[0]
    n_tg = db.execute("""SELECT COUNT(*) FROM groups g
                         WHERE EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=1)
                           AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test<>1)""").fetchone()[0]
    assert n_tg >= len(w["tg"]) >= 1
    n = seed.delete_test_data(db, STAFF)
    assert n == 2
    assert not db.in_transaction
    assert _real_digests(db, w) == before                                              # T1 / U1: every REAL row byte-identical, orphans included
    assert rows(db.execute("SELECT id FROM enquiries WHERE is_test=1")) == [] and rows(db.execute("SELECT id FROM homebot_enquiries WHERE is_test=1")) == []
    assert all(row(db.execute("SELECT 1 FROM groups WHERE id=?", (g,))) is None for g in w["tg"])
    assert rows(db.execute("SELECT token FROM mate_links WHERE is_test=1")) == [] and rows(db.execute("SELECT token FROM form_drafts WHERE is_test=1")) == []
    # U2: tombstones for exactly the captured TEST keys of each scope; the REAL orphan keys are untouched
    assert row(db.execute("SELECT status, enquiry_id, response_json FROM enquiry_submissions WHERE submit_key=?", (K("mb-test-key-1"),))) == {"status": "deleted", "enquiry_id": None, "response_json": None}
    assert row(db.execute("SELECT status, enquiry_id FROM enquiry_submissions WHERE submit_key=?", (K("hb-test-0001"),))) == {"status": "deleted", "enquiry_id": None}
    assert row(db.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (K("mb-real-orphan-1"),)))["status"] == "done"
    assert row(db.execute("SELECT status, enquiry_id FROM enquiry_submissions WHERE submit_key=?", (K("hb-real-orphan-1"),))) == {"status": "done", "enquiry_id": 999999}
    assert row(db.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (K("hb-real-0001"),)))["status"] != "deleted"
    # T2: the shared transition still has the REAL group's pending row -> key kept; it clears only when that row closes
    assert {r["group_id"] for r in rows(db.execute("SELECT group_id FROM offer_reconcile_work WHERE transition_id=? AND status='pending'", (w["tid"],)))} == set(w["rg"])
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (w["tid"],)))["building_key"]
    sl.run_pending(db, transition_id=w["tid"])
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (w["tid"],)))["building_key"] is None
    # U6: exactly one audit row, actor and counts only
    a = _audit(db, "delete_test_data")
    assert len(a) == n_audit + 1 and a[-1]["staff_member"] == STAFF
    assert a[-1]["detail"] == f"2 MateBot and 1 HomeBot test enquiries, {n_tg} groups cleared; 0 exit exceptions cleared; 4 unrelated real orphan rows left unchanged", a[-1]["detail"]
    assert "@" not in a[-1]["detail"] and K("mb-test-key-1") not in a[-1]["detail"]
    # idempotent: a second clear finds nothing and still audits counts. The baseline is re-taken AFTER the deliberate run_pending
    # above, which is the only REAL change since the first clear: exactly the REAL group's row and the group it reconciled.
    after_run = _real_digests(db, w)
    assert {k for k in before if before[k] != after_run[k]} == {"groups", "work"}, {k: (before[k], after_run[k]) for k in before if before[k] != after_run[k]}
    assert all(before[k][1] == after_run[k][1] for k in before)                        # the same row counts everywhere - nothing deleted
    assert seed.delete_test_data(db, STAFF) == 0 and _real_digests(db, w) == after_run
    a2 = _audit(db, "delete_test_data")
    assert len(a2) == len(a) + 1 and a2[-1]["detail"] == "0 MateBot and 0 HomeBot test enquiries, 0 groups cleared; 0 exit exceptions cleared; 4 unrelated real orphan rows left unchanged"


def _owners(db, aid):
    """Independent full owner census (primaries and alternatives), queried before the mutation under test."""
    ph = ",".join("?" * len(sl.LIVE_EXCLUDED))
    cur = {int(r["id"]) for r in rows(db.execute(f"SELECT id FROM groups WHERE apartment_id=? AND status NOT IN ({ph})", (aid, *sl.LIVE_EXCLUDED)))}
    alt = {int(r["id"]) for r in rows(db.execute(f"""SELECT DISTINCT g.id FROM group_apartments ga JOIN groups g ON g.id=ga.group_id
                                                     WHERE ga.apartment_id=? AND g.status NOT IN ({ph})""", (aid, *sl.LIVE_EXCLUDED)))}
    return cur | alt


def _pending_groups(db, tid):
    return {int(r["group_id"]) for r in rows(db.execute("SELECT group_id FROM offer_reconcile_work WHERE transition_id=? AND status='pending'", (tid,)))}


def _test_only_groups(db):
    return {int(r["id"]) for r in rows(db.execute("""SELECT g.id FROM groups g
                 WHERE EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=1)
                   AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test<>1)"""))}


def test_clear_that_closes_the_last_pending_rows_nulls_the_key_in_the_same_step(world):
    """Neutral M1 (T2/U4 composition through the seed path): when the TEST groups' rows are the LAST pending rows of a transition,
    the clear itself closes them with the groups and NULLs stock_transitions.building_key in its one step - no run_pending
    afterwards. Set-up: every non-TEST owner's row is completed first, one real step each (a deliberate REAL change, before the
    baseline); the owner census is queried independently before anything is mutated."""
    db, w = world["db"], world
    tid = w["tid"]
    owners = _owners(db, w["a"]["id"])
    test_groups = _test_only_groups(db)
    assert _pending_groups(db, tid) == owners and set(w["rg"]) | set(w["tg"]) <= owners
    test_owners = owners & test_groups
    assert set(w["tg"]) <= test_owners and (owners - test_groups), "the fixture needs REAL owners to close first"
    for r in rows(db.execute("SELECT id, group_id FROM offer_reconcile_work WHERE transition_id=? AND status='pending' ORDER BY id", (tid,))):
        if int(r["group_id"]) not in test_groups:
            sl.reconcile_work(db, r["id"])                                             # controlled closure of the REAL owners' rows
    assert _pending_groups(db, tid) == test_owners and test_owners                      # only TEST groups' rows remain
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (tid,)))["building_key"]   # still pending -> key kept
    before = _real_digests(db, w)
    n_closed = len(test_owners)
    assert seed.delete_test_data(db, STAFF) == 2 and not db.in_transaction
    assert _real_digests(db, w) == before                                              # REAL rows byte-identical (orphans included)
    assert _pending_groups(db, tid) == set()
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (tid,)))["building_key"] is None   # cleared BY the clear
    assert all(row(db.execute("SELECT 1 FROM groups WHERE id=?", (g,))) is None for g in test_owners)
    assert rows(db.execute("SELECT 1 FROM offer_reconcile_work w LEFT JOIN groups g ON g.id=w.group_id WHERE g.id IS NULL")) == []
    assert n_closed >= 1 and _test_only_groups(db) == set()
    # every transition carries a key iff it still has pending work
    for t in rows(db.execute("SELECT id, building_key FROM stock_transitions")):
        assert (t["building_key"] is not None) == bool(_pending_groups(db, t["id"])), t
    # idempotent: nothing left to clear, REAL digests unchanged against the post-clear baseline
    after = _real_digests(db, w)
    assert seed.delete_test_data(db, STAFF) == 0 and _real_digests(db, w) == after
    assert row(db.execute("SELECT building_key FROM stock_transitions WHERE id=?", (tid,)))["building_key"] is None


def test_return_contract_and_homebot_helper_contract(world):
    db, w = world["db"], world
    assert seed.delete_test_data(db) == 2                                               # staff optional (existing callers :318 / :92)
    assert _audit(db, "delete_test_data")[-1]["staff_member"] is None
    # the HomeBot helper returns the ids it removed and tombstones only those (no orphan sweep)
    hb2 = _hb(world["client"], "hb-test-0002", True)
    assert homebot.delete_test_data(db) == [hb2]
    db.commit()
    assert row(db.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (K("hb-test-0002"),)))["status"] == "deleted"
    assert row(db.execute("SELECT status FROM enquiry_submissions WHERE submit_key=?", (K("hb-real-orphan-1"),)))["status"] == "done"


# ------------------------------------------------------------------------------------------------------------------- T3 / T4

def test_injected_fault_after_the_deletes_rolls_everything_back_with_no_audit_row_and_a_retry_succeeds(world, monkeypatch):
    db, w = world["db"], world
    before = _snapshot(db)
    real = homebot.delete_test_data

    def failing(c):
        out = real(c)
        raise RuntimeError("injected after the deletes")
    monkeypatch.setattr(homebot, "delete_test_data", failing)
    with pytest.raises(RuntimeError, match="injected"):
        seed.delete_test_data(db, STAFF)
    assert not db.in_transaction and _snapshot(db) == before                         # full rollback, clean connection, NO audit row
    monkeypatch.setattr(homebot, "delete_test_data", real)
    assert seed.delete_test_data(db, STAFF) == 2
    assert len(_audit(db, "delete_test_data")) == 1


def test_a_second_connection_holding_the_write_lock_makes_the_clear_time_out_with_nothing_deleted(world, tmp_path):
    import sqlite3
    db, w = world["db"], world
    if getattr(db, "backend", "sqlite") != "sqlite":
        pytest.skip("SQLite lock path; the PG lock path is covered by test_retention_two_connections")
    before = _snapshot(db)
    path = row(db.execute("PRAGMA database_list"))["file"]
    other = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    busy_before = db.execute("PRAGMA busy_timeout").fetchone()[0]
    db.execute("PRAGMA busy_timeout = 300")                                              # a short wait for the test only; restored below
    try:
        with pytest.raises(RetentionLockTimeout):
            seed.delete_test_data(db, STAFF)
    finally:
        db.execute(f"PRAGMA busy_timeout = {int(busy_before)}")
        other.execute("ROLLBACK"); other.close()
    assert not db.in_transaction and _snapshot(db) == before
    assert seed.delete_test_data(db, STAFF) == 2


# ----------------------------------------------------------------------------------------------------------- U3 / U3b-e / U7

def _refused(world, expect_detail_part):
    db, c, w = world["db"], world["client"], world
    before = _snapshot(db)
    with pytest.raises(seed.TestDataClearRefused) as ei:
        seed.delete_test_data(db, STAFF)
    assert not isinstance(ei.value, RetentionGuardError)
    assert not db.in_transaction and _snapshot(db) == before                         # reads only before the refusal; nothing to roll back
    msg = str(ei.value)
    assert expect_detail_part in msg and "@" not in msg and "TEST-" not in msg
    assert msg.endswith("exceptions 0, receipt owners 0); resolve them first")
    r = c.delete("/api/test-data")
    assert r.status_code == 409 and r.json()["detail"] == msg, r.text
    after = _snapshot(db)
    assert {t: v for t, v in after.items() if t != "audit_log"} == {t: v for t, v in before.items() if t != "audit_log"}
    ref = _audit(db, "delete_test_data_refused")
    assert ref and ref[-1]["staff_member"] and ref[-1]["detail"] == msg
    assert _audit(db, "delete_test_data") == []
    return msg


def test_mixed_group_refuses_before_any_write(world):
    db, w = world["db"], world
    db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?, ?)", (w["tg"][0], w["real"][1])); db.commit()   # a group with a REAL and a TEST member
    msg = _refused(world, "groups 1")
    assert msg.startswith("test data not cleared: 1 contradiction(s)")


def test_real_flagged_link_on_a_test_person_refuses(world):
    db, w = world["db"], world
    _link(db, "tok-contradiction", w["test"][0], 0)
    _refused(world, "links 1")


def test_test_flagged_request_naming_a_real_person_refuses(world):
    db, w = world["db"], world
    _request(db, w["tg"][0], w["test"][0], w["real"][0], 1)
    _refused(world, "requests 1")


def test_real_flagged_key_pointing_at_a_test_id_refuses_in_both_scopes(world):
    db, w = world["db"], world
    _key(db, "mb-contradiction-1", w["test"][1], is_test=False)
    _refused(world, "submission keys 1")
    db.execute("DELETE FROM enquiry_submissions WHERE submit_key=?", (K("mb-contradiction-1"),)); db.commit()
    db.execute("UPDATE enquiry_submissions SET is_test=0 WHERE submit_key=?", (K("hb-test-0001"),)); db.commit()
    _refused(world, "submission keys 1")


def test_real_flagged_alert_on_a_test_homebot_enquiry_refuses(world):
    db, w = world["db"], world
    _alert(db, "homebot_enquiry", w["hb_test"], 0)
    _refused(world, "alerts 1")


def test_memberless_and_real_only_groups_are_neither_cleared_nor_counted(world):
    db, w = world["db"], world
    db.execute("INSERT INTO groups(status, is_primary, member_key, created_at, updated_at) VALUES ('Proposed', 1, 'orphan', ?, ?)", (now_iso(), now_iso()))
    empty = int(db.execute("SELECT last_insert_rowid()").fetchone()[0]); db.commit()
    assert seed.delete_test_data(db, STAFF) == 2
    assert row(db.execute("SELECT 1 FROM groups WHERE id=?", (empty,))) and all(row(db.execute("SELECT 1 FROM groups WHERE id=?", (g,))) for g in w["rg"])
    assert "groups cleared" in _audit(db, "delete_test_data")[-1]["detail"] and row(db.execute("SELECT 1 FROM groups WHERE id=?", (w["tg"][0],))) is None


def test_homebot_set_mismatch_after_the_writes_rolls_the_whole_clear_back(world, monkeypatch):
    db, w = world["db"], world
    before = _snapshot(db)
    real = homebot.delete_test_data
    monkeypatch.setattr(homebot, "delete_test_data", lambda c: real(c)[:-1])          # the helper reports a different set than captured
    with pytest.raises(seed.TestDataClearRefused, match="HomeBot test set changed"):
        seed.delete_test_data(db, STAFF)
    assert not db.in_transaction and _snapshot(db) == before and _audit(db, "delete_test_data") == []
