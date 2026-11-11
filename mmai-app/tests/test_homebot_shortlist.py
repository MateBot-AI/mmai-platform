"""HomeBot integrated matching, slice H2 (server/homebot_shortlist.py): durable jobs, versions, shortlists, rejections, planning,
inventory triggers, alerts and retention around the pure H1 module. Root amendments 1–9 of 9 Nov 2026 are each covered.
Temporary SQLite per test; the worker thread is never started (runs are driven by run_pending); no mail, no AI, no routing."""
import importlib
import json
from datetime import date, datetime, timedelta

import pytest

from server import alerts, homebot, homebot_match as hm, homebot_shortlist as hs
from server.db import now_iso, row, rows
from server.enquiries import ValidationError
from tests.test_homebot import K, body, client, post  # noqa: F401  (fixture + helpers)
from tests.test_homebot_match import KING

CUR, FIN = hm.SCENARIO_CURRENT, hm.SCENARIO_FINAL_SIZE
_REAL_BUILD = hm.build_shortlist


@pytest.fixture
def on(client, monkeypatch):
    """Switch on AFTER the app module was imported: the worker thread is not started (start_worker ran while off)."""
    monkeypatch.setenv(hs.GATE_ENV, "1")
    assert hs.enabled()
    return client


def _apt(c, i, type_code="2B2B", rent12=900, rent6=1200, avail="2026-12-01", building="10 Test Street", suburb="Melbourne CBD", **kw):
    from server import rules
    n = rules.APT_TYPES[type_code]["bedrooms"]
    r = {"id": i, "city": "Melbourne", "address": f"Unit {i} / {building}", "address_raw": f"Unit {i} / {building}", "suburb": suburb,
         "type_code": type_code, "bathrooms": rules.APT_TYPES[type_code]["bathrooms"], "rent_12m": rent12, "rent_6m": rent6,
         "available_from": avail, "min_lease_months": 2, "bills_included": 0, "updated_at": now_iso(),
         "bedroom1": KING, "bedroom2": KING if n >= 2 else None, "bedroom3": KING if n >= 3 else None}
    r.update(kw)
    keys = list(r)
    c.execute(f"INSERT INTO apartments({','.join(keys)}) VALUES ({','.join('?' * len(keys))})", [r[k] for k in keys])
    c.commit()
    return i


def inventory(c):
    """Controlled stock replacing the bootstrap import: three offerable 2-bedroom options (two in one building), one over budget."""
    c.execute("DELETE FROM apartments")
    c.commit()
    _apt(c, 1, rent12=900)
    _apt(c, 2, rent12=950)
    _apt(c, 3, type_code="3B", rent12=980, building="20 Other Road")
    _apt(c, 4, rent12=1500, building="30 Pricey Lane")


def submit(c, key="hb-key-0001", **over):
    over.setdefault("move_out", "2027-11-30")   # inside an exit window (5 Oct–7 Dec); the shared form body's default exit date is not
    r = post(c, body(**over), key)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def job(c, eid, sc=CUR):
    return row(c.db.execute("SELECT * FROM homebot_jobs WHERE enquiry_id=? AND scenario=?", (eid, sc)))


