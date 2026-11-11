"""G-SL2 (11 Nov 2026): a stock follow-up that runs AFTER the apartment ROW is gone (the delete route records the transition and
deletes the row in one step; the cascade removes the option rows and sets group pointers NULL) must still decide "history" for a
group whose current member declined that unit (apartment scope: the record's own `recorded` event id) or its building (building
scope: the building identity captured on the transition WITH its pending work), and must keep today's loss handling for every
effective loss. The captured key is pending-decision evidence only: NULL once no work row of the transition is pending. Every
group deletion goes through stock_lifecycle.delete_groups so pending work closes with the group in the same step.
Not a launch-readiness claim; counts here are the test's own assertions."""
from __future__ import annotations

import os
import re

import pytest

from server import apartment_rejections as ar
from server import db as dbm
from server import matching
from server import stock_lifecycle as sl
from server.db import now_iso, retention_guard, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_gp14_matebot_rejections import (STAFF, _alerts_for, _apt_same_building, _five_groups, _g, _group_of, _listed,  # noqa: F401
                                                _members_of, _only_this_stock, _work_for)
from tests.test_group_coverage_completion import _primaries, _solo_pool
from tests.test_launch_neutral import _apt


def _tr(db, aid=None, tid=None):
    if tid is not None:
        return row(db.execute("SELECT * FROM stock_transitions WHERE id=?", (tid,)))
    return row(db.execute("SELECT * FROM stock_transitions WHERE apartment_id=? ORDER BY id DESC LIMIT 1", (aid,)))


def _pending(db, tid):
    return rows(db.execute("SELECT id, group_id, role FROM offer_reconcile_work WHERE transition_id=? AND status='pending' ORDER BY id", (tid,)))


def _role(db, tid, gid):
    return row(db.execute("SELECT role FROM offer_reconcile_work WHERE transition_id=? AND group_id=?", (tid, gid)))["role"]


def _owners(db, aid):
    """Independent full owner census, queried BEFORE the mutation under test and never derived from a transition result: live
    groups (status not in LIVE_EXCLUDED) whose pointer is on `aid` or that hold an option row for it - primaries AND the
    non-primary alternatives a full run also creates."""
    ph = ",".join("?" * len(sl.LIVE_EXCLUDED))
    cur = {int(r["id"]) for r in rows(db.execute(f"SELECT id FROM groups WHERE apartment_id=? AND status NOT IN ({ph})", (aid, *sl.LIVE_EXCLUDED)))}
    alt = {int(r["id"]) for r in rows(db.execute(f"""SELECT DISTINCT g.id FROM group_apartments ga JOIN groups g ON g.id=ga.group_id
                                                     WHERE ga.apartment_id=? AND g.status NOT IN ({ph})""", (aid, *sl.LIVE_EXCLUDED)))}
    return cur | alt


def _non_primary_proposed(db, gids):
    return all(_g(db, gid)["is_primary"] == 0 and _g(db, gid)["status"] == "Proposed" for gid in gids)


def _close_unrelated(db, tid, keep):
    """Controlled closure of the rows that are not under test: every pending row of `tid` whose group is not in `keep` is completed
    one real step at a time (the ordinary per-row step, no fault). Returns the closed work ids; every one must have left 'pending'."""
    closed = []
    for w in _pending(db, tid):
        if w["group_id"] in keep:
            continue
        sl.reconcile_work(db, w["id"])
        assert row(db.execute("SELECT status FROM offer_reconcile_work WHERE id=?", (w["id"],)))["status"] != "pending", w
        closed.append(w["id"])
    assert {w["group_id"] for w in _pending(db, tid)} <= set(keep)
    return closed


def _spy_delete_groups(monkeypatch):
    """Observes stock_lifecycle.delete_groups; forwards the actual arguments and returns the actual result unchanged."""
    calls = []
    real = sl.delete_groups

    def seen(c, group_ids):
        ids = [int(i) for i in group_ids]
        out = real(c, group_ids)
        calls.append((tuple(ids), out))
        return out
    monkeypatch.setattr(sl, "delete_groups", seen)
    return calls


def _dangling_work(db):
    return rows(db.execute("SELECT w.id FROM offer_reconcile_work w LEFT JOIN groups g ON g.id=w.group_id WHERE g.id IS NULL"))


def _invariant(db):
    """Scoped invariant: a transition written by the new writer carries a key iff it has pending work; legacy rows (created_by
    'upgrade') are NULL always."""
    for t in rows(db.execute("SELECT id, building_key, created_by FROM stock_transitions ORDER BY id")):
        pending = bool(_pending(db, t["id"]))
        if t["created_by"] == "upgrade":
            assert t["building_key"] is None, t
        else:
            assert (t["building_key"] is not None) == pending, (t, pending)


def _settle(db, gids):
    from server import ai_queue
    ai_queue.process_pending(db)
    for gid in gids:
        g = _g(db, gid)
        assert g["ai_status"] == "ok" and g["review_verdict"] == "approve", (gid, g["ai_status"], g["ai_error"])


