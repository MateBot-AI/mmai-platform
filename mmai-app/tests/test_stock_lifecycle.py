"""Stock lifecycle v2 (9 Nov 2026; root decisions D1-D5, F1-F3, erratum + addendum). One durable path for every transition that can
make an apartment stop being a valid offer (lease, withdrawal, material change, delete, import replace, relist): transaction 1
records the transition and one pending work row per live group that lists the apartment; transactions 2..n re-check each group in
its own retention-guard step (next valid option / NULL hold / refreshed figures), raise the ONE generic H19 attention alert with the
transition id as revision, and queue the GP8(b) refresh. Same-apartment supersession is by the latest durable transition id (NOT by
lease_generation, which stays a lease-only compatibility counter - see LIFECYCLE-IMPLEMENTATION-ADDENDUM.md). Membership, GP13
versions, reviews and introduction history are never touched by a stock change. Fabricated doubles only: no provider, no mail
transport, no customer send (MMAI_AI_QUEUE off, MMAI_ALERT_MODE unset -> alert rows are 'held')."""
import os
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from server import ai_queue, alerts, db as dbm, group_responses as gr, matching, stock_lifecycle as sl
from server.db import retention_guard, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401  (fixtures)
from tests.test_gp13_group_responses import _introduced, _lookup, _mint, _respond, _state
from tests.test_launch_neutral import _apt, _pool
from tests.test_t6_staff_api import _introduce, _ready_party

KIND = sl.EV_APARTMENT_UNAVAILABLE
A1 = "Unit 1 / 1 Neutral Street"
A2 = "Unit 2 / 2 Neutral Street"


# ------------------------------------------------------------------------------------------------------------------ helpers
def _world(db):
    """Two 2B2B apartments and four compatible solo enquirers -> every proposed group lists a1 (rank 1, current) and a2 (rank 2)."""
    db.execute("DELETE FROM apartments"); db.commit()   # the API fixture seeds the real list: only the two fabricated apartments matter here
    a1 = _apt(db)
    a2 = _apt(db, address=A2, rent12=1160.0, rent6=1235.0)
    _pool(db)
    matching.full_run(db, "tester", dataset="test")
    gids = [r["id"] for r in rows(db.execute("SELECT id FROM groups WHERE status='Proposed' ORDER BY id"))]
    assert len(gids) >= 2
    for gid in gids:
        assert [o["apartment_id"] for o in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,)))] == [a1["id"], a2["id"]]
        assert row(db.execute("SELECT apartment_id FROM groups WHERE id=?", (gid,)))["apartment_id"] == a1["id"]
    return a1, a2, gids


def _g(db, gid):
    return row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))


def _members(db, gid):
    return rows(db.execute("SELECT enquiry_id, weekly_share, room, bed, arrangement FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))


def _options(db, gid):
    return [o["apartment_id"] for o in rows(db.execute("SELECT apartment_id FROM group_apartments WHERE group_id=? ORDER BY rank, id", (gid,)))]


def _outbox(db, gid=None):
    if gid is None:
        return rows(db.execute("SELECT * FROM internal_alert_outbox WHERE kind=? ORDER BY id", (KIND,)))
    return rows(db.execute("SELECT * FROM internal_alert_outbox WHERE kind=? AND record_id=? ORDER BY id", (KIND, gid)))


def _work(db, **where):
    sql, args = "SELECT w.*, t.apartment_id, t.cause FROM offer_reconcile_work w JOIN stock_transitions t ON t.id=w.transition_id", []
    if where:
        sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    return rows(db.execute(sql + " ORDER BY w.id", args))


def _transition(db, aid, cause, staff="tester", **kw):
    """Transaction 1 alone (the route's first step): record the transition, leave the follow-up pending."""
    with retention_guard(db, label="test transition"):
        return sl.record_transition(db, aid, cause, staff, **kw)


def _lease(db, a, gid, when="2026-11-09T10:00:00"):
    db.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester', lease_generation=lease_generation+1 WHERE id=? AND leased_at IS NULL", (when, gid, a["id"])); db.commit()
    return matching.mark_apartment_leased(db, a["id"], "tester", gid, claimed_at=when)


def _snapshot_untouched(db, gid):
    """What a stock change may never alter for a group: members, GP13 versions, reviews, briefs, response links."""
    return {
        "members": rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,))),
        "versions": rows(db.execute("SELECT version, introduced_at, superseded_at FROM group_membership_versions WHERE group_id=? ORDER BY version", (gid,))),
        "reviews": row(db.execute("SELECT COUNT(*) AS n FROM review_rounds WHERE group_id=?", (gid,)))["n"],
        "briefs": row(db.execute("SELECT COUNT(*) AS n FROM group_briefs WHERE group_id=?", (gid,)))["n"],
        "links": row(db.execute("SELECT COUNT(*) AS n FROM group_response_links WHERE group_id=?", (gid,)))["n"],
        "status": _g(db, gid)["status"],
    }


# ------------------------------------------------------------------------------------------------------------------ withdrawal
def test_withdrawing_the_current_apartment_moves_every_group_to_its_next_valid_option(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    before = {gid: _snapshot_untouched(db, gid) for gid in gids}
    rec = _transition(db, a1["id"], "withdrawn", changed_fields=["available_from"])
    db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    assert sorted(rec["group_ids"]) == gids and all(w["status"] == "pending" and w["role"] == "current" for w in _work(db))
    assert rec["generation"] == 0 and row(db.execute("SELECT lease_generation FROM apartments WHERE id=?", (a1["id"],)))["lease_generation"] == 0   # lease-only counter untouched
    for gid in gids:
        assert sl.group_has_pending_work(db, gid) and not ai_queue.is_ready(db, _g(db, gid))["ready"]
        assert any("re-checked after a stock change" in r for r in ai_queue.is_ready(db, _g(db, gid))["reasons"])
    out = sl.run_pending(db, transition_id=rec["transition_id"], staff="tester")
    assert sorted(x["group_id"] for x in out) == gids and {x["outcome"] for x in out} == {"moved"}
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] == a2["id"] and g["status"] == "Proposed" and _options(db, gid) == [a2["id"]]
        assert g["weekly_rent"] == 1235.0 and g["flag_kind"] == "stock" and g["reranked_at"] and "was withdrawn" in g["rerank_note"] and A2 in g["rerank_note"]
        assert [m["weekly_share"] for m in _members(db, gid)] == [617.5, 617.5]   # the members' CURRENT shares follow the current offer
        assert g["offer_changed_since_intro_at"] is None                               # Proposed: no introduction to flag
        ob = _outbox(db, gid)
        assert len(ob) == 1 and ob[0]["status"] == "held" and ob[0]["event_id"] == alerts.event_id_for(KIND, "group", gid, rec["transition_id"])
        assert _snapshot_untouched(db, gid) == before[gid]
        assert not sl.group_has_pending_work(db, gid)
    assert all(w["status"] == "done" and w["outcome"] == "moved" for w in _work(db))
    # the note never carries raw exception / SDK text, addresses are allowed (staff-only), customers see nothing of it
    assert not re.search(r"Traceback|Error\b|psycopg|sqlite3", _g(db, gids[0])["rerank_note"])
    # retry: nothing pending, nothing doubled
    assert sl.run_pending(db, apartment_id=a1["id"]) == [] and len(_outbox(db)) == len(gids)