def shortlists(c, eid, sc=CUR):
    return rows(c.db.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? ORDER BY id", (eid, sc)))


def current(c, eid, sc=CUR):
    return row(c.db.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND scenario=? AND is_current=1", (eid, sc)))


def alert_kinds(c, eid):
    return [r["kind"] for r in rows(c.db.execute("SELECT kind FROM internal_alert_outbox WHERE record_type='homebot_enquiry' AND record_id=? ORDER BY id", (eid,)))]


def run_all(c):
    return hs.run_pending(c.db)


def later(ts, minutes):
    return hs._iso(datetime.fromisoformat(ts) + timedelta(minutes=minutes))


# --------------------------------------------------------------------------------------------------------- switch (default OFF)
def test_switch_is_off_by_default_nothing_is_queued_and_routes_say_so(client, monkeypatch):
    monkeypatch.delenv(hs.GATE_ENV, raising=False)
    assert not hs.enabled()
    inventory(client.db)
    eid = submit(client)
    assert job(client, eid) is None
    assert rows(client.db.execute("SELECT id FROM homebot_requirement_versions")) == []
    e = client.get(f"/api/homebot/enquiries/{eid}").json()
    assert e["matching"]["automatic"] is False and hs.SWITCHED_OFF in e["matching"]["note"]
    assert client.get(f"/api/homebot/enquiries/{eid}/options").json()["enabled"] is False
    for path, meth in ((f"/api/homebot/enquiries/{eid}/options/rerun", "post"), (f"/api/homebot/enquiries/{eid}/planning", "post"),
                       (f"/api/homebot/enquiries/{eid}/rejections", "post"), (f"/api/homebot/enquiries/{eid}/requirements", "patch")):
        r = getattr(client, meth)(path, json={"scenario": CUR, "budget_total": 1, "apartment_id": 1, "move_in": "2027-01-01"})
        assert r.status_code == 409 and r.json()["detail"] == hs.SWITCHED_OFF, (path, r.text)
    assert hs.run_pending(client.db) == []
    assert hs.on_inventory_changed(client.db, "test", [1]) == {"queued": 0, "alerts": 0}
    assert hs.start_worker(client.db) is None
    assert client.get("/api/homebot/matching-status").json()["enabled"] is False


@pytest.mark.parametrize("value,expect", [("1", True), ("on", True), ("TRUE", True), ("yes", True), ("0", False), ("off", False), ("", False), ("maybe", False)])
def test_switch_values(monkeypatch, value, expect):
    monkeypatch.setenv(hs.GATE_ENV, value)
    assert hs.enabled() is expect


# ------------------------------------------------------------------------------------------------------ creation and first run
def test_submission_creates_version_and_job_in_the_same_transaction(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    v = rows(on.db.execute("SELECT * FROM homebot_requirement_versions WHERE enquiry_id=?", (eid,)))
    assert len(v) == 1 and v[0]["scenario"] == CUR and v[0]["version"] == 1 and v[0]["created_by"] == "customer form"
    basis = json.loads(v[0]["basis_json"])
    assert basis["head_count"] == 3 and [p["institution"] for p in basis["people"]][0] == "University of Melbourne – Parkville"
    assert all(k not in json.dumps(basis) for k in ("Mei", "Tan", "example.com", "0400"))   # no identity or contact data in the basis
    jb = job(on, eid)
    assert jb["status"] == hs.QUEUED and jb["desired_generation"] == 1 and jb["claimed_generation"] == 0 and jb["reason"] == "submission"
    # atomic: a failure in the hook leaves neither enquiry nor job nor version
    monkeypatch.setattr(hs, "_ensure_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    before = on.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0]
    with pytest.raises(RuntimeError):       # TestClient re-raises server exceptions
        post(on, body(email="other@example.com"), "hb-key-0002")
    assert on.db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == before
    assert on.db.execute("SELECT COUNT(*) FROM homebot_requirement_versions").fetchone()[0] == 1


@pytest.fixture
def no_review_hook(monkeypatch):
    """Explicit ABSENCE of the lead's AI review hook (H7) for tests that describe the shortlist module on its own. In the combined build
    `ai_queue.enqueue_homebot_review` / `homebot_review_state` exist and fire by design after every stored shortlist; these tests remove
    them for their duration so their 'nothing is enqueued' expectations describe the setup, not a weakened product. The positive control
    `test_with_the_real_review_module_present_the_hand_over_is_recorded` runs whenever the real hook is present."""
    from server import ai_queue
    monkeypatch.delattr(ai_queue, "enqueue_homebot_review", raising=False)
    monkeypatch.delattr(ai_queue, "homebot_review_state", raising=False)
    assert not hasattr(ai_queue, "enqueue_homebot_review")


def test_with_the_real_review_module_present_the_hand_over_is_recorded(on):
    """Positive control (combined build only): the real H7 hook is called after the shortlist commit and the hand-over is recorded."""
    from server import ai_queue
    if not hasattr(ai_queue, "enqueue_homebot_review") or not hasattr(ai_queue, "homebot_review_state"):
        pytest.skip("the lead's AI review module is not part of this build")
    inventory(on.db)
    eid = submit(on)
    assert run_all(on)[0]["outcome"] == hs.COMPLETE
    cur = current(on, eid)
    assert cur["review_enqueued_at"] is not None and cur["review_enqueue_attempts"] == 1
    assert ai_queue.homebot_review_state(on.db, cur["id"])["eligible"] is True
    assert hs.current_shortlist(on.db, eid, CUR)["ready"] is False      # recorded hand-over is not a review verdict


def test_first_run_stores_a_current_complete_shortlist_and_raises_the_ready_alert(on, no_review_hook):
    inventory(on.db)
    eid = submit(on)
    out = run_all(on)
    assert [o["outcome"] for o in out] == [hs.COMPLETE]
    sl = current(on, eid)
    assert sl and sl["status"] == hs.COMPLETE and sl["review_state"] == hs.UNREVIEWED and sl["generation"] == 1
    assert sl["algorithm_version"] == hs.ALGORITHM_VERSION and sl["basis_hash"] and sl["inventory_hash"] == hs.inventory_hash(hs.inventory_rows(on.db))
    listed = json.loads(sl["listed_ids"])
    assert set(listed) <= {1, 2, 3} and 4 not in listed and len(listed) == 3
    res = json.loads(sl["result_json"])
    # routing credential invalid: a known campus/workplace is never 'suitable' without a measured journey
    assert all(x["class"] == hm.NEEDS_CONFIRMATION for x in res["listed"])
    jb = job(on, eid)
    assert jb["status"] == hs.DONE and jb["owner"] is None and jb["claimed_generation"] == 1 and jb["attempts"] == 0
    assert alert_kinds(on, eid) == [hs.EV_READY]
    e = on.get(f"/api/homebot/enquiries/{eid}").json()
    assert e["matching"] == {"automatic": True, "note": "Options are up to date.", "job_status": "done", "current_shortlist_id": sl["id"],
                             "shortlist_status": "complete", "review_state": "unreviewed", "never_matebot": "HomeBot enquiries are never part of MateBot housemate matching."}
    ov = on.get(f"/api/homebot/enquiries/{eid}/options").json()
    assert ov["enabled"] and list(ov["scenarios"]) == [CUR]      # complete party: no final-size scenario
    cur = ov["scenarios"][CUR]["current"]
    assert cur["status_plain"] == "Rules check complete (not yet reviewed)." and cur["review"]["state"] == "unreviewed"
    assert {x["apartment_id"] for x in cur["listed"]} == set(listed)
    text = json.dumps(ov)
    for code in hm.PLAIN:   # staff see words, never rule codes (T10)
        assert f'"{code}"' not in text, code
    assert any(x["apartment_id"] == 4 and x["class_plain"] == "Not suitable" for x in cur["why_not"])
    assert run_all(on) == []    # nothing due afterwards


def test_complete_means_computed_not_reviewed_and_review_state_comes_from_the_lead_hook(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sid = current(on, eid)["id"]
    from server import ai_queue
    seen = {}
    monkeypatch.setattr(ai_queue, "homebot_review_state", lambda c, vid: seen.setdefault("vid", vid) and {"eligible": True, "reviewed": {"verdict": "ok"}}, raising=False)
    cur = hs.current_shortlist(on.db, eid, CUR)
    assert seen["vid"] == sid and cur["review"] == {"state": "reviewed", "detail": {"verdict": "ok"}}
    assert cur["status"] == hs.COMPLETE   # the stored status is still the computation status


def test_review_hook_runs_after_the_shortlist_commit_as_its_own_step_and_its_failure_is_recorded_not_hidden(on, monkeypatch):
    """Root amendment (9 Nov): the hand-over to the AI review queue is a separate guarded step AFTER the shortlist is durably
    stored. A failing hook leaves no partial writes, is counted, and the options are explicitly 'retrying' (held) — never ready."""
    inventory(on.db)
    eid = submit(on)
    from server import ai_queue
    calls = []

    def hook(c, sid, commit=True):
        calls.append((sid, commit))
        c.execute("INSERT INTO settings(key, value) VALUES (?,?)", ("partial-enqueue-marker", "x"))
        raise RuntimeError("queue down")
    monkeypatch.setattr(ai_queue, "enqueue_homebot_review", hook, raising=False)
    assert run_all(on)[0]["outcome"] == hs.COMPLETE
    cur = current(on, eid)
    assert calls == [(cur["id"], False)]
    assert row(on.db.execute("SELECT value FROM settings WHERE key='partial-enqueue-marker'")) is None   # the hook's partial write rolled back
    assert cur["review_enqueued_at"] is None and cur["review_enqueue_attempts"] == 1
    out = hs.current_shortlist(on.db, eid, CUR)
    assert out["ready"] is False and out["review"]["state"] == "retrying" and "held" in out["review"]["plain"]
    assert alert_kinds(on, eid) == [hs.EV_READY]   # no 'review unavailable' alert yet: retries continue


def test_review_handover_is_retried_durably_then_held_with_a_fixed_alert_and_an_explicit_staff_retry(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    from server import ai_queue
    state = {"fail": True, "calls": 0}

    def hook(c, sid, commit=True):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("queue down")
    monkeypatch.setattr(ai_queue, "enqueue_homebot_review", hook, raising=False)
    run_all(on)
    sl = current(on, eid)
    sid, t0 = sl["id"], sl["created_at"]
    assert sl["review_enqueue_attempts"] == 1
    assert hs.sweep_review_enqueue(on.db, t0) == [] and hs.sweep_review_enqueue(on.db, later(t0, 0.5)) == []   # backed off, not hammered
    for n in range(2, hs.REVIEW_ENQUEUE_MAX + 1):
        ts = later(t0, hs.BACKOFF_MIN[n - 2])
        assert hs.sweep_review_enqueue(on.db, later(t0, hs.BACKOFF_MIN[n - 2] - 0.1)) == []
        assert len(hs.sweep_review_enqueue(on.db, ts)) == 1
        assert current(on, eid)["review_enqueue_attempts"] == n
    assert hs.sweep_review_enqueue(on.db, later(t0, 100000)) == []   # bounded: no further automatic attempts
    assert state["calls"] == hs.REVIEW_ENQUEUE_MAX
    out = hs.current_shortlist(on.db, eid, CUR)
    assert out["review"]["state"] == "held" and out["review"]["retry"] is True and out["ready"] is False
    assert out["review"]["plain"].startswith("AI review did not complete; options remain held.") and "by hand" not in out["review"]["plain"]
    assert alert_kinds(on, eid) == [hs.EV_READY, hs.EV_REVIEW_UNAVAILABLE]
    assert "remain held" in alerts.EVENTS[hs.EV_REVIEW_UNAVAILABLE][0]
    # explicit staff retry: counter reset, one immediate attempt; once the queue is back the mark is set and the state is 'queued'
    state["fail"] = False
    res = hs.retry_review(on.db, eid, CUR, "Anna Chan")
    assert res["outcome"] == "enqueued" and res["shortlist_id"] == sid
    cur = current(on, eid)
    assert cur["review_enqueued_at"] and cur["review_enqueue_attempts"] == 1
    assert hs.current_shortlist(on.db, eid, CUR)["review"]["state"] == "queued"
    assert hs.retry_review(on.db, eid, CUR, "Anna Chan")["outcome"] == "already_queued"
    r = on.post(f"/api/homebot/enquiries/{eid}/options/review-retry", json={})
    assert r.status_code == 200 and r.json()["outcome"] == "already_queued"


def test_without_the_review_hook_nothing_is_marked_and_the_sweep_is_a_no_op(on, no_review_hook):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    cur = current(on, eid)
    assert cur["review_enqueued_at"] is None and cur["review_enqueue_attempts"] == 0
    assert hs.current_shortlist(on.db, eid, CUR)["review"]["state"] == hs.UNREVIEWED


def test_hook_exception_class_and_message_never_reach_the_log(on, monkeypatch, caplog):
    inventory(on.db)
    eid = submit(on)
    from server import ai_queue
    odd = type("PRIVATE_CLASS_MARKER", (RuntimeError,), {})

    def hook(c, sid, commit=True):
        raise odd("PRIVATE_MESSAGE_MARKER")
    monkeypatch.setattr(ai_queue, "enqueue_homebot_review", hook, raising=False)
    run_all(on)
    assert "PRIVATE_CLASS_MARKER" not in caplog.text and "PRIVATE_MESSAGE_MARKER" not in caplog.text and "kind=unknown" in caplog.text


@pytest.mark.parametrize("status", ["Closed", "Leased"])
def test_enquiry_closed_or_leased_during_computation_publishes_nothing(on, monkeypatch, status):
    inventory(on.db)
    eid = submit(on)
    build = hm.build_shortlist

    def close_then_build(*a, **kw):
        res = build(*a, **kw)
        homebot.update(on.db, eid, {"status": status}, "Anna Chan")
        return res
    monkeypatch.setattr(hm, "build_shortlist", close_then_build)
    res = hs.run_job(on.db, job(on, eid)["id"])
    assert res["outcome"] == "enquiry_closed"
    assert shortlists(on, eid) == [] and alert_kinds(on, eid) == [] and job(on, eid)["status"] == hs.CANCELLED


def test_every_job_write_is_owner_fenced_and_a_lost_fence_rolls_the_whole_write_back(on):
    """The first statement of each final transaction is the fenced job UPDATE; when it matches nothing the transaction is
    abandoned: no shortlist row, no alert, no job change, and the rival's claim is untouched."""
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    t0 = now_iso()
    old = hs._claim(on.db, jid, "old", t0)
    ts = later(t0, 11)
    new = hs._claim(on.db, jid, "new", ts)
    assert new and new["owner"] == "new"
    seen = []
    real = on.db.execute

    def spy(sql, params=()):
        seen.append(" ".join(sql.split()))
        return real(sql, params)
    on.db.execute = spy
    try:
        e = homebot.get(on.db, eid)
        ver = hs._current_version(on.db, eid, CUR)
        inv = hs.inventory_rows(on.db)
        sl = hm.build_shortlist(hs._requirements(on.db, e, CUR), inv, date(2026, 11, 8))
        assert hs._store(on.db, old, "old", ts, e, ver, hs.inventory_hash(inv), "h", date(2026, 11, 8), sl)["outcome"] == "lost_claim"
    finally:
        on.db.execute = real
    writes = [q for q in seen if q.upper().startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes == [] or all("homebot_jobs" in q and "owner=?" in q for q in writes), writes   # nothing but the fence was attempted
    assert shortlists(on, eid) == [] and alert_kinds(on, eid) == [] and job(on, eid)["owner"] == "new" and job(on, eid)["status"] == hs.RUNNING
    # and the fence predicate is on every job write the module issues
    import inspect
    src = inspect.getsource(hs)
    for frag in ("UPDATE homebot_jobs SET status=CASE", "UPDATE homebot_jobs SET status=?, owner=NULL, locked_at=NULL, error_kind", "UPDATE homebot_jobs SET status=?, desired_generation=CASE"):
        assert frag in src and src.count(frag) == src.count('_fenced(c, "' + frag), frag


def test_basis_mutators_run_under_the_retention_guard_not_a_bare_transaction(on):
    """Requirements, planning, rejections, reopen/re-offer, rerun request, inventory trigger and the enquiry status change all take
    the retention guard (PostgreSQL: the advisory lock the publication also takes), so an edit on another connection cannot slip
    between the publication's freshness read and its commit."""
    from server import db as dbm
    labels = []
    hook = lambda ev, c, label: labels.append(label) if ev == "entered" else None
    dbm.GUARD_EVENTS.append(hook)
    try:
        inventory(on.db)
        eid = submit(on, **GROWING)
        run_all(on)
        hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna Chan")
        hs.confirm_planning(on.db, eid, 1500, "Anna Chan")
        rid = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Anna Chan")["id"]
        hs.reopen_by_customer(on.db, rid, "Anna Chan", "asked again")
        rid2 = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna Chan")["id"]
        hs.allow_reoffer(on.db, rid2, 2, "Anna Chan", "rent dropped")
        hs.request_rerun(on.db, eid, "Anna Chan")
        hs.on_inventory_changed(on.db, "apartment changed", [1])
        homebot.update(on.db, eid, {"status": "Closed"}, "Anna Chan")
    finally:
        dbm.GUARD_EVENTS.remove(hook)
    for lbl in ("homebot requirements edit", "homebot planning confirm", "homebot rejection record", "homebot rejection reopen", "homebot reoffer allowance",
                "homebot rerun request", "homebot inventory trigger", "homebot enquiry update", "homebot shortlist write"):
        assert lbl in labels, (lbl, labels)


def test_current_options_whose_basis_moved_are_shown_out_of_date_never_as_current(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    assert hs.current_shortlist(on.db, eid, CUR)["stale"] is None
    # an apartment edit after the run (the trigger queues a fresh run; until it lands the old options are labelled out of date)
    with hs.transaction(on.db):
        on.db.execute("UPDATE apartments SET rent_12m=rent_12m+5 WHERE id=1")
    out = hs.current_shortlist(on.db, eid, CUR)
    assert out["stale"] and out["stale"].startswith("Out of date: the apartment list changed")
    run_all(on) if job(on, eid)["status"] == hs.QUEUED else hs.on_inventory_changed(on.db, "apartment changed", [1]) and run_all(on)
    assert hs.current_shortlist(on.db, eid, CUR)["stale"] is None
    # a requirement change likewise, until its run lands
    hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna Chan")
    out = hs.current_shortlist(on.db, eid, CUR)
    assert out["stale"] and "requirements" in out["stale"]
    run_all(on)
    assert hs.current_shortlist(on.db, eid, CUR)["stale"] is None


# -------------------------------------------------------------------------------------------------- claims, fencing, coalescing
def test_claim_is_compare_and_set_second_claimant_gets_nothing(on):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    ts = now_iso()
    a = hs._claim(on.db, jid, "tokenA", ts)
    assert a and a["status"] == hs.RUNNING and a["owner"] == "tokenA" and a["claimed_generation"] == 1 and a["attempts"] == 1
    assert hs._claim(on.db, jid, "tokenB", ts) is None
    assert hs._mine(on.db, a, "tokenA") and not hs._mine(on.db, a, "tokenB")


def test_stale_claim_is_taken_over_and_the_old_owner_cannot_write(on):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    t0 = now_iso()
    old = hs._claim(on.db, jid, "old", t0)
    assert old
    assert hs._claim(on.db, jid, "new", later(t0, 9)) is None   # within STALE_CLAIM_MIN: still the old owner's claim
    ts = later(t0, 11)   # 11 minutes later: past STALE_CLAIM_MIN
    new = hs._claim(on.db, jid, "new", ts)
    assert new and new["owner"] == "new" and new["attempts"] == 2
    e = homebot.get(on.db, eid)
    ver = hs._current_version(on.db, eid, CUR)
    inv = hs.inventory_rows(on.db)
    sl = hm.build_shortlist(hs._requirements(on.db, e, CUR), inv, date(2026, 11, 8))
    res = hs._store(on.db, old, "old", ts, e, ver, hs.inventory_hash(inv), "h", date(2026, 11, 8), sl)
    assert res["outcome"] == "lost_claim" and shortlists(on, eid) == [] and alert_kinds(on, eid) == []
    assert hs._finish(on.db, old, "old", ts, outcome="done")["outcome"] == "lost_claim"
    assert hs._fail(on.db, old, "old", ts, "unknown")["outcome"] == "lost_claim"
    assert job(on, eid)["owner"] == "new" and job(on, eid)["status"] == hs.RUNNING
    res = hs._store(on.db, new, "new", ts, e, ver, hs.inventory_hash(inv), "h", date(2026, 11, 8), sl)
    assert res["outcome"] == hs.COMPLETE and current(on, eid)["id"] == res["shortlist_id"]


def test_triggers_during_a_run_raise_desired_not_claimed_and_requeue_at_the_end(on):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    jb = hs._claim(on.db, jid, "t", now_iso())
    hs.request_rerun(on.db, eid, "Anna", CUR)
    hs.request_rerun(on.db, eid, "Anna", CUR)
    j2 = job(on, eid)
    assert j2["status"] == hs.RUNNING and j2["owner"] == "t" and j2["claimed_generation"] == 1 and j2["desired_generation"] == 3
    assert hs._finish(on.db, jb, "t", now_iso(), outcome="done")["outcome"] == "done"
    j3 = job(on, eid)
    assert j3["status"] == hs.QUEUED and j3["owner"] is None and j3["desired_generation"] == 3
    out = run_all(on)
    assert len(out) == 1 and out[0]["outcome"] == hs.COMPLETE and job(on, eid)["status"] == hs.DONE and job(on, eid)["claimed_generation"] == 3


def test_repeated_triggers_coalesce_into_one_job_row_and_one_run(on):
    inventory(on.db)
    eid = submit(on)
    for i in range(5):
        hs.on_inventory_changed(on.db, "test", [1])
    assert on.db.execute("SELECT COUNT(*) FROM homebot_jobs WHERE enquiry_id=?", (eid,)).fetchone()[0] == 1
    assert job(on, eid)["desired_generation"] == 6
    out = run_all(on)
    assert len(out) == 1 and len(shortlists(on, eid)) == 1 and job(on, eid)["status"] == hs.DONE


def test_idempotency_uses_job_generation_and_exact_basis_not_the_requirement_version(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sid = current(on, eid)["id"]
    # the same claim generation and the same basis again (e.g. a restart after a lost 'done' write): no duplicate row, no new alert
    on.db.execute("UPDATE homebot_jobs SET status='queued', next_attempt_at=NULL WHERE enquiry_id=?", (eid,))
    on.db.commit()
    out = run_all(on)
    assert out[0]["outcome"] == "already_stored" and out[0]["shortlist_id"] == sid and len(shortlists(on, eid)) == 1
    assert alert_kinds(on, eid) == [hs.EV_READY]
    # same requirement version but a NEW generation (inventory changed, outcome identical): a new row is stored, version unchanged
    hs.on_inventory_changed(on.db, "test", None)
    out = run_all(on)
    assert out[0]["outcome"] == hs.COMPLETE and out[0]["shortlist_id"] != sid and len(shortlists(on, eid)) == 2
    assert current(on, eid)["requirement_version_id"] == shortlists(on, eid)[0]["requirement_version_id"]
    assert alert_kinds(on, eid) == [hs.EV_READY]      # offers identical: no 'changed' alert


# ------------------------------------------------------------------------------------------- stale results never become current
def _compute_then(on, action):
    """Make build_shortlist perform `action` (an edit while computing) before returning its result."""
    real = hm.build_shortlist

    def patched(req, apartments, today, estimates=None, progress=None):
        out = real(req, apartments, today, estimates, progress=progress)
        action()
        return out
    return patched


def test_inventory_edit_while_computing_stores_superseded_and_requeues(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    real_builder = _REAL_BUILD
    assert hm.build_shortlist is real_builder

    def edit():
        on.db.execute("UPDATE apartments SET rent_12m=910 WHERE id=1")
        on.db.commit()
    with monkeypatch.context() as phase:
        phase.setattr(hm, "build_shortlist", _compute_then(on, edit))
        out = run_all(on)
    assert hm.build_shortlist is real_builder
    assert out[0]["outcome"] == hs.SUPERSEDED
    sls = shortlists(on, eid)
    assert len(sls) == 1 and sls[0]["status"] == hs.SUPERSEDED and sls[0]["is_current"] == 0 and current(on, eid) is None
    jb = job(on, eid)
    assert jb["status"] == hs.QUEUED and jb["desired_generation"] == 2 and jb["owner"] is None
    assert alert_kinds(on, eid) == []
    out = run_all(on)
    assert out[0]["outcome"] == hs.COMPLETE and current(on, eid)["generation"] == 2 and len(shortlists(on, eid)) == 2
    assert hs.history(on.db, eid)[1]["status_plain"].startswith("Out of date")


def test_lease_and_delete_while_computing_are_stale_too(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    real_builder = _REAL_BUILD
    assert hm.build_shortlist is real_builder
    callbacks = {"lease": 0, "delete": 0}

    def lease():
        callbacks["lease"] += 1
        on.db.execute("UPDATE apartments SET leased_at=? WHERE id=2", (now_iso(),))
        on.db.commit()
    # Each phase must wrap the real builder, not the prior phase's simulated edit.
    with monkeypatch.context() as phase:
        phase.setattr(hm, "build_shortlist", _compute_then(on, lease))
        assert run_all(on)[0]["outcome"] == hs.SUPERSEDED
    assert hm.build_shortlist is real_builder
    assert callbacks == {"lease": 1, "delete": 0}

    def delete():
        callbacks["delete"] += 1
        on.db.execute("DELETE FROM apartments WHERE id=3")
        on.db.commit()
    with monkeypatch.context() as phase:
        phase.setattr(hm, "build_shortlist", _compute_then(on, delete))
        assert run_all(on)[0]["outcome"] == hs.SUPERSEDED
    assert hm.build_shortlist is real_builder
    assert callbacks == {"lease": 1, "delete": 1}
    out = run_all(on)
    assert out[0]["outcome"] == hs.COMPLETE and json.loads(current(on, eid)["listed_ids"]) == [1]
    assert callbacks == {"lease": 1, "delete": 1}
    assert hm.build_shortlist is real_builder


def test_requirement_change_while_computing_is_stale(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    real_builder = _REAL_BUILD
    assert hm.build_shortlist is real_builder
    with monkeypatch.context() as phase:
        phase.setattr(hm, "build_shortlist", _compute_then(on, lambda: hs.update_requirements(on.db, eid, {"budget_total_weekly": 960}, "Anna")))
        assert run_all(on)[0]["outcome"] == hs.SUPERSEDED
    assert hm.build_shortlist is real_builder
    assert current(on, eid) is None
    assert run_all(on)[0]["outcome"] == hs.COMPLETE
    sl = current(on, eid)
    assert sl["requirement_version_id"] == hs._current_version(on.db, eid, CUR)["id"] and 4 not in json.loads(sl["listed_ids"])


def test_enquiry_deleted_while_computing_leaves_nothing_behind(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    monkeypatch.setattr(hm, "build_shortlist", _compute_then(on, lambda: homebot.delete(on.db, eid, "Anna")))
    out = run_all(on)
    assert out[0]["outcome"] in ("enquiry_gone", "lost_claim")
    for t in hs.TABLES:
        assert on.db.execute(f"SELECT COUNT(*) FROM {t} WHERE enquiry_id=?", (eid,)).fetchone()[0] == 0, t
    assert alert_kinds(on, eid) == []


# ------------------------------------------------------------------------------------------------------ availability triggers
def test_apartment_change_re_evaluates_the_whole_inventory_for_every_open_enquiry(on):
    inventory(on.db)
    e1 = submit(on, "hb-key-0001")
    e2 = submit(on, "hb-key-0002", email="b@example.com")
    closed = submit(on, "hb-key-0003", email="c@example.com")
    on.patch(f"/api/homebot/enquiries/{closed}", json={"status": "Closed"})
    run_all(on)
    assert json.loads(current(on, e1)["listed_ids"]) == json.loads(current(on, e2)["listed_ids"])
    assert current(on, closed) is None and job(on, closed)["status"] == hs.CANCELLED
    # a previously excluded apartment becomes feasible (price drop) and a brand-new one appears: both are reconsidered
    r = on.patch("/api/apartments/4", json={"rent_12m": 800})
    assert r.status_code == 200
    r = on.post("/api/apartments", json={"address": "Unit 5 / 40 New Street", "suburb": "Melbourne CBD", "type_code": "2B2B", "rent_12m": 700, "rent_6m": 900,
                                         "available_from": "2026-12-01", "bedroom1": KING, "bedroom2": KING})
    assert r.status_code == 201, r.text
    aid5 = r.json()["id"]
    assert job(on, e1)["status"] == hs.QUEUED and job(on, e2)["status"] == hs.QUEUED and job(on, closed)["status"] == hs.CANCELLED
    run_all(on)
    for eid in (e1, e2):
        listed = json.loads(current(on, eid)["listed_ids"])
        assert aid5 in listed and 4 in listed and len(listed) == 3    # cheaper options displaced the dearer ones
        assert alert_kinds(on, eid) == [hs.EV_READY, hs.EV_CHANGED]


def test_leased_listed_apartment_raises_unavailable_alert_and_drops_out(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    assert 1 in json.loads(current(on, eid)["listed_ids"])
    on.db.execute("UPDATE apartments SET leased_at=? WHERE id=1", (now_iso(),))
    on.db.commit()
    res = hs.on_inventory_changed(on.db, "apartment leased", [1])
    assert res == {"queued": 1, "alerts": 1}
    assert alert_kinds(on, eid) == [hs.EV_READY, hs.EV_UNAVAILABLE]
    run_all(on)
    assert 1 not in json.loads(current(on, eid)["listed_ids"])
    assert alert_kinds(on, eid) == [hs.EV_READY, hs.EV_UNAVAILABLE, hs.EV_CHANGED]
    # deleting an apartment that is not listed: run queued, no unavailable alert
    on.db.execute("DELETE FROM apartments WHERE id=4")
    on.db.commit()
    assert hs.on_inventory_changed(on.db, "apartment deleted", [4]) == {"queued": 1, "alerts": 0}


def test_apartment_routes_trigger_runs_and_lease_route_triggers_unavailable(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    r = on.delete("/api/apartments/2")
    assert r.status_code == 200 and job(on, eid)["status"] == hs.QUEUED and alert_kinds(on, eid) == [hs.EV_READY, hs.EV_UNAVAILABLE]
    run_all(on)
    assert 2 not in json.loads(current(on, eid)["listed_ids"])


# ------------------------------------------------------------------------------------------------- rejections (H25) and GP14
def test_apartment_rejection_is_immutable_excludes_it_and_customer_reopening_restores_it(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sid = current(on, eid)["id"]
    r = on.post(f"/api/homebot/enquiries/{eid}/rejections", json={"scope": "apartment", "apartment_id": 1, "customer_reason": "Too far from campus"})
    assert r.status_code == 201, r.text
    rj = r.json()
    assert rj["status"] == "active" and rj["shortlist_id"] == sid and rj["offered_basis"]["candidate"]["apartment_id"] == 1
    assert rj["offered_basis"]["apartment"]["rent_12m"] == 900 and [ev["event"] for ev in rj["events"]] == ["recorded"]
    v = rows(on.db.execute("SELECT version, change_note FROM homebot_requirement_versions WHERE enquiry_id=? ORDER BY version", (eid,)))
    assert [x["version"] for x in v] == [1, 2] and "declined" in v[1]["change_note"]
    run_all(on)
    sl = current(on, eid)
    assert 1 not in json.loads(sl["listed_ids"]) and sl["requirement_version_id"] == hs._current_version(on.db, eid, CUR)["id"]
    res = json.loads(sl["result_json"])
    gone = next(x for x in res["candidates"] if x["apartment_id"] == 1)
    assert gone["class"] == hm.EXCLUDED
    # the offered basis does not move when the apartment changes afterwards
    on.db.execute("UPDATE apartments SET rent_12m=850 WHERE id=1")
    on.db.commit()
    assert hs.rejection(on.db, rj["id"])["offered_basis"]["apartment"]["rent_12m"] == 900
    # route 1: customer asked to reconsider — staff action with a note; everything stays visible
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reopen", json={})
    assert r.status_code == 422
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reopen", json={"note": "Customer called back and wants to see it after all"})
    assert r.status_code == 200, r.text
    ro = r.json()
    assert ro["status"] == "reopened" and ro["reopen_route"] == "customer_request" and [ev["event"] for ev in ro["events"]] == ["recorded", "reopened_customer"]
    assert ro["offered_basis"]["apartment"]["rent_12m"] == 900
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reopen", json={"note": "again"})
    assert r.status_code == 422
    run_all(on)
    assert 1 in json.loads(current(on, eid)["listed_ids"])
    assert len(hs.overview(on.db, eid)["rejections"]) == 1 and len(hs.overview(on.db, eid)["versions"]) == 3


def test_building_rejection_and_narrow_material_change_reoffer(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna", customer_reason="Noisy street")
    assert rj["scope"] == "building" and rj["building_key"] == hm.building_identity("Unit 1 / 10 Test Street", "Melbourne CBD", "Melbourne")   # 11 Nov 2026: full identity stored
    run_all(on)
    assert set(json.loads(current(on, eid)["listed_ids"])) == {3}          # units 1 and 2 share the building
    # nothing changed: a re-offer cannot be allowed on that basis
    with pytest.raises(ValidationError):
        hs.allow_reoffer(on.db, rj["id"], 2, "Anna", "Customer might like unit 2")
    # unit 2's rent drops: material change for THAT apartment only
    on.db.execute("UPDATE apartments SET rent_12m=800 WHERE id=2")
    on.db.commit()
    assert hs.material_change(on.db, rj["id"], 1) == []
    diff = hs.material_change(on.db, rj["id"], 2)
    assert diff == [{"field": "rent_12m", "was": 950, "now": 800}]     # unit 2 against ITS OWN state when the building was declined
    with pytest.raises(ValidationError):
        hs.material_change(on.db, rj["id"], 3)     # not in the declined building
    out = hs.allow_reoffer(on.db, rj["id"], 2, "Anna", "Rent came down; customer said price was the issue")
    assert out["status"] == "active"                                          # the building rejection stays
    assert [ev["event"] for ev in out["events"]] == ["recorded", "reoffer_allowed"] and out["events"][1]["apartment_id"] == 2
    assert out["events"][1]["change_basis"][0]["field"] == "rent_12m"
    assert hs.active_rejections(on.db, eid) == ([], [rj["building_key"]], [2])
    run_all(on)
    listed = json.loads(current(on, eid)["listed_ids"])
    assert 2 in listed and 1 not in listed and 3 in listed
    # a second cycle keeps every event
    r = hs.reopen_by_customer(on.db, rj["id"], "Anna", "Customer wants the whole building reconsidered")
    assert [ev["event"] for ev in r["events"]] == ["recorded", "reoffer_allowed", "reopened_customer"]
    run_all(on)
    assert set(json.loads(current(on, eid)["listed_ids"])) == {1, 2, 3}
    assert len(rows(on.db.execute("SELECT id FROM homebot_rejection_events WHERE enquiry_id=?", (eid,)))) == 3


def test_apartment_scope_material_change_route(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=3, staff="Anna")
    r = on.get(f"/api/homebot/rejections/{rj['id']}/material-change", params={"apartment_id": 3})
    assert r.status_code == 200 and r.json() == {"changes": []}
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reoffer", json={"apartment_id": 3, "note": "x"})
    assert r.status_code == 422 and "Nothing material" in r.json()["errors"]["apartment_id"]
    on.patch("/api/apartments/3", json={"available_from": "2026-12-20"})
    r = on.get(f"/api/homebot/rejections/{rj['id']}/material-change", params={"apartment_id": 3})
    assert [d["field"] for d in r.json()["changes"]] == ["available_from"]
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reoffer", json={"apartment_id": 3, "note": "Later date now fits"})
    assert r.status_code == 200 and r.json()["status"] == "reopened" and r.json()["reopen_route"] == "material_change"
    r = on.post(f"/api/homebot/rejections/{rj['id']}/reoffer", json={"apartment_id": 1, "note": "x"})
    assert r.status_code == 422


# -------------------------------------------------------------------------------------------------- planning (H24) and scenarios
GROWING = dict(need_more="more", headcount_now=2, headcount_target=3, want_help="no",
               people=[{"sleep": "Own bedroom", "weekly_max": 500, "doing": "Working", "place": "Collins St, CBD"},
                       {"name": "Jun", "sleep": "Own bedroom", "weekly_max": 500, "doing": "Working", "place": "Docklands"}],
               budget_total=1000, bedrooms=["2 bedrooms", "3 bedrooms"], budget_types=["2B2B", "3B"])


def test_planning_budget_creates_a_separately_labelled_final_size_scenario(on):
    inventory(on.db)
    eid = submit(on, **GROWING)
    assert hs.planning_state(on.db, homebot.get(on.db, eid))["note"] == "No planning budget has been confirmed yet."
    r = on.post(f"/api/homebot/enquiries/{eid}/planning", json={"budget_total": "x"})
    assert r.status_code == 422
    r = on.post(f"/api/homebot/enquiries/{eid}/planning", json={"budget_total": 1600})
    assert r.status_code == 200 and r.json()["applies"] and r.json()["target_size"] == 3 and r.json()["budget_total"] == 1600
    assert job(on, eid, FIN)["status"] == hs.QUEUED
    out = run_all(on)
    assert sorted(o["outcome"] for o in out) == [hs.COMPLETE, hs.COMPLETE]
    cur, fin = current(on, eid, CUR), current(on, eid, FIN)
    assert cur and fin and cur["id"] != fin["id"] and cur["requirement_version_id"] != fin["requirement_version_id"]
    bc, bf = json.loads(hs._current_version(on.db, eid, CUR)["basis_json"]), json.loads(hs._current_version(on.db, eid, FIN)["basis_json"])
    assert bc["scenario"] == CUR and bf["scenario"] == FIN and bf["whole_apartment_max"] == "1600" and bc["whole_apartment_max"] == "1000"
    ov = hs.overview(on.db, eid)
    assert ov["scenarios"][FIN]["label"].startswith("Provisional") and ov["scenarios"][CUR]["label"] != ov["scenarios"][FIN]["label"]
    k4 = lambda sl: next(x["class"] for x in json.loads(sl["result_json"])["candidates"] if x["apartment_id"] == 4)
    assert k4(cur) == hm.EXCLUDED and k4(fin) != hm.EXCLUDED     # $1,500 is over the party's $1,000 but within the $1,600 planning budget
    # a complete party cannot have a planning budget
    eid2 = submit(on, "hb-key-0002", email="b@example.com")
    r = on.post(f"/api/homebot/enquiries/{eid2}/planning", json={"budget_total": 1600})
    assert r.status_code == 422


def test_changing_headcount_or_budget_invalidates_the_planning_confirmation(on):
    inventory(on.db)
    eid = submit(on, **GROWING)
    hs.confirm_planning(on.db, eid, 1400, "Anna")
    run_all(on)
    assert current(on, eid, FIN)
    hs.update_requirements(on.db, eid, {"headcount_target": 4}, "Anna")
    st = hs.planning_state(on.db, homebot.get(on.db, eid))
    assert st["applies"] is False and "no longer applies" in st["note"]
    assert row(on.db.execute("SELECT superseded_reason FROM homebot_planning WHERE enquiry_id=?", (eid,)))["superseded_reason"] == "party size or stated budget changed"
    with pytest.raises(ValidationError):
        hs.request_rerun(on.db, eid, "Anna", FIN)
    assert job(on, eid, CUR)["status"] == hs.QUEUED
    out = run_all(on)
    outcomes = {o["job_id"]: o["outcome"] for o in out}
    assert outcomes[job(on, eid, CUR)["id"]] == hs.COMPLETE
    # the final-size job, if it ran, was cancelled (no applicable planning budget) and the old final-size result is kept as history only
    assert job(on, eid, FIN)["status"] in (hs.CANCELLED, hs.DONE, hs.QUEUED)
    hs.confirm_planning(on.db, eid, 1600, "Anna")
    assert hs.planning_state(on.db, homebot.get(on.db, eid))["applies"] and row(on.db.execute(
        "SELECT target_size FROM homebot_planning WHERE enquiry_id=? AND superseded_at IS NULL", (eid,)))["target_size"] == 4
    # a stated-budget change by staff also invalidates
    hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna")
    assert hs.planning_state(on.db, homebot.get(on.db, eid))["applies"] is False


# -------------------------------------------------------------------------------------------- staff requirement edits (H29)
def test_requirement_edit_is_validated_by_the_form_validator_and_versions_the_basis(on):
    inventory(on.db)
    eid = submit(on)
    v1 = hs._current_version(on.db, eid, CUR)
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"email": "x@y.z"})
    assert r.status_code == 422 and "cannot be changed here" in r.json()["errors"]["form"]
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"move_in": "2020-01-01"})
    assert r.status_code == 422 and "move_in" in r.json()["errors"]
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"people": [{"institution": "Monash University – Clayton"}, {}, {}]})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["people"][0]["institution"] == "Monash University – Clayton" and e["people"][1]["name"] == "Jun"
    v2 = hs._current_version(on.db, eid, CUR)
    assert v2["version"] == 2 and v2["basis_hash"] != v1["basis_hash"] and "people" in v2["change_note"]
    assert v2["created_by"] == "Anna Chan"   # staff display name recorded
    # an edit that changes nothing matching-relevant does not create a version
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"apartment_notes": ""})
    assert r.status_code == 200 and hs._current_version(on.db, eid, CUR)["version"] == 2
    r = on.patch(f"/api/homebot/enquiries/{eid}/requirements", json={"people": [{"name": "Zed"}]})
    assert r.status_code == 422
    assert job(on, eid)["status"] == hs.QUEUED and job(on, eid)["reason"] == "requirements_changed"


# ------------------------------------------------------------------------------------------------- failures: finite categories
def test_failures_are_finite_tokens_with_backoff_and_a_final_alert_and_never_store_messages(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)

    def boom(*a, **k):
        raise RuntimeError("secret traceback text /srv/mmai/x.py")   # accepts the progress kwarg too
    monkeypatch.setattr(hm, "build_shortlist", boom)
    t0 = now_iso()
    out = hs.run_pending(on.db, now=t0)
    assert out[0] == {"job_id": job(on, eid)["id"], "outcome": "failed", "error_kind": "unknown", "final": False}
    jb = job(on, eid)
    assert jb["status"] == hs.QUEUED and jb["error_kind"] == "unknown" and jb["attempts"] == 1 and jb["next_attempt_at"] == later(t0, 1)
    assert hs.run_pending(on.db, now=later(t0, 0.5)) == []        # not yet due
    nxt = [later(t0, 1), later(t0, 6), later(t0, 21), later(t0, 81)]      # 1, 5, 15, 60 minute back-off steps
    for i, t in enumerate(nxt, start=2):
        res = hs.run_pending(on.db, now=t)
        assert res and res[0]["outcome"] == "failed" and job(on, eid)["attempts"] == i
    jb = job(on, eid)
    assert jb["status"] == hs.FAILED and jb["next_attempt_at"] is None and jb["error_kind"] == "unknown"
    assert alert_kinds(on, eid) == [hs.EV_FAILED]
    dump = json.dumps(rows(on.db.execute("SELECT * FROM homebot_jobs")) + rows(on.db.execute("SELECT * FROM audit_log")) + rows(on.db.execute("SELECT * FROM internal_alert_outbox")))
    assert "secret traceback" not in dump and "RuntimeError" not in dump and "/srv/" not in dump
    st = hs.job_state(on.db, eid, CUR)
    assert st["plain"] == "Options could not be prepared because of an unexpected problem; retries have stopped — use Run again."
    assert hs.run_pending(on.db, now=later(t0, 100000)) == []          # failed jobs are not retried on their own
    monkeypatch.undo()
    monkeypatch.setenv(hs.GATE_ENV, "1")
    assert hs.request_rerun(on.db, eid, "Anna", CUR)["status"] == hs.QUEUED
    assert hs.run_pending(on.db)[0]["outcome"] == hs.COMPLETE
    assert current(on, eid)


@pytest.mark.parametrize("exc,kind", [(hm.InvalidRequirements("x"), "requirements_invalid"), (ValidationError({"a": "b"}), "requirements_invalid"),
                                      (KeyError("x"), "requirements_invalid"), (__import__("sqlite3").OperationalError("locked"), "database"),
                                      (hs.RetentionGuardError("g"), "database"), (ValueError("v"), "unknown"), (ZeroDivisionError(), "unknown")])
def test_error_kind_mapping_is_finite(exc, kind):
    from server import homebot_bedding
    assert hs._error_kind(exc) == kind
    assert hs._error_kind(homebot_bedding.InvalidInventory("bad")) == "inventory_unreadable"
    assert set(hs.ERROR_SENTENCES) == {"requirements_invalid", "inventory_unreadable", "database", "unknown"}


def test_unresolved_partial_is_explicit_never_a_no_match_claim(on, monkeypatch):
    inventory(on.db)
    eid = submit(on, move_out_flex="Flexible by a month or more", move_out_earliest="2027-10-10", move_out_latest="2027-12-07")
    monkeypatch.setattr(hm, "MAX_PAIRS", 10)
    out = run_all(on)
    assert out[0]["outcome"] == hs.UNRESOLVED_PARTIAL
    sl = current(on, eid)
    assert sl["status"] == hs.UNRESOLVED_PARTIAL and json.loads(sl["unresolved_ids"])
    cur = hs.current_shortlist(on.db, eid, CUR)
    assert cur["unresolved"] and "need a manual check" in cur["status_plain"]
    assert "no suitable" not in json.dumps(cur).lower() and "no match" not in json.dumps(cur).lower()
    assert all(x["apartment_id"] not in {u["apartment_id"] for u in cur["unresolved"]} for x in cur["why_not"])
    assert hs.EV_REVIEW in alert_kinds(on, eid)


# ------------------------------------------------------------------------------------------------------------- retention
def _fill(on):
    inventory(on.db)
    eid = submit(on, **GROWING)
    hs.confirm_planning(on.db, eid, 1400, "Anna")
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    on.db.execute("UPDATE apartments SET rent_12m=800 WHERE id=2")
    on.db.commit()
    hs.allow_reoffer(on.db, rj["id"], 2, "Anna", "rent down")
    run_all(on)
    on.db.execute("UPDATE apartments SET leased_at=? WHERE id=3", (now_iso(),))
    on.db.commit()
    hs.on_inventory_changed(on.db, "apartment leased", [3])
    # the lead's H7 review rows (homebot_reviews, proposed DDL accepted 9 Nov) are written by the review hook, which this fixture runs
    # without; a row is written directly here so retention is shown to remove review rows too (the hook itself is the lead's test)
    on.db.execute("INSERT INTO homebot_reviews(shortlist_id, enquiry_id, state, version, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  (current(on, eid)["id"], eid, "pending", "test-basis", now_iso(), now_iso()))
    on.db.commit()
    for t in hs.TABLES:
        assert on.db.execute(f"SELECT COUNT(*) FROM {t} WHERE enquiry_id=?", (eid,)).fetchone()[0] > 0, t
    assert alert_kinds(on, eid)
    return eid


def _assert_gone(on, eid):
    for t in hs.TABLES:
        assert on.db.execute(f"SELECT COUNT(*) FROM {t} WHERE enquiry_id=?", (eid,)).fetchone()[0] == 0, t
    assert on.db.execute("SELECT COUNT(*) FROM internal_alert_outbox WHERE record_type='homebot_enquiry' AND record_id=?", (eid,)).fetchone()[0] == 0
    assert on.db.execute("SELECT COUNT(*) FROM homebot_enquiries WHERE id=?", (eid,)).fetchone()[0] == 0


def test_delete_removes_every_matching_row_and_alert_and_nothing_resurrects(on):
    eid = _fill(on)
    assert on.delete(f"/api/homebot/enquiries/{eid}").status_code == 200
    _assert_gone(on, eid)
    assert hs.run_pending(on.db) == []
    hs.on_inventory_changed(on.db, "test", None)
    assert on.db.execute("SELECT COUNT(*) FROM homebot_jobs").fetchone()[0] == 0
    r = post(on, body(**GROWING), "hb-key-0001")     # the submission key is tombstoned: no recreation
    assert r.status_code != 201 or r.json()["id"] != eid


def test_test_data_wipe_and_retention_purge_cover_matching_rows(on):
    inventory(on.db)
    t = submit(on, "hb-key-0001", is_test=True)
    live = submit(on, "hb-key-0002", email="b@example.com")
    run_all(on)
    assert current(on, t)["is_test"] == 1 and current(on, live)["is_test"] == 0
    assert row(on.db.execute("SELECT is_test FROM internal_alert_outbox WHERE record_type='homebot_enquiry' AND record_id=?", (t,)))["is_test"] == 1
    homebot.delete_test_data(on.db)
    _assert_gone(on, t)
    assert current(on, live)
    on.db.execute("UPDATE homebot_enquiries SET last_contact_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", live))
    on.db.commit()
    homebot.purge_expired(on.db, today=date(2026, 11, 8))
    _assert_gone(on, live)


# ---------------------------------------------------------------------------------------------------------- hashes / basis
def test_inventory_hash_covers_every_matching_relevant_column_and_ignores_watermarks(on):
    inventory(on.db)
    h0 = hs.inventory_hash(hs.inventory_rows(on.db))
    on.db.execute("UPDATE apartments SET updated_at='2030-01-01T00:00:00+00:00' WHERE id=1")   # diagnostic only, not a token
    on.db.commit()
    assert hs.inventory_hash(hs.inventory_rows(on.db)) == h0
    seen = {h0}
    for sql in ("UPDATE apartments SET rent_6m=rent_6m+1 WHERE id=1", "UPDATE apartments SET available_from='2026-12-02' WHERE id=1",
                "UPDATE apartments SET bedroom2='1 queen' WHERE id=1", "UPDATE apartments SET feat_balcony=1 WHERE id=1",
                "UPDATE apartments SET leased_at='x' WHERE id=2", "UPDATE apartments SET min_lease_months=3 WHERE id=3", "DELETE FROM apartments WHERE id=4"):
        on.db.execute(sql)
        on.db.commit()
        h = hs.inventory_hash(hs.inventory_rows(on.db))
        assert h not in seen, sql
        seen.add(h)


def test_basis_snapshot_includes_institution_place_and_destination_and_hash_moves_with_them(on):
    inventory(on.db)
    eid = submit(on)
    v1 = hs._current_version(on.db, eid, CUR)
    ppl = json.loads(v1["basis_json"])["people"]
    assert ppl[0]["institution"] and ppl[1]["place"] == "Collins St, CBD" and ppl[2]["primary_destination"] == "Study"
    hs.update_requirements(on.db, eid, {"people": [{}, {"place": "Southbank"}, {}]}, "Anna")
    v2 = hs._current_version(on.db, eid, CUR)
    assert v2["version"] == 2 and v2["basis_hash"] != v1["basis_hash"]
    hs.update_requirements(on.db, eid, {"people": [{}, {}, {"primary_destination": "Work"}]}, "Anna")
    assert hs._current_version(on.db, eid, CUR)["version"] == 3


def test_alert_kinds_are_registered_with_the_homebot_record_type(on):
    for k in (hs.EV_READY, hs.EV_CHANGED, hs.EV_UNAVAILABLE, hs.EV_FAILED, hs.EV_REVIEW):
        assert alerts.EVENTS[k][1] == "homebot_enquiry"
    assert alerts.reference("homebot_enquiry", 7) == "HomeBot-00007"
    r = alerts.render({"kind": hs.EV_READY, "record_type": "homebot_enquiry", "record_id": 7, "created_at": "x", "is_test": 0})
    assert "HomeBot-00007" in r["subject"] and "@" not in r["text"].replace("MateBot AI", "")


def test_matching_status_route(on):
    r = on.get("/api/homebot/matching-status")
    assert r.status_code == 200 and r.json()["enabled"] is True and r.json()["algorithm_version"] == hs.ALGORITHM_VERSION


# ------------------------------------------------------------------------------------- long computations: lease heartbeats
class Clock:
    """A deterministic ISO clock that advances `step` minutes per reading."""
    def __init__(self, start, step):
        self.t, self.step, self.reads = start, step, 0

    def __call__(self):
        self.reads += 1
        self.t = later(self.t, self.step)
        return self.t


def test_a_long_legitimate_computation_keeps_its_lease_through_heartbeats(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    t0 = now_iso()
    clock = Clock(t0, 3)         # every reading is 3 minutes later: the 4-apartment run spans well over STALE_CLAIM_MIN
    rivals = []
    real = hm.build_shortlist

    def patched(req, apartments, today, estimates=None, progress=None):
        def tick(done, total):
            progress(done, total)                       # the owner's heartbeat (every evaluation here: heartbeat_sec=0)
            jb = job(on, eid)
            rival_time = later(jb["locked_at"], hs.STALE_CLAIM_MIN - 1)
            assert datetime.fromisoformat(rival_time) > datetime.fromisoformat(t0) + timedelta(minutes=hs.STALE_CLAIM_MIN)
            rivals.append(hs._claim(on.db, jid, "rival", rival_time))   # a rival worker sees a fresh lease, not a stale one
        return real(req, apartments, today, estimates, progress=tick)
    monkeypatch.setattr(hm, "build_shortlist", patched)
    out = hs.run_job(on.db, jid, clock=clock, heartbeat_sec=0)
    assert out["outcome"] == hs.COMPLETE and rivals == [None] * 4 and clock.reads > 4
    jb = job(on, eid)
    assert jb["status"] == hs.DONE and jb["owner"] is None and current(on, eid)["id"] == out["shortlist_id"]


def test_takeover_after_a_real_stall_old_owner_cannot_publish_and_the_newest_generation_still_runs(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    t0 = now_iso()
    real = hm.build_shortlist
    rival = {}

    def patched(req, apartments, today, estimates=None, progress=None):
        def tick(done, total):
            if done == 2 and not rival:   # the old owner stalled past the threshold: a rival takes over
                rival["claim"] = hs._claim(on.db, jid, "rival", later(job(on, eid)["locked_at"], hs.STALE_CLAIM_MIN + 1))
                assert rival["claim"] and rival["claim"]["owner"] == "rival"
                hs.request_rerun(on.db, eid, "Anna", CUR)   # and a trigger arrives meanwhile: desired generation moves on
            progress(done, total)                           # the old owner's next heartbeat finds the claim gone
        return real(req, apartments, today, estimates, progress=tick)
    monkeypatch.setattr(hm, "build_shortlist", patched)
    out = hs.run_job(on.db, jid, clock=Clock(t0, 0), heartbeat_sec=0)
    assert out == {"job_id": jid, "outcome": "lost_claim"}
    assert shortlists(on, eid) == [] and alert_kinds(on, eid) == []
    jb = job(on, eid)
    assert jb["owner"] == "rival" and jb["status"] == hs.RUNNING and jb["claimed_generation"] == 1 and jb["desired_generation"] == 2
    # the rival completes its claim normally; because desired moved on, the job is queued again and the newest generation runs
    monkeypatch.setattr(hm, "build_shortlist", real)
    e = homebot.get(on.db, eid)
    ver = hs._current_version(on.db, eid, CUR)
    inv = hs.inventory_rows(on.db)
    sl = real(hs._requirements(on.db, e, CUR), inv, date(2026, 11, 8))
    res = hs._store(on.db, rival["claim"], "rival", now_iso(), e, ver, hs.inventory_hash(inv), "basis-1", date(2026, 11, 8), sl)
    assert res["outcome"] == hs.COMPLETE and current(on, eid)["generation"] == 1 and alert_kinds(on, eid) == [hs.EV_READY]
    assert job(on, eid)["status"] == hs.QUEUED
    out = run_all(on)
    assert out[0]["outcome"] == hs.COMPLETE and job(on, eid)["status"] == hs.DONE and job(on, eid)["claimed_generation"] == 2
    assert current(on, eid)["generation"] == 2 and len(shortlists(on, eid)) == 2


def test_heartbeat_is_a_bounded_fenced_write_outside_any_guard(on):
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    jb = hs._claim(on.db, jid, "me", now_iso())
    t1 = later(now_iso(), 5)
    hs._heartbeat(on.db, jb, "me", lambda: t1)
    assert job(on, eid)["locked_at"] == t1 and not on.db.in_transaction
    with pytest.raises(hs.ClaimLost):
        hs._heartbeat(on.db, jb, "someone-else", lambda: t1)
    with pytest.raises(hs.ClaimLost):
        hs._heartbeat(on.db, dict(jb, claimed_generation=99), "me", lambda: t1)
    assert hs._error_kind(hs.ClaimLost("x")) == "unknown"   # if it ever escaped, it would still be a finite category


def test_heartbeat_leaves_unknown_pending_work_untouched_and_never_commits_it(on):
    """Root renewal probe (9 Nov): a write some other caller left pending on the shared connection is NOT committed by the lease
    renewal as a side effect. The renewal refuses (or is skipped by the progress tick); the pending work stays pending for its owner."""
    from server import db as dbm
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    jb = hs._claim(on.db, jid, "me", now_iso())
    locked_before = job(on, eid)["locked_at"]
    before = row(on.db.execute("SELECT updated_at FROM homebot_enquiries WHERE id=?", (eid,)))["updated_at"]
    marker = "synthetic-pending-update"
    on.db.execute("UPDATE homebot_enquiries SET updated_at=? WHERE id=?", (marker, eid))
    try:
        with pytest.raises(dbm.DirtyConnectionAtGuardEntry):
            hs._heartbeat(on.db, jb, "me", lambda: later(now_iso(), 5))
        assert on.db.in_transaction, "the renewal committed (or rolled back) someone else's pending work"
        assert row(on.db.execute("SELECT updated_at FROM homebot_enquiries WHERE id=?", (eid,)))["updated_at"] == marker
        assert job(on, eid)["locked_at"] == locked_before
        # the progress tick skips such a renewal instead of failing the computation; the claim is still ours
        tick = hs._progress(on.db, jb, "me", lambda: later(now_iso(), 5), every_sec=0)
        tick(1, 4)
        assert on.db.in_transaction and job(on, eid)["locked_at"] == locked_before and hs._mine(on.db, jb, "me")
    finally:
        on.db.rollback()
    assert row(on.db.execute("SELECT updated_at FROM homebot_enquiries WHERE id=?", (eid,)))["updated_at"] == before
    # clean again: the renewal runs and commits only itself
    t1 = later(now_iso(), 5)
    hs._heartbeat(on.db, jb, "me", lambda: t1)
    assert job(on, eid)["locked_at"] == t1 and not on.db.in_transaction


def test_heartbeat_waits_a_bounded_time_for_a_rival_row_lock_and_refuses_without_writing(on, monkeypatch):
    """Root renewal probe (9 Nov), SQLite form: an independent connection holds the database write lock; the renewal must come back
    within its bound (RetentionLockTimeout), not wait for the rival. The progress tick treats that as a skipped renewal."""
    import threading
    import time as _time
    from server import db as dbm
    if dbm.is_postgres():
        pytest.skip("SQLite write-lock form of the renewal bound (sqlite3 rival + PRAGMA); PostgreSQL row-lock renewal has separate probes")
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    jb = hs._claim(on.db, jid, "me", now_iso())
    locked_before = job(on, eid)["locked_at"]
    monkeypatch.setattr(hs, "HEARTBEAT_DB_TIMEOUT_MS", 100, raising=False)
    path = row(on.db.execute("PRAGMA database_list"))["file"]
    assert path, "needs a file database for an independent rival connection"
    import sqlite3
    rival = sqlite3.connect(path, timeout=1, isolation_level=None)
    rival.execute("BEGIN IMMEDIATE")
    rival.execute("UPDATE homebot_jobs SET locked_at=locked_at WHERE id=?", (jid,))
    result, done = {}, threading.Event()

    def renewal():
        t0 = _time.monotonic()
        try:
            hs._heartbeat(on.db, jb, "me", lambda: later(now_iso(), 5))
            result["outcome"] = "wrote"
        except Exception as ex:   # noqa: BLE001
            result["outcome"] = type(ex)
        result["elapsed"] = _time.monotonic() - t0
        done.set()
    th = threading.Thread(target=renewal, daemon=True)
    try:
        th.start()
        returned_while_locked = done.wait(1.5)
    finally:
        rival.rollback()
        th.join(timeout=5)
        rival.close()
    assert returned_while_locked and not th.is_alive(), "the renewal waited for the rival's lock"
    assert result["outcome"] is dbm.RetentionLockTimeout and result["elapsed"] < 1.4, result
    assert job(on, eid)["locked_at"] == locked_before and not on.db.in_transaction
    # the connection is clean and usable afterwards; the busy timeout is back to what it was
    assert row(on.db.execute("PRAGMA busy_timeout"))["timeout"] != 100
    t1 = later(now_iso(), 5)
    hs._heartbeat(on.db, jb, "me", lambda: t1)
    assert job(on, eid)["locked_at"] == t1


def test_bounded_write_refuses_a_non_positive_bound(on):
    """lock_timeout 0 would DISABLE the PostgreSQL limit: a non-positive bound is refused, never clamped silently."""
    from server import db as dbm
    for bad in (0, -1):
        with pytest.raises(ValueError):
            dbm.bounded_write(on.db, "t", bad)
    assert dbm.bounded_write(on.db, "t", 1).timeout_ms == 1


def test_heartbeat_inside_a_guard_is_nested_and_never_commits_the_step(on):
    """A renewal issued inside an active retention guard belongs to that step: no commit of its own; a lost fence poisons the step."""
    from server import db as dbm
    inventory(on.db)
    eid = submit(on)
    jid = job(on, eid)["id"]
    jb = hs._claim(on.db, jid, "me", now_iso())
    t1 = later(now_iso(), 5)
    with dbm.retention_guard(on.db, "test step"):
        hs._heartbeat(on.db, jb, "me", lambda: t1)
        assert on.db.in_transaction
    assert job(on, eid)["locked_at"] == t1
    with pytest.raises(hs.ClaimLost):
        with dbm.retention_guard(on.db, "test step 2"):
            on.db.execute("UPDATE homebot_jobs SET attempts=attempts+10 WHERE id=?", (jid,))
            hs._heartbeat(on.db, jb, "rival", lambda: later(t1, 1))
    assert job(on, eid)["attempts"] == jb["attempts"] and job(on, eid)["locked_at"] == t1   # the whole step rolled back


# ------------------------------------------------------------------ basis freshness (root basis-freshness probe, 9 Nov; overlay R)
from contextlib import contextmanager


def _at_guard(monkeypatch, label, action):
    """Fire `action` (a real, validated edit on the same connection) the first time the named guard is entered — i.e. after
    anything the mutator read BEFORE the guard, and before anything it writes inside it."""
    original = hs.retention_guard
    fired = []

    @contextmanager
    def interpose(c, lbl):
        if lbl == label and not fired:
            fired.append(True)
            action()
        with original(c, lbl):
            yield
    monkeypatch.setattr(hs, "retention_guard", interpose)
    return fired


def _reoffer_after_rent_drop(on, eid):
    run_all(on)   # the decline is recorded against a computed offer (its inventory snapshot is the GP14 evidence; without one no re-offer basis exists)
    rid = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="A")["id"]
    with hs.transaction(on.db):
        on.db.execute("UPDATE apartments SET rent_12m=rent_12m-100 WHERE id=2")   # a material change for unit 2 only
    return hs.allow_reoffer(on.db, rid, 2, "Earlier", "rent came down")


def _basis_budget(on, eid, sc=CUR):
    return json.loads(hs._current_version(on.db, eid, sc)["basis_json"])["whole_apartment_max"]


@pytest.mark.parametrize("label, op", [
    ("homebot version ensure", lambda on, eid: hs.run_job(on.db, job(on, eid)["id"])),
    ("homebot rerun request", lambda on, eid: hs.request_rerun(on.db, eid, "Earlier")),
    ("homebot rejection record", lambda on, eid: hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Earlier")),
    ("homebot rejection reopen", lambda on, eid: hs.reopen_by_customer(on.db, hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="A")["id"], "Earlier", "asked")),
    ("homebot reoffer allowance", lambda on, eid: _reoffer_after_rent_drop(on, eid)),
])
def test_every_version_writing_mutator_captures_the_enquiry_under_the_guard_so_a_concurrent_edit_is_never_undone(on, monkeypatch, label, op):
    inventory(on.db)
    eid = submit(on)
    fired = _at_guard(monkeypatch, label, lambda: hs.update_requirements(on.db, eid, {"budget_total_weekly": 100}, "Later editor"))
    op(on, eid)
    assert fired and homebot.get(on.db, eid)["budget_total_weekly"] == 100
    assert float(_basis_budget(on, eid)) == 100.0   # the newest version carries the committed edit, not the pre-guard capture


def test_requirement_patches_merge_under_the_guard_so_two_editors_both_land(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    fired = _at_guard(monkeypatch, "homebot requirements edit", lambda: hs.update_requirements(on.db, eid, {"budget_total_weekly": 100}, "Budget editor"))
    hs.update_requirements(on.db, eid, {"area": "South Yarra"}, "Area editor")
    e = homebot.get(on.db, eid)
    assert fired and e["area"] == "South Yarra" and e["budget_total_weekly"] == 100
    assert float(_basis_budget(on, eid)) == 100.0


def test_planning_confirmation_records_the_basis_as_it_is_under_the_guard(on, monkeypatch):
    inventory(on.db)
    eid = submit(on, **GROWING)
    fired = _at_guard(monkeypatch, "homebot planning confirm", lambda: hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Later editor"))
    st = hs.confirm_planning(on.db, eid, 1500, "Anna")
    assert fired and st["applies"] is True   # confirmed against the edited record, so it still applies after the edit
    assert hs.planning_state(on.db, homebot.get(on.db, eid))["applies"] is True


def test_the_computation_runs_on_exactly_the_stored_versions_basis(on, monkeypatch):
    """Capture, inventory snapshot, version and requirements are taken in one guarded step; a decline recorded right after that
    step cannot leak into the computation, and the publication is marked out of date rather than current."""
    inventory(on.db)
    eid = submit(on)
    seen = {}
    build = hm.build_shortlist

    def spy(req, inv, today, estimates, progress=None):
        seen["calls"] = seen.get("calls", 0) + 1
        if "basis" not in seen:                                                             # one-shot race injection: the decline
            seen["basis"] = hs._hash(req.as_dict())                                         # lands after the FIRST capture only
            hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Anna")
        return build(req, inv, today, estimates, progress=progress)
    monkeypatch.setattr(hm, "build_shortlist", spy)
    res = hs.run_job(on.db, job(on, eid)["id"])
    assert res["outcome"] == "superseded" and current(on, eid) is None
    stored = row(on.db.execute("SELECT requirement_version_id FROM homebot_shortlists WHERE id=?", (res["shortlist_id"],)))
    ver = row(on.db.execute("SELECT basis_hash, basis_json FROM homebot_requirement_versions WHERE id=?", (stored["requirement_version_id"],)))
    assert ver["basis_hash"] == seen["basis"] and json.loads(ver["basis_json"])["rejected_apartment_ids"] == []
    assert run_all(on)[0]["outcome"] == hs.COMPLETE and json.loads(current(on, eid)["listed_ids"]) and 1 not in json.loads(current(on, eid)["listed_ids"])
    assert seen["calls"] == 2                                                                # the follow-up ran the real builder on the new basis


def test_invalidated_planning_marks_the_final_size_options_out_of_date_in_plain_words(on):
    inventory(on.db)
    eid = submit(on, **GROWING)
    hs.confirm_planning(on.db, eid, 1400, "Anna")
    hs.run_job(on.db, job(on, eid, FIN)["id"])
    assert hs.current_shortlist(on.db, eid, FIN)["stale"] is None
    hs.update_requirements(on.db, eid, {"headcount_target": 4}, "Anna")
    out = hs.current_shortlist(on.db, eid, FIN)
    assert out["stale"].startswith("Out of date: the planning budget these options were prepared under no longer applies")
    # confirming again makes a new version and a fresh run; the old options stay out of date until it lands
    hs.confirm_planning(on.db, eid, 1800, "Anna")
    assert hs.current_shortlist(on.db, eid, FIN)["stale"]
    run_all(on)
    assert hs.current_shortlist(on.db, eid, FIN)["stale"] is None


def test_planning_invalidated_during_the_final_size_computation_is_never_published_as_current(on, monkeypatch):
    inventory(on.db)
    eid = submit(on, **GROWING)
    hs.confirm_planning(on.db, eid, 1400, "Anna")
    build = hm.build_shortlist

    def invalidate_then_build(*a, **kw):
        res = build(*a, **kw)
        hs.update_requirements(on.db, eid, {"headcount_target": 4}, "Anna")
        return res
    monkeypatch.setattr(hm, "build_shortlist", invalidate_then_build)
    res = hs.run_job(on.db, job(on, eid, FIN)["id"])
    assert res["outcome"] == "superseded" and current(on, eid, FIN) is None
    assert alert_kinds(on, eid) == []
    # the requeued run finds no applicable planning budget and is cancelled, leaving nothing current
    assert [r["outcome"] for r in hs.run_pending(on.db) if r["job_id"] == job(on, eid, FIN)["id"]] == ["cancelled"]
    assert current(on, eid, FIN) is None


# ------------------------------------------------------------------ GP14 re-offer freshness (root re-offer probe, 9 Nov; overlay S)
@pytest.mark.parametrize("scope", ["apartment", "building"])
def test_reoffer_basis_and_rejection_status_are_evaluated_inside_the_guard(on, monkeypatch, scope):
    """A material change seen before the guard that is reverted at guard entry authorises nothing; the rejection stays active with
    its events untouched. A reopening that lands meanwhile is likewise not repeated."""
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope=scope, apartment_id=1, staff="Anna")
    with hs.transaction(on.db):
        on.db.execute("UPDATE apartments SET rent_12m=800 WHERE id=1")
    assert hs.material_change(on.db, rj["id"], 1)
    guard = hs.retention_guard
    fired = _at_guard(monkeypatch, "homebot reoffer allowance", lambda: on.db.execute("UPDATE apartments SET rent_12m=900 WHERE id=1") and on.db.commit())
    with pytest.raises(ValidationError):
        hs.allow_reoffer(on.db, rj["id"], 1, "Anna", "price came down")
    assert fired and hs.rejection(on.db, rj["id"])["status"] == "active" and hs.rejection(on.db, rj["id"])["events"] == rj["events"]
    assert row(on.db.execute("SELECT COUNT(*) AS n FROM homebot_rejection_events WHERE rejection_id=? AND event='reoffer_allowed'", (rj["id"],)))["n"] == 0
    # the stored change basis is the one evaluated under the guard
    with hs.transaction(on.db):
        on.db.execute("UPDATE apartments SET rent_12m=850 WHERE id=1")
    monkeypatch.setattr(hs, "retention_guard", guard)
    out = hs.allow_reoffer(on.db, rj["id"], 1, "Anna", "price came down")
    ev = [x for x in out["events"] if x["event"] == "reoffer_allowed"][0]
    assert ev["change_basis"] == [{"field": "rent_12m", "was": 900, "now": 850}]


def test_customer_reopening_rechecks_the_status_inside_the_guard(on, monkeypatch):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Anna")
    fired = _at_guard(monkeypatch, "homebot rejection reopen", lambda: hs.reopen_by_customer(on.db, rj["id"], "Other staff", "asked first"))
    with pytest.raises(ValidationError):
        hs.reopen_by_customer(on.db, rj["id"], "Anna", "asked again")
    out = hs.rejection(on.db, rj["id"])
    assert fired and out["status"] == "reopened" and out["reopened_by"] == "Other staff"
    assert [x["event"] for x in out["events"]] == ["recorded", "reopened_customer"]


def test_freshness_is_the_public_name_of_the_read_side_basis_check(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sl = row(on.db.execute("SELECT * FROM homebot_shortlists WHERE enquiry_id=? AND is_current=1", (eid,)))
    assert hs.freshness(on.db, sl) is None
    hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna")
    assert hs.freshness(on.db, sl) and hs.freshness(on.db, sl).startswith("Out of date")


def test_freshness_check_never_re_enters_the_review_state(on, monkeypatch):
    """Lead's H7/H14 finding (9 Nov): the review module calls `freshness` from inside `homebot_review_state`, which `homebot.get`
    reaches through the matching note. The freshness read therefore takes the record without the note (`homebot.get_record`),
    so a review-state hook that itself asks for freshness is answered without recursion — and the state is still reported."""
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    from server import ai_queue
    depth = {"n": 0, "max": 0}

    def state(c, sid):
        depth["n"] += 1
        depth["max"] = max(depth["max"], depth["n"])
        try:
            sl = row(c.execute("SELECT * FROM homebot_shortlists WHERE id=?", (sid,)))
            fresh = hs.freshness(c, sl)   # what the lead's module does before it trusts a stored review
            return {"eligible": True, "reviewed": {"verdict": "ok", "fresh": fresh is None}}
        finally:
            depth["n"] -= 1
    monkeypatch.setattr(ai_queue, "homebot_review_state", state, raising=False)
    e = homebot.get(on.db, eid)   # get -> matching_note -> _review_state -> hook -> freshness -> get_record (no second matching note)
    assert e["matching"]["review_state"] == "reviewed" and depth["max"] == 1
    cur = hs.current_shortlist(on.db, eid, CUR)
    assert cur["review"] == {"state": "reviewed", "detail": {"verdict": "ok", "fresh": True}}
    assert "matching" not in homebot.get_record(on.db, eid) and "matching" in e


# ------------------------------------------------------------------ GP14 exact rejection basis (root rejection-basis probe, 9 Nov; overlay U)
def test_shortlist_carries_the_inventory_snapshot_it_was_computed_from(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sl = current(on, eid)
    res = json.loads(sl["result_json"])
    snap = res["inventory_snapshot"]
    assert hs.inventory_hash(snap) == sl["inventory_hash"] and {r["id"] for r in snap} == {1, 2, 3, 4}
    assert set(snap[0]) == set(hs.INVENTORY_COLUMNS)


def test_rejection_without_a_computed_offer_is_recorded_but_no_material_change_can_be_measured(on):
    """Fail closed: a decline recorded before any shortlist exists has no inventory snapshot to compare with. The rejection stands
    (it blocks the apartment as usual); a re-offer on 'material change' is refused with the fixed sentence; only the customer's own
    request (route 1) reopens it. Nothing is invented from the live table."""
    inventory(on.db)
    eid = submit(on)
    rj = hs.record_rejection(on.db, eid, scope="building", apartment_id=1, staff="Anna")
    assert rj["offered_basis"]["snapshot_missing"] is True and rj["offered_basis"]["apartment"] is None and rj["status"] == "active"
    with hs.transaction(on.db):
        on.db.execute("UPDATE apartments SET rent_12m=rent_12m-100 WHERE id=2")
    with pytest.raises(ValidationError) as ei:
        hs.material_change(on.db, rj["id"], 2)
    assert ei.value.errors["apartment_id"] == hs.NO_BASIS
    with pytest.raises(ValidationError):
        hs.allow_reoffer(on.db, rj["id"], 2, "Anna", "rent came down")
    assert hs.rejection(on.db, rj["id"])["status"] == "active" and len(hs.rejection(on.db, rj["id"])["events"]) == 1
    r = on.get(f"/api/homebot/rejections/{rj['id']}/material-change", params={"apartment_id": 2})
    assert r.status_code == 422 and r.json()["errors"]["apartment_id"] == hs.NO_BASIS
    out = hs.reopen_by_customer(on.db, rj["id"], "Anna", "customer asked again")
    assert out["status"] == "reopened"


def test_legacy_shortlist_without_a_matching_snapshot_is_not_evidence(on):
    inventory(on.db)
    eid = submit(on)
    run_all(on)
    sl = current(on, eid)
    res = json.loads(sl["result_json"])
    res["inventory_snapshot"][0]["rent_12m"] = 1   # a snapshot that does not hash to the row's inventory_hash is ignored
    with hs.transaction(on.db):
        on.db.execute("UPDATE homebot_shortlists SET result_json=? WHERE id=?", (json.dumps(res), sl["id"]))
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=1, staff="Anna", shortlist_id=sl["id"])
    assert rj["offered_basis"]["snapshot_missing"] is True
    with pytest.raises(ValidationError):
        hs.material_change(on.db, rj["id"], 1)


def test_final_size_decline_is_measured_against_the_final_size_scenario_and_fails_closed_when_planning_lapses(on):
    inventory(on.db)
    eid = submit(on, **GROWING)
    hs.confirm_planning(on.db, eid, 1600, "Anna")
    run_all(on)
    declined = current(on, eid, FIN)
    rj = hs.record_rejection(on.db, eid, scope="apartment", apartment_id=3, staff="Anna", shortlist_id=declined["id"])
    assert rj["offered_basis"]["scenario"] == FIN
    assert hs.material_change(on.db, rj["id"], 3) == []          # same scenario, nothing changed: not a material change
    # the party's stated budget changes: the planning confirmation lapses, so the final-size requirements no longer apply
    hs.update_requirements(on.db, eid, {"budget_total_weekly": 1100}, "Anna")
    assert hs.planning_state(on.db, homebot.get(on.db, eid))["applies"] is False
    with pytest.raises(ValidationError) as ei:
        hs.material_change(on.db, rj["id"], 3)
    assert "planning budget" in ei.value.errors["apartment_id"]
    with pytest.raises(ValidationError):
        hs.allow_reoffer(on.db, rj["id"], 3, "Anna", "budget changed")
    assert hs.rejection(on.db, rj["id"])["status"] == "active"