@pytest.fixture
def hold_followup(monkeypatch):
    """Fault injection on the follow-up ONLY (pending-work simulation, not a rollback proof): the transition and the deletion are
    committed by the product route, every follow-up step is left pending; `release()` restores the real step."""
    real = sl.reconcile_work

    def injected(c, work_id, staff=None):
        raise RuntimeError("injected: follow-up left pending")
    monkeypatch.setattr(sl, "reconcile_work", injected)

    def release():
        monkeypatch.setattr(sl, "reconcile_work", real)
    return release


# -------------------------------------------------------------------------------------- 1 / 4 / C2: the delete route, five groups

def test_delete_route_declined_unit_is_history_for_the_declining_group_and_a_loss_for_every_effective_offer(client, monkeypatch, hold_followup):
    """x is DELETED through the product route. G: x declined (apartment scope, group context) -> history: no note/flag/alert/refresh,
    work 'unchanged', evidence hash unchanged, review current. H: effective alternative -> trimmed with today's handling. K: effective
    current -> moved + alert, work role 'current'. K2: x current and `other` declined -> held with no offer, alert. L (legacy):
    decline without group context + pointer left on x -> role 'current' -> judged and moved + alert (a current row with no apartment
    row is never history). The apartment-scope decision after the row is gone comes from the record's `recorded` event; the key is
    captured on the transition while work is pending and NULL after the last step."""
    from server import ai_queue
    db = appdb(client)
    x, other, gids = _five_groups(db, monkeypatch)
    G, H, K, K2, L = gids
    ar.record(db, _members_of(db, G)[0], "apartment", x, STAFF, group_id=G)
    ar.record(db, _members_of(db, K2)[0], "apartment", other, STAFF, group_id=K2)
    ar.record(db, _members_of(db, L)[0], "apartment", x, STAFF)
    _settle(db, gids)
    matching.offer_apartment(db, H, other, STAFF)
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (x, L)); db.commit()   # labelled legacy-state setup (inc2b shape), not a product path
    assert int(_g(db, G)["apartment_id"]) == other and set(_listed(db, G)) == {x, other}
    assert int(_g(db, H)["apartment_id"]) == other and int(_g(db, K)["apartment_id"]) == x
    assert int(_g(db, K2)["apartment_id"]) == x and int(_g(db, L)["apartment_id"]) == x
    assert all(_work_for(db, gid) == [] for gid in gids)
    h0 = _g(db, G)["evidence_hash"]
    assert h0 and ai_queue.is_ready(db, _g(db, G))["ready"]
    x_row = row(db.execute("SELECT * FROM apartments WHERE id=?", (x,)))
    expected_key = ar.building_identity(x_row)
    assert expected_key
    alerts_before = {gid: len(_alerts_for(db, gid)) for gid in gids}
    rec_G = ar.records_for(db, _members_of(db, G)[0])[0]
    owners = _owners(db, x)                                                          # full census BEFORE the delete (independent query)
    assert set(gids) < owners and _non_primary_proposed(db, owners - set(gids))      # the run's alternatives own the unit too

    r = client.delete(f"/api/apartments/{x}")
    assert r.status_code == 200, r.text
    assert r.json()["groups_rechecked"]["status"] == "partial"                      # every follow-up left pending by the injected fault
    assert row(db.execute("SELECT 1 FROM apartments WHERE id=?", (x,))) is None
    t = _tr(db, aid=x)
    assert t["cause"] == "deleted" and t["building_key"] == expected_key            # captured WITH the pending work
    assert {w["group_id"] for w in _pending(db, t["id"])} == owners                   # exact: every owner, primaries and alternatives
    assert _role(db, t["id"], G) == "alternative" and _role(db, t["id"], H) == "alternative"
    assert _role(db, t["id"], K) == "current" and _role(db, t["id"], K2) == "current" and _role(db, t["id"], L) == "current"
    # the cascade: option rows gone, pointers on x NULL, the apartment-scope record's apartment_id NULL - the event keeps the id
    assert all(x not in _listed(db, gid) for gid in gids)
    assert all(_g(db, gid)["apartment_id"] is None for gid in (K, K2, L)) and int(_g(db, G)["apartment_id"]) == other
    assert row(db.execute("SELECT apartment_id FROM apartment_rejections WHERE id=?", (rec_G["id"],)))["apartment_id"] is None
    assert ar.records_for(db, _members_of(db, G)[0]) == []                          # display/matcher view: nothing left to exclude
    assert ar.records_for_decision(db, _members_of(db, G)[0]) == [("apartment", str(x), ())]   # decision view: the recorded id
    assert ar.group_declined_absent_row(db, G, x, expected_key) and not ar.group_declined_absent_row(db, H, x, expected_key)
    _invariant(db)

    hold_followup()
    closed = _close_unrelated(db, t["id"], keep=set(gids))                           # every alternative owner's row, one real step each
    assert len(closed) == len(owners - set(gids)) and {w["group_id"] for w in _pending(db, t["id"])} == set(gids)
    assert _tr(db, tid=t["id"])["building_key"] == expected_key                      # five rows still pending -> the key lives
    _invariant(db)
    sl.run_pending(db, transition_id=t["id"])                                        # the ordinary retry, row absent
    assert _pending(db, t["id"]) == [] and _tr(db, tid=t["id"])["building_key"] is None   # closed -> key cleared in the last step
    assert _dangling_work(db) == []
    _invariant(db)
    g = _g(db, G)
    assert [w["outcome"] for w in _work_for(db, G)] == ["unchanged"]
    assert int(g["apartment_id"]) == other and _listed(db, G) == [other]
    assert (g["rerank_note"], g["flag_kind"], g["reranked_at"]) == (None, None, None)
    assert len(_alerts_for(db, G)) == alerts_before[G] and not g["ai_refresh"] and g["ai_status"] == "ok"
    assert g["evidence_hash"] == h0 and matching.group_evidence(db, G)[0] == h0
    assert ai_queue.evidence_currency(db, g)["current"] and ai_queue.is_ready(db, g)["ready"], ai_queue.is_ready(db, g)["reasons"]
    h = _g(db, H)                                                                    # effective alternative: today's loss handling (row already gone)
    assert [w["status"] for w in _work_for(db, H)] == ["done"] and int(h["apartment_id"]) == other
    assert h["flag_kind"] == "stock" and "was deleted from the apartment list" in (h["rerank_note"] or "") and len(_alerts_for(db, H)) == alerts_before[H] + 1
    k = _g(db, K)                                                                    # effective current: moved + alert
    assert [w["outcome"] for w in _work_for(db, K)] == ["moved"] and int(k["apartment_id"]) == other
    assert k["flag_kind"] == "stock" and len(_alerts_for(db, K)) == alerts_before[K] + 1
    k2 = _g(db, K2)                                                                  # current lost, the only other row declined -> held
    assert [w["outcome"] for w in _work_for(db, K2)] == ["no_apartment"] and k2["apartment_id"] is None and _listed(db, K2) == []
    assert len(_alerts_for(db, K2)) == alerts_before[K2] + 1
    l_ = _g(db, L)                                                                   # legacy current on a declined, now deleted unit: judged, not history
    assert [w["outcome"] for w in _work_for(db, L)] == ["moved"] and int(l_["apartment_id"]) == other
    assert l_["flag_kind"] == "stock" and len(_alerts_for(db, L)) == alerts_before[L] + 1