def test_withdrawing_an_alternative_trims_the_list_and_keeps_the_pointer(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    rec = _transition(db, a2["id"], "withdrawn", changed_fields=["available_from"])
    db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a2["id"],)); db.commit()
    assert all(w["role"] == "alternative" for w in _work(db))
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert {x["outcome"] for x in out} == {"list_trimmed"}
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] == a1["id"] and _options(db, gid) == [a1["id"]] and g["weekly_rent"] == 1225.0
        assert [m["weekly_share"] for m in _members(db, gid)] == [612.5, 612.5]
        assert len(_outbox(db, gid)) == 1   # H19: a previously suggested apartment became unavailable


def test_losing_the_sole_remaining_offer_holds_the_group_with_a_null_pointer(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    r1 = _transition(db, a2["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a2["id"],)); db.commit()
    sl.run_pending(db, transition_id=r1["transition_id"])
    before = {gid: _snapshot_untouched(db, gid) for gid in gids}
    r2 = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    queued: list = []
    real_enqueue = ai_queue.enqueue
    monkeypatch.setattr(ai_queue, "enqueue", lambda c, ids, **kw: (queued.extend(ids if isinstance(ids, (list, tuple, set)) else [ids]), real_enqueue(c, ids, **kw))[1])
    out = sl.run_pending(db, transition_id=r2["transition_id"])
    assert {x["outcome"] for x in out} == {"no_apartment"}
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] is None and g["status"] == "Proposed" and _options(db, gid) == []
        assert g["weekly_rent"] is None and g["total_weekly"] is None and g["allocation_json"] is None and g["lease_start"] is None
        # the members stay; nothing is shown as a CURRENT allocation
        assert len(_members(db, gid)) == 2 and all(m["weekly_share"] is None and m["room"] is None and m["bed"] is None and m["arrangement"] is None for m in _members(db, gid))
        assert _snapshot_untouched(db, gid) == before[gid]
        rd = ai_queue.is_ready(db, g)
        assert not rd["ready"] and any("no current apartment" in r for r in rd["reasons"])
        assert len(_outbox(db, gid)) == 2 and len({o["event_id"] for o in _outbox(db, gid)}) == 2   # one alert per lost apartment
        assert gid not in queued   # a group held with no apartment is not queued for a refresh (nothing to rank)
    # held state is visible and retryable: no pending work left, but the hold is explicit
    assert sl.pending_work(db) == []


# ------------------------------------------------------------------------------------------------------------------ material change
def test_material_change_on_the_current_apartment_refreshes_figures_and_member_shares(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    before = {gid: _snapshot_untouched(db, gid) for gid in gids}
    assert sl.material_changes(a1, {"rent_6m": 1325.0, "parking_note": "x"}) == {"rent_6m": [1225.0, 1325.0]}
    assert sl.material_changes(a1, {"rent_6m": 1225.0}) == {}
    rec = _transition(db, a1["id"], "changed", changed_fields=["rent_6m"])
    db.execute("UPDATE apartments SET rent_6m=1325.0 WHERE id=?", (a1["id"],)); db.commit()
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert {x["outcome"] for x in out} == {"refreshed"}
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] == a1["id"] and _options(db, gid) == [a1["id"], a2["id"]]
        assert g["weekly_rent"] == 1325.0 and g["total_weekly"] == 1325.0
        assert [m["weekly_share"] for m in _members(db, gid)] == [662.5, 662.5]   # same pointer: the member shares follow too
        assert g["offer_changed_since_intro_at"] is None and g["rerank_note"] is None   # Proposed: nothing lost, nothing to flag
        assert _outbox(db, gid) == [] and _snapshot_untouched(db, gid) == before[gid]
    assert all(w["outcome"] == "refreshed" for w in _work(db))


def test_material_change_that_breaks_a_rule_removes_the_option(db, monkeypatch):
    """a later available date beyond the A17 window is no longer a valid offer for the group: next option, alert, note"""
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    rec = _transition(db, a1["id"], "changed", changed_fields=["available_from"])
    db.execute("UPDATE apartments SET available_from='2031-01-27' WHERE id=?", (a1["id"],)); db.commit()
    out = sl.run_pending(db, transition_id=rec["transition_id"])
    assert {x["outcome"] for x in out} == {"moved"}
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] == a2["id"] and _options(db, gid) == [a2["id"]] and len(_outbox(db, gid)) == 1
        assert A1 in g["rerank_note"] and A2 in g["rerank_note"] and "next best apartment" in g["rerank_note"]


# ------------------------------------------------------------------------------------------------------------------ delete
def test_deleting_the_current_apartment_moves_groups_and_deleting_the_last_holds_them(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db = appdb(client)
    a1, a2, gids = _world(db)
    r = client.delete(f"/api/apartments/{a1['id']}")
    assert r.status_code == 200, r.text
    fu = r.json()["groups_rechecked"]
    assert fu["status"] == "complete" and sorted(x["group_id"] for x in fu["affected"]) == gids and fu["pending_group_ids"] == []
    assert row(db.execute("SELECT 1 FROM apartments WHERE id=?", (a1["id"],))) is None
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] == a2["id"] and _options(db, gid) == [a2["id"]] and len(_members(db, gid)) == 2
    t = row(db.execute("SELECT * FROM stock_transitions WHERE id=?", (fu["transition_id"],)))
    assert t["cause"] == "deleted" and t["apartment_id"] == a1["id"] and t["address"] == A1   # the history keeps the address of a deleted row
    r = client.delete(f"/api/apartments/{a2['id']}")
    assert r.status_code == 200
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] is None and g["status"] == "Proposed" and _options(db, gid) == [] and len(_members(db, gid)) == 2
        d = client.get(f"/api/groups/{gid}").json()
        assert d["no_current_offer"] is True and d["stock_recheck_pending"] is False and d["apartment_id"] is None
        assert len(_outbox(db, gid)) == 2
    lst = client.get("/api/groups", params={"dataset": "test"}).json()["items"]
    assert all(x["no_current_offer"] is True and x["apartment_id"] is None for x in lst if x["id"] in gids) and any(x["id"] in gids for x in lst)   # held groups stay listed, shown as held
    # deleting the apartment twice / an unknown one is harmless
    assert client.delete(f"/api/apartments/{a1['id']}").status_code == 200 and "groups_rechecked" not in client.delete(f"/api/apartments/{a1['id']}").json()