# ------------------------------------------------------------------------------------- 2 / 7: building scope from the captured key

def _building_world(db, monkeypatch):
    """Three listed units - a and b in one building, c elsewhere - and four solos -> two real groups listing all three."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    b = _apt_same_building(db, "Unit 2 / 1 Neutral Street")
    c = _apt(db, address="7 Other Road")
    _only_this_stock(db, (a["id"], b["id"], c["id"]))
    _solo_pool(db, 4)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    gids = sorted({r["group_id"] for r in _primaries(db)})
    assert len(gids) == 2, gids
    for gid in gids:
        assert set(_listed(db, gid)) == {a["id"], b["id"], c["id"]}, "the fixture needs all three units listed for both groups"
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    return a, b, c, gids


def test_deleted_sibling_of_a_declined_building_is_history_from_the_captured_key_which_lives_while_any_row_is_pending(db, monkeypatch, hold_followup):
    """P declines the BUILDING of a (group context): a and b are excluded for P, pointer on c. Q keeps everything. Unit b is deleted
    (route-equivalent: transition + row deletion in one guarded step, follow-up pending by the injected fault). The key is the
    building identity, present while Q's row is still pending after P's closed, NULL after the last. P: history (no loss); Q: an
    effective alternative lost -> today's handling."""
    from server import ai_queue
    a, b, c, gids = _building_world(db, monkeypatch)
    P, Q = gids
    ar.record(db, _members_of(db, P)[0], "building", a["id"], STAFF, group_id=P)
    _settle(db, gids)
    assert int(_g(db, P)["apartment_id"]) == c["id"]
    assert {int(o["apartment_id"]) for o in matching.eligible_offers(db, P)} == {c["id"]}
    hP = _g(db, P)["evidence_hash"]
    key = ar.building_identity(b)
    assert key == ar.building_identity(a) and key
    owners = _owners(db, b["id"])                                                    # independent census before the delete
    assert {P, Q} < owners and _non_primary_proposed(db, owners - {P, Q})
    with retention_guard(db, label="apartment delete"):
        tr = sl.record_transition(db, b["id"], "deleted", STAFF)
        db.execute("DELETE FROM apartments WHERE id=?", (b["id"],))
    tid = tr["transition_id"]
    assert set(tr["group_ids"]) == owners and {w["group_id"] for w in _pending(db, tid)} == owners
    assert _tr(db, tid=tid)["building_key"] == key
    assert ar.records_for_decision(db, _members_of(db, P)[0]) == [("building", key, ())]
    assert ar.group_declined_absent_row(db, P, b["id"], key) and not ar.group_declined_absent_row(db, P, b["id"], "")   # '' never matches
    assert not ar.group_declined_absent_row(db, Q, b["id"], key)
    hold_followup()
    wid_P = [w["id"] for w in _pending(db, tid) if w["group_id"] == P][0]
    sl.reconcile_work(db, wid_P)                                                     # P's step alone
    assert [w["outcome"] for w in _work_for(db, P)] == ["unchanged"]
    assert _tr(db, tid=tid)["building_key"] == key                                   # Q's row still pending -> the key stays
    _invariant(db)
    p = _g(db, P)
    assert int(p["apartment_id"]) == c["id"] and (p["rerank_note"], p["flag_kind"]) == (None, None) and _alerts_for(db, P) == []
    assert p["evidence_hash"] == hP and ai_queue.evidence_currency(db, p)["current"]
    closed = _close_unrelated(db, tid, keep={Q})                                     # the alternatives' rows, one real step each
    assert len(closed) == len(owners - {P, Q}) and {w["group_id"] for w in _pending(db, tid)} == {Q}
    assert _tr(db, tid=tid)["building_key"] == key                                   # Q's row is the last one pending -> key still there
    _invariant(db)
    sl.run_pending(db, transition_id=tid)
    assert _pending(db, tid) == [] and _tr(db, tid=tid)["building_key"] is None       # the last closure clears it
    _invariant(db)
    q = _g(db, Q)
    assert [w["status"] for w in _work_for(db, Q)] == ["done"] and q["flag_kind"] == "stock" and "was deleted" in (q["rerank_note"] or "") and _alerts_for(db, Q)


def test_legacy_null_key_means_building_unknown_but_apartment_scope_still_decides_from_the_event(db, monkeypatch, hold_followup):
    """Test 3: a transition row with NO captured key (labelled legacy-state setup: the column is NULLed after capture). P's building
    record cannot be matched -> today's loss path and wording for P. R's APARTMENT-scope record on the deleted unit still decides
    history from the recorded event id under the same legacy row."""
    a, b, c, gids = _building_world(db, monkeypatch)
    P, R = gids
    ar.record(db, _members_of(db, P)[0], "building", a["id"], STAFF, group_id=P)
    ar.record(db, _members_of(db, R)[0], "apartment", b["id"], STAFF, group_id=R)
    _settle(db, gids)
    hR = _g(db, R)["evidence_hash"]
    with retention_guard(db, label="apartment delete"):
        tr = sl.record_transition(db, b["id"], "deleted", STAFF)
        db.execute("DELETE FROM apartments WHERE id=?", (b["id"],))
    tid = tr["transition_id"]
    assert _tr(db, tid=tid)["building_key"]
    db.execute("UPDATE stock_transitions SET building_key=NULL WHERE id=?", (tid,)); db.commit()   # legacy-state setup: key never captured
    hold_followup()
    sl.run_pending(db, transition_id=tid)
    p, r_ = _g(db, P), _g(db, R)
    assert [w["status"] for w in _work_for(db, P)] == ["done"]                                      # building UNKNOWN -> loss path as today
    assert p["flag_kind"] == "stock" and "was deleted from the apartment list" in (p["rerank_note"] or "") and _alerts_for(db, P)
    assert [w["outcome"] for w in _work_for(db, R)] == ["unchanged"]                                # apartment scope by event id
    assert (r_["rerank_note"], r_["flag_kind"]) == (None, None) and _alerts_for(db, R) == [] and r_["evidence_hash"] == hR
    assert _tr(db, tid=tid)["building_key"] is None
    _invariant(db)