# ------------------------------------------------------------------------------------------------------------------ staff routes
def test_patch_route_records_withdrawal_or_change_and_ignores_non_material_edits(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db = appdb(client)
    a1, a2, gids = _world(db)
    r = client.patch(f"/api/apartments/{a1['id']}", json={"parking_note": "Permit from the building manager"})
    assert r.status_code == 200 and "groups_rechecked" not in r.json() and rows(db.execute("SELECT 1 FROM stock_transitions")) == []
    r = client.patch(f"/api/apartments/{a1['id']}", json={"rent_6m": 1325.0})
    assert r.status_code == 200, r.text
    fu = r.json()["groups_rechecked"]
    assert fu["status"] == "complete" and {x["outcome"] for x in fu["affected"]} == {"refreshed"}
    assert row(db.execute("SELECT cause, changed_fields FROM stock_transitions WHERE id=?", (fu["transition_id"],)))["cause"] == "changed"
    assert all(_g(db, gid)["weekly_rent"] == 1325.0 for gid in gids)
    r = client.patch(f"/api/apartments/{a1['id']}", json={"available_from": None})
    assert r.status_code == 200, r.text
    fu = r.json()["groups_rechecked"]
    assert {x["outcome"] for x in fu["affected"]} == {"moved"} and row(db.execute("SELECT cause FROM stock_transitions WHERE id=?", (fu["transition_id"],)))["cause"] == "withdrawn"
    assert all(_g(db, gid)["apartment_id"] == a2["id"] for gid in gids)
    aud = rows(db.execute("SELECT detail FROM audit_log WHERE action='update_apartment' ORDER BY id"))
    assert "withdrawn" in aud[-1]["detail"] and "re-check" in aud[-1]["detail"]
    # the apartment page lists nothing pending once the follow-up completed
    assert client.get(f"/api/apartments/{a2['id']}").json().get("rerank_pending_group_ids", []) == []


def test_introduced_needs_a_current_apartment_and_leased_waits_for_pending_work(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    g = _g(db, gid)
    aid = g["apartment_id"]
    assert g["status"] == "Introduced" and aid
    # pending stock work blocks readiness / circulation / Leased - not only a label
    rec = _transition(db, aid, "changed", changed_fields=["rent_6m"])
    assert sl.group_has_pending_work(db, gid)
    rd = ai_queue.is_ready(db, _g(db, gid))
    assert not rd["ready"] and any("re-checked after a stock change" in r for r in rd["reasons"])
    assert ai_queue.can_circulate(db, _g(db, gid))["ok"] is False
    d = client.get(f"/api/groups/{gid}").json()
    assert d["stock_recheck_pending"] is True and d["ready"] is not True
    r = client.patch(f"/api/groups/{gid}", json={"status": "Leased"})
    assert r.status_code == 409 and "re-checked" in r.json()["detail"]
    assert client.post(f"/api/groups/{gid}/introduction").status_code == 409
    # GP13 state is unaffected by the stock work: the membership response page still applies, the version is the same
    st = _state(client, gid)
    assert st["applies"] and st["version"]["version"] == 1
    sl.run_pending(db, transition_id=rec["transition_id"])
    # BEFORE (succ2): unchanged figures -> "nothing moved, no flag" even though the transition named a MATERIAL field (rent_6m).
    # AFTER (succ3, L3b, root clarification 10 Nov 2026): a material-field change on the group's CURRENT apartment refreshes the
    # group and stamps the display flag whether or not the stored figures differ - the introduced text described the apartment as it
    # was. Pointer, members, GP13 version are untouched (checked below). The work completes (no pending hold).
    assert not sl.group_has_pending_work(db, gid) and _g(db, gid)["apartment_id"] == aid
    assert _g(db, gid)["offer_changed_since_intro_at"] is not None
    assert row(db.execute("SELECT outcome FROM offer_reconcile_work WHERE transition_id=? AND group_id=?", (rec["transition_id"], gid)))["outcome"] == "refreshed"
    # the Introduced group loses its sole offer: held with the NULL pointer, status unchanged, flagged; Leased refused
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id<>?", (gid, aid)); db.commit()
    rec = _transition(db, aid, "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (aid,)); db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    g = _g(db, gid)
    assert g["apartment_id"] is None and g["status"] == "Introduced" and g["offer_changed_since_intro_at"]   # held; the introduction is outdated
    r = client.patch(f"/api/groups/{gid}", json={"status": "Leased"})
    assert r.status_code == 409 and "no apartment" in r.json()["detail"].lower()
    assert client.post(f"/api/groups/{gid}/introduction").status_code == 409
    assert _state(client, gid)["version"]["version"] == 1   # GP13: no membership change, no reset


def test_introduced_is_refused_for_a_group_held_without_an_apartment(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, a, lead, solo, g = _ready_party(client, monkeypatch)
    gid = g["id"]
    assert client.post(f"/api/groups/{gid}/introduction").status_code == 200
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id<>?", (gid, a["id"])); db.commit()
    rec = _transition(db, a["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a["id"],)); db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    g = _g(db, gid)
    assert g["apartment_id"] is None and g["status"] == "Proposed" and g["offer_changed_since_intro_at"] is None   # not introduced: nothing to flag
    r = client.patch(f"/api/groups/{gid}", json=_introduce(client, gid))
    assert r.status_code == 409, r.text
    assert _g(db, gid)["status"] == "Proposed"
    d = client.get(f"/api/groups/{gid}").json()
    assert d["no_current_offer"] is True and d["ready"] is not True


def test_offer_acknowledgement_clears_only_the_flag_and_refuses_when_nothing_valid_is_current(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    g0 = _g(db, gid)
    aid = g0["apartment_id"]
    v0 = row(db.execute("SELECT version, introduced_at FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,)))
    # a no-op when nothing changed
    assert client.patch(f"/api/groups/{gid}", json={"offer_ack": True}).status_code == 200 and _g(db, gid)["offer_changed_since_intro_at"] is None
    # a same-pointer rent change on an Introduced group flags the offer (the public page withholds details until acknowledged)
    rec = _transition(db, aid, "changed", changed_fields=["rent_6m"])
    db.execute("UPDATE apartments SET rent_6m=rent_6m+100 WHERE id=?", (aid,)); db.commit()
    assert {x["outcome"] for x in sl.run_pending(db, transition_id=rec["transition_id"])} == {"refreshed"}
    g = _g(db, gid)
    assert g["offer_changed_since_intro_at"] and g["weekly_rent"] == g0["weekly_rent"] + 100
    d = client.get(f"/api/groups/{gid}").json()
    assert d["offer_changed_since_intro"] is True and d["no_current_offer"] is False
    # refused while re-check work is pending
    rec2 = _transition(db, aid, "changed", changed_fields=["rent_6m"])
    r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
    assert r.status_code == 409 and "re-checked" in r.json()["detail"]
    sl.run_pending(db, transition_id=rec2["transition_id"])
    # refused when the current introduction draft is not current (it went stale with the offer change); the facts changed since the AI
    # review too, so staff re-queue the assessment (existing T6 workflow, faked review) and draft again before acknowledging
    ist = ai_queue.intro_state(db, gid)
    if not ist.get("current"):
        r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
        assert r.status_code == 409 and "introduction" in r.json()["detail"].lower()
        ai_queue.reassess_all(db, "test", "tester")
        ai_queue.process_pending(db)
        assert client.post(f"/api/groups/{gid}/introduction").status_code == 200, (client.get(f"/api/groups/{gid}").json().get("circulation"))
    r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
    assert r.status_code == 200, r.text
    g = _g(db, gid)
    assert g["offer_changed_since_intro_at"] is None and g["status"] == "Introduced" and g["apartment_id"] == aid
    assert row(db.execute("SELECT version, introduced_at FROM group_membership_versions WHERE group_id=? AND superseded_at IS NULL", (gid,))) == v0   # no membership version / clock change
    assert row(db.execute("SELECT 1 FROM audit_log WHERE action='group_offer_acknowledged' AND entity_id=?", (str(gid),)))
    # refused with no valid current apartment (held after a withdrawal)
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id<>?", (gid, aid)); db.commit()
    rec3 = _transition(db, aid, "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (aid,)); db.commit()
    sl.run_pending(db, transition_id=rec3["transition_id"])
    assert _g(db, gid)["apartment_id"] is None and _g(db, gid)["offer_changed_since_intro_at"]
    r = client.patch(f"/api/groups/{gid}", json={"offer_ack": True})
    assert r.status_code == 409 and "no valid current apartment" in r.json()["detail"]


def test_public_response_page_withholds_apartment_details_but_still_takes_the_answer(client, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    db, lead, solo, gid = _introduced(client, monkeypatch)
    aid = _g(db, gid)["apartment_id"]
    people = _state(client, gid)["people"]
    tok = _mint(client, gid, people[0]["person_uid"])["token"]
    view = _lookup(client, tok).json()
    assert view["group"]["address"] and view["group"]["apartment_changed"] is False and view["group"]["proposed_weekly_share"]
    # same-pointer material change -> the page must not show the new rent under the old introduction
    rec = _transition(db, aid, "changed", changed_fields=["rent_6m"])
    db.execute("UPDATE apartments SET rent_6m=rent_6m+100 WHERE id=?", (aid,)); db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    view = _lookup(client, tok).json()
    gv = view["group"]
    assert gv["apartment_changed"] is True and gv["apartment_changed_text"] == gr.APARTMENT_CHANGED
    assert gv["address"] is None and gv["suburb"] is None and gv["lease_start"] is None and gv["proposed_weekly_share"] is None and gv["proposed_total_weekly"] is None
    assert "Apartment details have changed" in gv["apartment_changed_text"] and "not an agreement to rent" in gv["apartment_changed_text"]
    r = _respond(client, tok, "accept")
    assert r.status_code == 200, r.text   # the answer is about the housemates: still accepted
    st = _state(client, gid)
    assert st["version"]["version"] == 1 and any((p["response"] or {}).get("response") == "accept" if isinstance(p["response"], dict) else p["response"] == "accept" for p in st["people"])
    # a NULL pointer (sole offer lost) withholds the same way
    db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id<>?", (gid, aid)); db.commit()
    rec = _transition(db, aid, "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (aid,)); db.commit()
    sl.run_pending(db, transition_id=rec["transition_id"])
    assert _g(db, gid)["apartment_id"] is None
    view = _lookup(client, tok).json()
    assert view["group"]["apartment_changed"] is True and view["group"]["address"] is None
    assert _respond(client, tok, "accept").status_code == 200


# ------------------------------------------------------------------------------------------------------------------ supersession
def test_relist_records_a_distinct_latest_transition_and_supersedes_older_pending_work(db, monkeypatch):
    """lease -> relist -> lease: supersession is by the latest transition id for the apartment, never by lease_generation"""
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    leasing, others = gids[0], gids[1:]
    # the lease's follow-up is left pending for one group (its step failed): a stale row from the lease transition
    victim = others[0]
    real = alerts.raise_attention
    fired = []

    def flaky(c, kind, record_id, **kw):
        if record_id == victim and not fired:
            fired.append(1)
            return {"event_id": None, "status": "error", "created": False, "error": "OperationalError"}
        return real(c, kind, record_id, **kw)
    monkeypatch.setattr(alerts, "raise_attention", flaky)
    out = _lease(db, a1, leasing)
    assert out["rerank"]["pending_group_ids"] == [victim]
    lease_tid = row(db.execute("SELECT id FROM stock_transitions WHERE apartment_id=? AND cause='leased'", (a1["id"],)))["id"]
    stale = _work(db, group_id=victim)
    assert len(stale) == 1 and stale[0]["status"] == "pending" and stale[0]["transition_id"] == lease_tid
    note_before = _g(db, victim)["rerank_note"]
    assert _g(db, victim)["apartment_id"] == a1["id"] and _options(db, victim) == [a1["id"], a2["id"]]   # the failed step rolled back
    # relist: a distinct, newer transition for the same apartment; the generation counter is NOT bumped (lease-only counter)
    matching.relist_apartment(db, a1["id"], "2027-02-10", "tester")
    rl = sl.latest_transition(db, a1["id"])
    assert rl["cause"] == "relisted" and rl["id"] > lease_tid and rl["generation"] == 1
    assert row(db.execute("SELECT lease_generation, leased_at, available_from FROM apartments WHERE id=?", (a1["id"],))) == {"lease_generation": 1, "leased_at": None, "available_from": "2027-02-10"}
    assert _work(db, transition_id=rl["id"]) == []   # a relist takes nothing away: no work rows
    # the stale lease row is superseded, untouched: no note overwrite, no alert, options intact (the apartment IS available again)
    got = matching.retry_pending_lease_reranks(db, "tester")
    w = _work(db, group_id=victim)
    assert len(w) == 1 and w[0]["status"] == "superseded" and w[0]["outcome"] == "superseded"
    assert _g(db, victim)["rerank_note"] == note_before and _options(db, victim) == [a1["id"], a2["id"]] and _outbox(db, victim) == []
    assert got["failed"] == [] and sl.pending_work(db) == []
    # the second lease of the same apartment is a new event for the groups still listing it
    db.execute("UPDATE groups SET status='Proposed' WHERE id=?", (leasing,)); db.commit()   # fabricated: the lease fell through
    out2 = _lease(db, a1, leasing, when="2026-11-10T10:00:00")
    assert out2["rerank"]["status"] == "complete"
    t2 = sl.latest_transition(db, a1["id"])
    assert t2["cause"] == "leased" and t2["generation"] == 2 and t2["id"] > rl["id"]
    # the victim still listed a1 (its first step rolled back): it is the one re-checked now, alerted ONCE with the new transition id
    assert [o["event_id"] for o in _outbox(db, victim)] == [alerts.event_id_for(KIND, "group", victim, t2["id"])]
    assert _g(db, victim)["apartment_id"] == a2["id"] and _options(db, victim) == [a2["id"]]
    for gid in others[1:]:   # moved on the first lease; nothing new for them
        assert [o["event_id"] for o in _outbox(db, gid)] == [alerts.event_id_for(KIND, "group", gid, lease_tid)] and _g(db, gid)["apartment_id"] == a2["id"]
    assert alerts.event_id_for(KIND, "group", victim, lease_tid) not in {o["event_id"] for o in _outbox(db)}   # the superseded step never alerted


def test_changed_then_deleted_before_the_retry_skips_the_stale_change_without_a_stale_note(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    gid = gids[0]
    # transaction 1 of a rent change; the follow-up never ran (e.g. the server stopped)
    r1 = _transition(db, a1["id"], "changed", changed_fields=["rent_6m"])
    db.execute("UPDATE apartments SET rent_6m=1325.0 WHERE id=?", (a1["id"],)); db.commit()
    assert all(w["status"] == "pending" for w in _work(db, transition_id=r1["transition_id"]))
    # then the apartment is deleted (transaction 1 of the delete), before any retry
    r2 = _transition(db, a1["id"], "deleted")
    db.execute("DELETE FROM apartments WHERE id=?", (a1["id"],)); db.commit()
    assert sl.latest_transition(db, a1["id"])["id"] == r2["transition_id"]   # derived from the transitions, the row is gone
    out = matching.retry_pending_lease_reranks(db, "tester")
    assert out["failed"] == []
    for w in _work(db, transition_id=r1["transition_id"]):
        assert w["status"] == "superseded"
    for w in _work(db, transition_id=r2["transition_id"]):
        assert w["status"] == "done" and w["outcome"] == "moved"
    g = _g(db, gid)
    assert g["apartment_id"] == a2["id"] and _options(db, gid) == [a2["id"]]
    assert "was deleted" in g["rerank_note"] and "1325" not in g["rerank_note"]   # the note is the delete's, not the stale change's
    ob = _outbox(db, gid)
    assert [o["event_id"] for o in ob] == [alerts.event_id_for(KIND, "group", gid, r2["transition_id"])]   # one alert, for the latest transition
    assert sl.pending_work(db) == []


def test_relist_then_patch_then_delete_each_supersede_older_same_apartment_work(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    r1 = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    # staff relist it straight away (available again): the withdrawal's work is now stale
    matching.relist_apartment(db, a1["id"], "2027-01-27", "tester")
    r2 = _transition(db, a1["id"], "changed", changed_fields=["rent_6m"]); db.execute("UPDATE apartments SET rent_6m=1325.0 WHERE id=?", (a1["id"],)); db.commit()
    assert sl.run_pending(db, transition_id=r1["transition_id"]) == [] and all(w["status"] == "superseded" for w in _work(db, transition_id=r1["transition_id"]))
    out = sl.run_pending(db, transition_id=r2["transition_id"])
    assert {x["outcome"] for x in out} == {"refreshed"} and all(_g(db, gid)["weekly_rent"] == 1325.0 and _g(db, gid)["apartment_id"] == a1["id"] for gid in gids)
    assert _outbox(db) == []   # nothing was lost: the withdrawal never produced an alert once superseded by the relist


def test_two_separate_apartments_lost_keep_their_own_alerts_in_both_orders(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    r1 = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    r2 = _transition(db, a2["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a2["id"],)); db.commit()
    assert len(sl.pending_work(db)) == 2 * len(gids)
    # order A: the later transition (a2) first, then the earlier (a1) - neither supersedes the other (different apartments)
    sl.run_pending(db, transition_id=r2["transition_id"])
    sl.run_pending(db, transition_id=r1["transition_id"])
    for gid in gids:
        ev = sorted(o["event_id"] for o in _outbox(db, gid))
        assert ev == sorted([alerts.event_id_for(KIND, "group", gid, r1["transition_id"]), alerts.event_id_for(KIND, "group", gid, r2["transition_id"])])
        assert _g(db, gid)["apartment_id"] is None and _options(db, gid) == []
    assert {w["status"] for w in _work(db)} == {"done"}


def test_two_separate_apartments_lost_order_b(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    r1 = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    r2 = _transition(db, a2["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a2["id"],)); db.commit()
    sl.run_pending(db, transition_id=r1["transition_id"])
    for gid in gids:
        # a1's step judges every option against TODAY's stock: a2 is already withdrawn too, so the group is held at once; one alert (a1's)
        assert _g(db, gid)["apartment_id"] is None and _options(db, gid) == [] and [o["event_id"] for o in _outbox(db, gid)] == [alerts.event_id_for(KIND, "group", gid, r1["transition_id"])]
    sl.run_pending(db, transition_id=r2["transition_id"])
    for gid in gids:
        # a2's own step is still current work (different apartment): its loss gets its own alert, the hold is unchanged
        assert _g(db, gid)["apartment_id"] is None and len(_outbox(db, gid)) == 2 and len({o["event_id"] for o in _outbox(db, gid)}) == 2
    assert {w["status"] for w in _work(db)} == {"done"}


# ------------------------------------------------------------------------------------------------------------------ failure / retry
def test_outbox_failure_leaves_the_withdrawal_step_pending_and_the_retry_recovers_it(db, monkeypatch):
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    victim = gids[0]
    real = alerts.raise_attention
    fired = []

    def flaky(c, kind, record_id, **kw):
        if record_id == victim and not fired:
            fired.append(1)
            return {"event_id": None, "status": "error", "created": False, "error": "OperationalError"}
        return real(c, kind, record_id, **kw)
    monkeypatch.setattr(alerts, "raise_attention", flaky)
    rec = _transition(db, a1["id"], "withdrawn"); db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (a1["id"],)); db.commit()
    failed = []
    out = sl.run_pending(db, transition_id=rec["transition_id"], failures=failed)
    assert failed == [{"group_id": victim, "category": "AlertOutboxUnavailable"}] and sorted(x["group_id"] for x in out) == gids[1:]
    assert _g(db, victim)["apartment_id"] == a1["id"] and _options(db, victim) == [a1["id"], a2["id"]] and _outbox(db, victim) == []   # rolled back whole
    assert sl.group_has_pending_work(db, victim) and not ai_queue.is_ready(db, _g(db, victim))["ready"]
    out = sl.run_pending(db, apartment_id=a1["id"])
    assert [x["group_id"] for x in out] == [victim] and _g(db, victim)["apartment_id"] == a2["id"] and len(_outbox(db, victim)) == 1
    assert sl.pending_work(db) == [] and len(_outbox(db)) == len(gids)


def test_transition_outside_a_guard_or_with_an_unknown_cause_is_refused(db):
    a1, a2, gids = _world(db)
    with pytest.raises(RuntimeError):
        sl.record_transition(db, a1["id"], "withdrawn", "tester")
    with pytest.raises(ValueError):
        _transition(db, a1["id"], "vanished")
    with pytest.raises(ValueError):
        _transition(db, 99999, "withdrawn")
    assert rows(db.execute("SELECT 1 FROM stock_transitions")) == []


# ------------------------------------------------------------------------------------------------------------------ import
def _sheet(path, recs):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    cols = ["Address", "Apartment type", "Bedroom 1", "Bedroom 2", "Bedroom 3", "Bills/utilities", "2026 12 months rate", "2026 6 months rate", "Vacate date"]
    ws.append(cols)
    for r in recs:
        ws.append([r.get(k) for k in cols])
    wb.save(path)


def test_import_update_records_withdrawals_and_changes_and_replace_retires_history_preservingly(db, tmp_path, monkeypatch):
    from datetime import date
    from server import importer
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    a1, a2, gids = _world(db)
    base = {"Apartment type": "2B2B", "Bedroom 1": "1 king or 2 singles", "Bedroom 2": "1 king or 2 singles", "Bedroom 3": None, "Bills/utilities": "Not included"}
    db.execute("UPDATE apartments SET suburb='Melbourne CBD'"); db.commit()   # as the importer normalises the address, so only the sheet's changes count
    # update by address: a1 withdrawn (no date), a2 rent changed
    p = str(tmp_path / "list.xlsx")
    _sheet(p, [dict(base, Address=A1, **{"2026 12 months rate": 1150.0, "2026 6 months rate": 1225.0, "Vacate date": "TBA"}),
               dict(base, Address=A2, **{"2026 12 months rate": 1160.0, "2026 6 months rate": 1335.0, "Vacate date": date(2027, 1, 27)})])
    rep = importer.import_apartments(db, p, "tester")
    assert rep["errors"] == [] and rep["updated"] == 1 and rep["unchanged"] == 1 and rep["added"] == 0, rep   # a1: date kept, nothing else changed
    # A15(a): the list says text where staff entered a date -> the staff date is KEPT, so a1 is not withdrawn by the import
    assert [x["address"] for x in rep["dates_kept"]] == [A1]
    causes = [row(db.execute("SELECT cause FROM stock_transitions WHERE id=?", (t,)))["cause"] for t in rep["stock_transitions"]]
    assert causes == ["changed"] and {x["outcome"] for x in rep["groups_rechecked"]["affected"]} == {"refreshed"} and rep["groups_rechecked"]["pending_group_ids"] == []
    for gid in gids:
        assert _g(db, gid)["apartment_id"] == a1["id"]
        assert row(db.execute("SELECT weekly_rent FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, a2["id"])))["weekly_rent"] == 1335.0
    # a genuine withdrawal through the list: the date column cleared for an apartment whose staff date is also cleared first
    db.execute("UPDATE apartments SET available_from=NULL, available_raw='TBA' WHERE id=?", (a2["id"],)); db.commit()   # staff already withdrew it... 
    rec = _transition(db, a2["id"], "withdrawn"); sl.run_pending(db, transition_id=rec["transition_id"])
    for gid in gids:
        assert _options(db, gid) == [a1["id"]]
    # replace=True: every existing apartment retires through the lifecycle ('replaced') before the wipe; the groups are held, not deleted
    before = {gid: _snapshot_untouched(db, gid) for gid in gids}
    p2 = str(tmp_path / "list2.xlsx")
    _sheet(p2, [dict(base, Address="Unit 9 / 9 Neutral Street", **{"2026 12 months rate": 1150.0, "2026 6 months rate": 1225.0, "Vacate date": date(2027, 1, 27)})])
    rep = importer.import_apartments(db, p2, "tester", replace=True)
    assert rep["errors"] == [] and rep["added"] == 1
    assert sorted(row(db.execute("SELECT cause FROM stock_transitions WHERE id=?", (t,)))["cause"] for t in rep["stock_transitions"]) == ["replaced", "replaced"]
    assert [a["address"] for a in rows(db.execute("SELECT address FROM apartments"))] == ["Unit 9 / 9 Neutral Street"]
    for gid in gids:
        g = _g(db, gid)
        assert g["apartment_id"] is None and g["status"] == "Proposed" and _options(db, gid) == [] and len(_members(db, gid)) == 2
        assert _snapshot_untouched(db, gid) == before[gid]
        assert any(o["event_id"].endswith(f":{rep['stock_transitions'][0]}") for o in _outbox(db, gid))   # a1's replacement alerted (the group listed it)
    assert sl.pending_work(db) == []


# ------------------------------------------------------------------------------------------------------------------ migration + bootstrap
OLD_DEF = "apartment_id INTEGER NOT NULL REFERENCES apartments(id) ON DELETE CASCADE"
NEW_DEF = "apartment_id INTEGER REFERENCES apartments(id) ON DELETE SET NULL"


def _old_shape_schema():
    s = dbm.SCHEMA
    i = s.index("CREATE TABLE IF NOT EXISTS groups (")
    k = s.index(NEW_DEF, i)
    assert k < s.index(");", i)
    return s[:k] + OLD_DEF + s[k + len(NEW_DEF):]


@pytest.fixture
def old_shape(tmp_path, monkeypatch):
    """A database created in the five-fix shape (groups.apartment_id NOT NULL ... ON DELETE CASCADE, no bootstrap marker)."""
    monkeypatch.setattr(dbm, "SCHEMA", _old_shape_schema())
    monkeypatch.setattr(dbm, "_migrate_groups_apartment_fk", lambda c: False)
    monkeypatch.setattr(sl, "bootstrap", lambda c: {"bootstrapped": False})
    path = str(tmp_path / "old.db")
    os.environ["MMAI_DB"] = path
    c = dbm.connect(path)
    assert dbm._groups_fk_state(c) == (True, "CASCADE")
    yield c, path
    monkeypatch.undo()


def _legacy_state(c):
    """Five-fix world: a1 leased to g0 with its rerank done for g1 (legacy alert row), but NOT for g2 (outstanding); g3 pointed at a
    leased apartment with no options left (old 'no_apartment' outcome)."""
    a1, a2, gids = _world(c)
    g0, g1, g2 = gids[0], gids[1], gids[2]
    when = "2026-11-09T10:00:00"
    c.execute("UPDATE apartments SET leased_at=?, leased_group_id=?, leased_by='tester', lease_generation=1 WHERE id=?", (when, g0, a1["id"]))
    c.execute("UPDATE groups SET status='Leased' WHERE id=?", (g0,))
    legacy_rev = a1["id"] * 10 ** 6 + 1
    # g1: the old rerank completed (moved to a2, legacy event row present)
    c.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g1, a1["id"]))
    c.execute("UPDATE groups SET apartment_id=?, reranked_at='2026-11-09T10:00:01', rerank_note='old' WHERE id=?", (a2["id"], g1))
    c.execute("""INSERT INTO internal_alert_outbox(kind, record_type, record_id, event_id, to_email, is_test, status, created_at, updated_at)
                 VALUES (?,?,?,?,?,1,'held',?,?)""", (KIND, "group", g1, f"{KIND}:group:{g1}:{legacy_rev}", alerts.RECIPIENT, when, when))
    c.commit()
    return a1, a2, gids, legacy_rev


def test_sqlite_migration_rebuilds_groups_preserving_everything_and_bootstrap_dedupes_legacy_events(old_shape, monkeypatch):
    c, path = old_shape
    if getattr(c, "backend", "sqlite") == "postgres":
        pytest.skip("SQLite rebuild path")
    a1, a2, gids, legacy_rev = _legacy_state(c)
    g0, g1, g2 = gids[0], gids[1], gids[2]
    old_cols = [tuple(r)[1:] for r in c.execute("PRAGMA table_info(groups)").fetchall()]
    old_extras = sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name='groups' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall())
    old_fks = sorted(tuple(r)[2:] for r in c.execute("PRAGMA foreign_key_list(groups)").fetchall() if r[3] != "apartment_id")
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in dbm._CHILD_TABLES_OF_GROUPS if c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()}
    groups_before = rows(c.execute("SELECT * FROM groups ORDER BY id"))
    seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='groups'").fetchone()[0]
    c.close()
    monkeypatch.undo()   # the real schema, migration and bootstrap
    dbm._conn = None
    c = dbm.connect(path)
    assert dbm._groups_fk_state(c) == (False, "SET NULL")
    assert [tuple(r)[1:] for r in c.execute("PRAGMA table_info(groups)").fetchall() if r[1] != "apartment_id"] == [x for x in old_cols if x[0] != "apartment_id"]
    assert sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE tbl_name='groups' AND type IN ('index','trigger') AND sql IS NOT NULL").fetchall()) == old_extras
    assert sorted(tuple(r)[2:] for r in c.execute("PRAGMA foreign_key_list(groups)").fetchall() if r[3] != "apartment_id") == old_fks
    for t, n in counts.items():
        if t == "offer_reconcile_work":
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] >= n   # bootstrap adds the carried work rows (checked below)
        else:
            assert c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == n, t
    seqs = c.execute("SELECT seq FROM sqlite_sequence WHERE name='groups'").fetchall()
    assert len(seqs) == 1 and int(seqs[0][0]) >= seq
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    after = rows(c.execute("SELECT * FROM groups ORDER BY id"))
    # every row survived; only bootstrap's own, documented changes differ (g2's pending carry-over leaves rows as they were)
    assert [g["id"] for g in after] == [g["id"] for g in groups_before]
    # bootstrap: marker, revision floor above the legacy event revision, one leased transition, g2 carried with its legacy id
    assert row(c.execute("SELECT value FROM settings WHERE key=?", (sl.BOOTSTRAP_KEY,)))
    t = rows(c.execute("SELECT * FROM stock_transitions ORDER BY id"))
    assert len(t) == 1 and t[0]["cause"] == "leased" and t[0]["apartment_id"] == a1["id"] and t[0]["generation"] == 1 and t[0]["id"] > legacy_rev
    w = _work(c)
    assert {x["group_id"] for x in w} >= {g2} and g0 not in {x["group_id"] for x in w} and all(x["status"] == "pending" for x in w)
    carried = [x for x in w if x["group_id"] == g2][0]
    assert carried["legacy_event_id"] == f"{KIND}:group:{g2}:{legacy_rev}"
    # idempotent: a second open changes nothing more
    n_t, n_w = len(t), len(w)
    c.close(); dbm._conn = None
    c = dbm.connect(path)
    assert len(rows(c.execute("SELECT * FROM stock_transitions"))) == n_t and len(_work(c)) == n_w and sl.bootstrap(c) == {"bootstrapped": False}
    # running the carried work: g2 moves to a2 and alerts once with the NEW id; g1 (legacy row exists) would not be alerted twice
    c.execute("INSERT OR IGNORE INTO offer_reconcile_work(transition_id, group_id, role, status, legacy_event_id, created_at) VALUES (?,?,?,'pending',?,?)",
              (t[0]["id"], g1, "alternative", f"{KIND}:group:{g1}:{legacy_rev}", "2026-11-09T11:00:00")); c.commit()
    monkeypatch.delenv("MMAI_ALERT_MODE", raising=False)
    sl.run_pending(c, apartment_id=a1["id"])
    assert _g(c, g2)["apartment_id"] == a2["id"] and [o["event_id"] for o in _outbox(c, g2)] == [alerts.event_id_for(KIND, "group", g2, t[0]["id"])]
    assert [o["event_id"] for o in _outbox(c, g1)] == [f"{KIND}:group:{g1}:{legacy_rev}"]   # no duplicate concession for the legacy row
    assert sl.pending_work(c) == []
    c.close(); dbm._conn = None


def test_sqlite_rebuild_rolls_back_whole_on_a_mid_rebuild_fault_and_succeeds_on_the_next_open(old_shape, monkeypatch):
    c, path = old_shape
    if getattr(c, "backend", "sqlite") == "postgres":
        pytest.skip("SQLite rebuild path")
    a1, a2, gids, legacy_rev = _legacy_state(c)
    snapshot = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("groups", "group_members", "group_apartments", "internal_alert_outbox")}
    ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='groups'").fetchone()[0]
    c.close()
    monkeypatch.undo()
    dbm._conn = None
    real_execute = dbm.Connection.execute

    def faulty(self, sql, *a, **k):
        if isinstance(sql, str) and sql.strip().upper().startswith("ALTER TABLE GROUPS_NEW RENAME"):
            raise sqlite3.OperationalError("injected fault during rebuild")
        return real_execute(self, sql, *a, **k)
    monkeypatch.setattr(dbm.Connection, "execute", faulty)
    with pytest.raises(sqlite3.OperationalError):
        dbm.connect(path)
    monkeypatch.undo()
    dbm._conn = None
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT sql FROM sqlite_master WHERE name='groups'").fetchone()[0] == ddl   # the old table is intact
    assert raw.execute("SELECT 1 FROM sqlite_master WHERE name='groups_new'").fetchone() is None
    assert {t: raw.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in snapshot} == snapshot
    assert raw.execute("SELECT 1 FROM settings WHERE key=?", (sl.BOOTSTRAP_KEY,)).fetchone() is None   # bootstrap never ran on a failed upgrade
    raw.close()
    c = dbm.connect(path)
    assert dbm._groups_fk_state(c) == (False, "SET NULL") and row(c.execute("SELECT 1 FROM settings WHERE key=?", (sl.BOOTSTRAP_KEY,)))
    assert {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in snapshot} == snapshot
    c.close(); dbm._conn = None


def test_postgres_migration_alters_in_place_and_bootstraps(old_shape, monkeypatch):
    c, path = old_shape
    if getattr(c, "backend", "sqlite") != "postgres":
        pytest.skip("PostgreSQL path")
    a1, a2, gids, legacy_rev = _legacy_state(c)
    g2 = gids[2]
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("groups", "group_members", "group_apartments", "internal_alert_outbox")}
    monkeypatch.undo()
    c2 = dbm.connect("second")   # a separate connection to the same database: schema + migration + bootstrap run for real
    assert dbm._groups_fk_state(c2) == (False, "SET NULL")
    assert {t: c2.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in counts} == counts
    t = rows(c2.execute("SELECT * FROM stock_transitions ORDER BY id"))
    assert len(t) == 1 and t[0]["id"] > legacy_rev and t[0]["cause"] == "leased"
    assert any(w["group_id"] == g2 and w["legacy_event_id"] == f"{KIND}:group:{g2}:{legacy_rev}" for w in _work(c2))
    assert sl.bootstrap(c2) == {"bootstrapped": False} and dbm._migrate_groups_apartment_fk(c2) is False
    # ON DELETE SET NULL is live: deleting the leased apartment leaves the held groups in place
    c2.execute("DELETE FROM apartments WHERE id=?", (a2["id"],)); c2.commit()
    assert row(c2.execute("SELECT COUNT(*) AS n FROM groups"))["n"] == counts["groups"]
    c2.close()