def test_empty_captured_key_for_a_unit_without_building_identity_matches_no_building_record(db, monkeypatch, hold_followup):
    """Test 3b (Neutral F3): a unit with NO building identity captures '' (not NULL) with its pending work; '' never matches any
    building record (a record's key is never empty); an apartment-scope record on the unit still decides by event id. '' is cleared
    to NULL only by closure."""
    from server import ai, ai_queue
    from tests.test_t6_fail_closed import _reply_builder, _good
    a = _apt(db, address="Unit 1 / 1 Neutral Street")
    n = _apt_same_building(db, "", suburb="Carlton")                                 # address-less row: no building identity
    c = _apt(db, address="7 Other Road")
    assert ar.building_identity(n) == ""
    _only_this_stock(db, (a["id"], n["id"], c["id"]))
    _solo_pool(db, 4)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    gids = sorted({r["group_id"] for r in _primaries(db)})
    assert len(gids) == 2 and all(n["id"] in _listed(db, gid) for gid in gids), "the fixture needs the address-less unit listed for both groups"
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    B, A = gids
    ar.record(db, _members_of(db, B)[0], "building", a["id"], STAFF, group_id=B)      # a building record elsewhere: must never match ''
    ar.record(db, _members_of(db, A)[0], "apartment", n["id"], STAFF, group_id=A)     # apartment scope on the address-less unit
    _settle(db, gids)
    hA = _g(db, A)["evidence_hash"]
    with retention_guard(db, label="apartment delete"):
        tr = sl.record_transition(db, n["id"], "deleted", STAFF)
        db.execute("DELETE FROM apartments WHERE id=?", (n["id"],))
    tid = tr["transition_id"]
    assert _tr(db, tid=tid)["building_key"] == ""                                     # captured, empty: not NULL
    _invariant(db)
    assert not ar.group_declined_absent_row(db, B, n["id"], "") and ar.group_declined_absent_row(db, A, n["id"], "")
    hold_followup()
    sl.run_pending(db, transition_id=tid)
    assert [w["outcome"] for w in _work_for(db, A)] == ["unchanged"] and _g(db, A)["evidence_hash"] == hA and _alerts_for(db, A) == []
    assert [w["status"] for w in _work_for(db, B)] == ["done"] and _g(db, B)["flag_kind"] == "stock" and _alerts_for(db, B)   # B: an effective loss
    assert _tr(db, tid=tid)["building_key"] is None
    _invariant(db)


# ---------------------------------------------------------------------- 5 / 6: allowances and current membership are the live ones

def test_allowance_for_the_deleted_unit_and_a_declining_member_who_left_make_it_effective_again(client, monkeypatch, hold_followup):
    """5: G's member declined x, staff permitted a re-offer after a material change and a run relisted x -> x is EFFECTIVE for G ->
    its deletion is a loss (today's handling), although an active record names x. 6: H's member declined x (history row cached),
    the deletion is captured while they are a member, then they leave before the retry (labelled setup: the member row removed) ->
    nobody CURRENT excludes x -> loss path. Allowances and membership are the live ones at decision time."""
    db = appdb(client)
    x, other, gids = _five_groups(db, monkeypatch)
    permitted_member = _members_of(db, gids[0])[0]
    leaver = _members_of(db, gids[1])[0]
    rec = ar.record(db, permitted_member, "apartment", x, STAFF, group_id=gids[0])
    db.execute("UPDATE apartments SET rent_12m=990.0 WHERE id=?", (x,)); db.commit()   # material change
    ar.allow_reoffer(db, rec["id"], x, STAFF, note="rent came down")
    assert ar.records_for(db, permitted_member)[0]["allowed"] == [x]
    matching.full_run(db, "tester", dataset="test", use_ai=False)                      # relists x for G (permitted) through normal generation
    G, H = _group_of(db, permitted_member)["id"], _group_of(db, leaver)["id"]
    assert G != H and len(_members_of(db, H)) >= 2
    ar.record(db, leaver, "apartment", x, STAFF, group_id=H)                            # H: x declined -> pointer other, x row cached as history
    gids = sorted({r["group_id"] for r in _primaries(db)})
    _settle(db, gids)
    assert x in _listed(db, G) and {int(o["apartment_id"]) for o in matching.eligible_offers(db, G)} >= {x}
    assert int(_g(db, H)["apartment_id"]) == other and x in _listed(db, H) and [int(o["apartment_id"]) for o in matching.eligible_offers(db, H)] == [other]
    r = client.delete(f"/api/apartments/{x}")
    assert r.status_code == 200, r.text
    t = _tr(db, aid=x)
    assert {G, H} <= {w["group_id"] for w in _pending(db, t["id"])} and t["building_key"]
    db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (H, leaver)); db.commit()   # labelled setup: the declining member left
    assert leaver not in _members_of(db, H) and _members_of(db, H)
    assert not ar.group_declined_absent_row(db, H, x, t["building_key"]) and not ar.group_declined_absent_row(db, G, x, t["building_key"])
    hold_followup()
    sl.run_pending(db, transition_id=t["id"])
    for gid in (G, H):
        g = _g(db, gid)
        assert _work_for(db, gid) and _work_for(db, gid)[-1]["status"] == "done", (gid, _work_for(db, gid))
        assert g["flag_kind"] == "stock" and "was deleted from the apartment list" in (g["rerank_note"] or "") and _alerts_for(db, gid)
    assert _tr(db, tid=t["id"])["building_key"] is None
    _invariant(db)


# ------------------------------------------------------------------------------------- 7 / 9: supersession and zero-work transitions

def test_zero_work_transitions_store_null_and_supersession_clears_the_older_key(db, monkeypatch):
    a, b, c, gids = _building_world(db, monkeypatch)
    lonely = _apt(db, address="9 Lonely Lane")
    with retention_guard(db, label="t"):
        t0 = sl.record_transition(db, lonely["id"], "changed", STAFF, changed_fields=["rent_12m"])
        t_re = sl.record_transition(db, a["id"], "relisted", STAFF, bump=False)
    assert t0["group_ids"] == [] and _tr(db, tid=t0["transition_id"])["building_key"] is None
    assert t_re["group_ids"] == [] and _tr(db, tid=t_re["transition_id"])["building_key"] is None   # relisted never attaches work
    owners = _owners(db, a["id"])                                                    # independent census before the transition
    assert set(gids) < owners
    with retention_guard(db, label="t"):
        t1 = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    assert set(t1["group_ids"]) == owners and {w["group_id"] for w in _pending(db, t1["transition_id"])} == owners
    assert _tr(db, tid=t1["transition_id"])["building_key"] == ar.building_identity(a)
    with retention_guard(db, label="t"):
        t2 = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    _invariant(db)
    sl.run_pending(db, apartment_id=a["id"])                                           # t1's rows are superseded by t2, t2's complete
    t1_rows = rows(db.execute("SELECT outcome FROM offer_reconcile_work WHERE transition_id=?", (t1["transition_id"],)))
    assert len(t1_rows) == len(owners) and all(w["outcome"] == "superseded" for w in t1_rows)
    assert len(rows(db.execute("SELECT 1 FROM offer_reconcile_work WHERE transition_id=? AND status='done'", (t2["transition_id"],)))) == len(owners)
    assert _tr(db, tid=t1["transition_id"])["building_key"] is None and _tr(db, tid=t2["transition_id"])["building_key"] is None
    _invariant(db)


# ------------------------------------------------------------------------------- C1 / C3: row-present semantics are inc3's, unchanged

def test_row_present_later_decline_of_the_captured_current_is_history_exactly_as_before(client, monkeypatch, hold_followup):
    """C1: x is LEASED (row present, role 'current' captured), the member declines x in group context AFTER the transition, the retry
    runs -> `aid in history_only` -> history, as inc3 decided. C3 (L under a lease): a legacy current pointer on a declined x whose row
    is present -> judged and moved, inc3 result unchanged."""
    db = appdb(client)
    x, other, gids = _five_groups(db, monkeypatch)
    C1, L = gids[0], gids[1]
    ar.record(db, _members_of(db, L)[0], "apartment", x, STAFF)                        # L: legacy shape (no group context)
    _settle(db, gids)
    db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (x, L)); db.commit()     # labelled legacy-state setup
    assert int(_g(db, C1)["apartment_id"]) == x and int(_g(db, L)["apartment_id"]) == x
    res = matching.mark_apartment_leased(db, x, STAFF)                                 # the genuine claim; follow-ups pending (fault)
    assert res["rerank"]["status"] == "partial"
    t = _tr(db, aid=x)
    assert t["cause"] == "leased" and _role(db, t["id"], C1) == "current" and t["building_key"]
    ar.record(db, _members_of(db, C1)[0], "apartment", x, STAFF, group_id=C1)          # later decline, row present -> pointer moves, row cached
    assert int(_g(db, C1)["apartment_id"]) == other and x in _listed(db, C1)
    alerts_before = len(_alerts_for(db, C1))
    hold_followup()
    sl.run_pending(db, transition_id=t["id"])
    c1 = _g(db, C1)
    assert [w["outcome"] for w in _work_for(db, C1)] == ["unchanged"] and _listed(db, C1) == [other]
    assert (c1["rerank_note"], c1["flag_kind"]) == (None, None) and len(_alerts_for(db, C1)) == alerts_before
    l_ = _g(db, L)
    assert [w["outcome"] for w in _work_for(db, L)] == ["moved"] and int(l_["apartment_id"]) == other and l_["flag_kind"] == "lease"
    assert _tr(db, tid=t["id"])["building_key"] is None
    _invariant(db)


# -------------------------------------------------------------------------------------------------- 8 / §6: the deletion helper

def test_delete_groups_is_the_only_group_delete_and_closes_pending_work_with_the_key_in_the_same_step(db, monkeypatch):
    a, b, c, gids = _building_world(db, monkeypatch)
    P, Q = gids
    owners = _owners(db, a["id"])                                                    # independent census before the transition
    assert {P, Q} < owners
    rest = sorted(owners - {P})                                                      # Q and every alternative owner
    with retention_guard(db, label="t"):
        t = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    tid = t["transition_id"]
    assert {w["group_id"] for w in _pending(db, tid)} == owners and _tr(db, tid=tid)["building_key"]
    with pytest.raises(RuntimeError):
        sl.delete_groups(db, [P])                                                      # never outside a guard
    assert row(db.execute("SELECT 1 FROM groups WHERE id=?", (P,)))
    with retention_guard(db, label="t"):
        assert sl.delete_groups(db, []) == 0
        assert sl.delete_groups(db, [P]) == 1
        assert {w["group_id"] for w in _pending(db, tid)} == set(rest) and _tr(db, tid=tid)["building_key"]   # others still pending -> key stays
    _invariant(db)
    # rollback restores every group and every pending row (the guard's step is atomic)
    with pytest.raises(RuntimeError, match="injected"):
        with retention_guard(db, label="t"):
            assert sl.delete_groups(db, rest) == len(rest)
            assert _pending(db, tid) == [] and _tr(db, tid=tid)["building_key"] is None
            raise RuntimeError("injected")
    assert all(row(db.execute("SELECT 1 FROM groups WHERE id=?", (g,))) for g in rest)
    assert {w["group_id"] for w in _pending(db, tid)} == set(rest) and _tr(db, tid=tid)["building_key"]
    with retention_guard(db, label="t"):
        assert sl.delete_groups(db, rest) == len(rest)                                 # the last pending rows close with their groups
    assert _pending(db, tid) == [] and _tr(db, tid=tid)["building_key"] is None and _dangling_work(db) == []
    _invariant(db)


def test_source_invariant_delete_from_groups_only_inside_delete_groups():
    """§6: `DELETE FROM groups` appears in server/*.py only inside stock_lifecycle.delete_groups (docstring mentions excluded)."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
    hits = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(root, name), encoding="utf-8").read()
        for m in re.finditer(r"DELETE\s+FROM\s+groups\b", src):
            if src[max(0, m.start() - 1):m.start()] == "`":
                continue   # prose in a docstring/comment
            hits.append((name, src[:m.start()].count("\n") + 1))
    src = open(os.path.join(root, "stock_lifecycle.py"), encoding="utf-8").read()
    start = src.index("def delete_groups(")
    end = src.index("\ndef ", start + 1)
    first = src[:start].count("\n") + 1
    last = src[:end].count("\n") + 1
    assert hits == [("stock_lifecycle.py", line) for _, line in hits] and hits, hits
    assert all(first <= line <= last for _, line in hits), (hits, first, last)
    assert len(hits) == 1

def test_full_run_save_sites_delete_memberless_and_stale_groups_through_the_helper(db, monkeypatch):
    """Sites 1 and 2 (full run save): a Proposed group left without members (labelled setup) and a Proposed group whose key is no
    longer produced both go through delete_groups; their pending rows close and the key clears when the last one closes."""
    a, b, c, gids = _building_world(db, monkeypatch)
    P, Q = gids
    owners = _owners(db, a["id"])                                                    # independent census before the transition
    assert {P, Q} < owners
    with retention_guard(db, label="t"):
        t = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    tid = t["transition_id"]
    assert {w["group_id"] for w in _pending(db, tid)} == owners
    calls = _spy_delete_groups(monkeypatch)
    db.execute("DELETE FROM group_members WHERE group_id=?", (P,)); db.commit()         # labelled setup: memberless Proposed group (site 2)
    ids_before = {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    vanished = ids_before - {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    through_helper = {g for ids, _ in calls for g in ids}
    assert P in vanished and vanished == through_helper                              # every deletion of this run went through the helper
    assert all(out == len(set(ids)) for ids, out in calls)                           # and the helper's own count was returned unchanged
    assert {w["group_id"] for w in _pending(db, tid)} == owners - vanished            # each vanished owner's row closed with it
    assert _dangling_work(db) == []
    _invariant(db)
    # site 1: a new solo changes the formation; groups whose key is no longer produced go the same way
    n1 = len(calls)
    _solo_pool(db, 1, prefix="Late")
    ids_before = {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    vanished2 = ids_before - {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    assert vanished2 == {g for ids, _ in calls[n1:] for g in ids}                     # exact: the second run's deletions, all through the helper
    assert {w["group_id"] for w in _pending(db, tid)} == owners - vanished - vanished2
    assert _dangling_work(db) == []
    _invariant(db)


def test_bar_sites_delete_alternative_groups_through_the_helper(db, monkeypatch):
    """Sites 3 and 4 (bar): alternative Proposed groups holding the barred person go through delete_groups with pending work."""
    from server.enquiries import create_enquiry
    from tests.test_adults_declaration import member
    from tests.test_data_and_api import good_answers
    a = _apt(db)
    _only_this_stock(db, (a["id"],))
    ids = _solo_pool(db, 9)
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    alts = rows(db.execute("SELECT g.id, gm.enquiry_id FROM groups g JOIN group_members gm ON gm.group_id=g.id WHERE g.is_primary=0 AND g.status='Proposed'"))
    assert alts, "the fixture needs alternative groups"
    with retention_guard(db, label="t"):
        t = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    tid = t["transition_id"]
    alt_gids = {r["id"] for r in alts}
    assert alt_gids <= {w["group_id"] for w in _pending(db, tid)} and _tr(db, tid=tid)["building_key"]
    calls = _spy_delete_groups(monkeypatch)
    victim = alts[0]["enquiry_id"]
    doomed = {r["id"] for r in alts if r["enquiry_id"] == victim}
    ids_before = {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    matching.bar_enquiry(db, victim, STAFF, "test reason")                               # site 4
    vanished = ids_before - {int(r["id"]) for r in rows(db.execute("SELECT id FROM groups"))}
    assert doomed and vanished == doomed == {g for ids, _ in calls for g in ids}         # exact: the barred person's alternatives, all through the helper
    assert all(w["group_id"] not in doomed for w in _pending(db, tid)) and _dangling_work(db) == []
    _invariant(db)
    # site 3: a form party of two in a non-primary Proposed group (labelled setup: the alternative row is inserted directly)
    e = create_enquiry(db, good_answers(email="party@example.invalid", party="With friends", party_size="2", party_members=[member()]),
                       "Phone", staff="S", is_test=True, use_ai_countries=False)
    db.execute("INSERT INTO groups(status, is_primary, member_key, created_at, updated_at) VALUES ('Proposed', 0, ?, ?, ?)", (str(e["id"]), "2026-11-11T00:00:00", "2026-11-11T00:00:00"))
    alt = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?, ?)", (alt, e["id"]))
    db.execute("INSERT INTO group_apartments(group_id, apartment_id, rank) VALUES (?, ?, 1)", (alt, a["id"]))
    db.commit()
    with retention_guard(db, label="t"):
        t3 = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    assert alt in {w["group_id"] for w in _pending(db, t3["transition_id"])}
    n1 = len(calls)
    matching.bar_enquiry(db, e["id"], STAFF, "test reason", person=e["party_members"][0]["id"], party_choice="stay")   # site 3
    assert row(db.execute("SELECT 1 FROM groups WHERE id=?", (alt,))) is None
    assert alt in {g for ids, _ in calls[n1:] for g in ids}                              # the alternative row went through the helper
    assert alt not in {w["group_id"] for w in _pending(db, t3["transition_id"])} and _dangling_work(db) == []
    _invariant(db)


def test_skipped_terminal_closes_the_last_pending_row_and_nulls_the_key(db, monkeypatch):
    """Neutral M2: the third terminal of the step - the group is no longer live (LIVE_EXCLUDED) - closes its row 'done'/'skipped'
    and, when that row was the last one pending, NULLs the key in the same step; a repeat of the step on the closed row changes
    nothing. The status change is a labelled setup (the shape matching writes when a group is replaced), not a product path."""
    a, b, c, gids = _building_world(db, monkeypatch)
    P, Q = gids
    owners = _owners(db, a["id"])                                                    # independent census before the transition
    assert {P, Q} < owners
    with retention_guard(db, label="t"):
        t = sl.record_transition(db, a["id"], "changed", STAFF, changed_fields=["rent_12m"])
    tid = t["transition_id"]
    assert {w["group_id"] for w in _pending(db, tid)} == owners and _tr(db, tid=tid)["building_key"]
    closed = _close_unrelated(db, tid, keep={Q})                                     # every other owner's row, one real step each
    assert len(closed) == len(owners) - 1 and {w["group_id"] for w in _pending(db, tid)} == {Q}
    assert _tr(db, tid=tid)["building_key"]                                          # Q's row is the last one pending
    db.execute("UPDATE groups SET status='Dissolved', updated_at=? WHERE id=?", (now_iso(), Q)); db.commit()   # labelled setup
    wid = _pending(db, tid)[0]["id"]
    before = row(db.execute("SELECT * FROM groups WHERE id=?", (Q,)))
    assert sl.reconcile_work(db, wid) is None and not db.in_transaction
    w = row(db.execute("SELECT status, outcome FROM offer_reconcile_work WHERE id=?", (wid,)))
    assert (w["status"], w["outcome"]) == ("done", "skipped")
    assert _pending(db, tid) == [] and _tr(db, tid=tid)["building_key"] is None       # the skipped terminal was the last closure
    assert row(db.execute("SELECT * FROM groups WHERE id=?", (Q,))) == before          # a non-live group is left alone
    _invariant(db)
    t_after = _tr(db, tid=tid)
    assert sl.reconcile_work(db, wid) is None                                        # idempotent: nothing to do on a closed row
    assert row(db.execute("SELECT status, outcome FROM offer_reconcile_work WHERE id=?", (wid,))) == w and _tr(db, tid=tid) == t_after
    _invariant(db)


# ----------------------------------------------------------------------------------------------------------- 10: the migration

def test_migration_adds_the_building_key_column_in_place(db):
    assert "building_key" in dbm.table_columns(db, "stock_transitions")
    db.execute("ALTER TABLE stock_transitions DROP COLUMN building_key"); db.commit()
    assert "building_key" not in dbm.table_columns(db, "stock_transitions")
    dbm.migrate(db)
    assert "building_key" in dbm.table_columns(db, "stock_transitions")
    assert rows(db.execute("SELECT building_key FROM stock_transitions")) == []
