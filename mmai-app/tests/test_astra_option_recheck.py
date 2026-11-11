"""Explicit staff repair after real member edits; no provider or notification delivery."""
import pytest
from fastapi.testclient import TestClient

from server import ai, ai_queue, alerts, apartment_rejections, matching, option_recheck, stock_lifecycle as sl
from server import db as dbm
from server.db import j, now_iso, row, rows
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_gp13_group_responses import _accept_all, _introduced
from tests.test_succ3_lifecycle_repairs import _introduced_with_alternative, _shelve_other_stock


def _g(c, gid):
    return row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))


def _membership(c, gid):
    return {
        "members": rows(c.execute("SELECT id,enquiry_id FROM group_members WHERE group_id=? ORDER BY id", (gid,))),
        **{t: rows(c.execute(f"SELECT * FROM {t} WHERE group_id=? ORDER BY id", (gid,)))
           for t in ("group_membership_versions", "group_response_links", "group_responses")},
    }


def _snapshot(c):
    return {t: rows(c.execute(f"SELECT * FROM {t} ORDER BY id")) for t in (
        "groups", "group_members", "group_apartments", "group_membership_versions",
        "group_response_links", "group_responses", "audit_log", "internal_alert_outbox")}


def _no_provider(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError("option recheck must not call the model")
    monkeypatch.setattr(ai, "_ask", forbidden)


def _legacy_pointer_on_declined_option(c, gid, apartment):
    """Labelled legacy state: the pointer still names an excluded row.

    Do NOT use record(): its real GP6(d) hook already repoints the group.
    This is the legacy current-pointer case explicitly handled by reconcile_work.
    """
    eid = c.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY id LIMIT 1", (gid,)).fetchone()[0]
    kind, basis = apartment_rejections._basis(c, eid, apartment, gid)
    c.execute("""INSERT INTO apartment_rejections
        (enquiry_id,scope,apartment_id,building_key,group_id,basis_kind,
         offered_basis_json,recorded_by,recorded_at,status)
        VALUES (?,'apartment',?,?,?,?,?,?,?,'active')""",
        (eid, apartment["id"], apartment_rejections.building_identity(apartment),
         gid, kind, j(basis), "legacy fixture", now_iso()))
    rid = c.execute("SELECT MAX(id) FROM apartment_rejections WHERE enquiry_id=?", (eid,)).fetchone()[0]
    apartment_rejections._event(c, rid, eid, "recorded", apartment["id"], "legacy fixture", None, None)
    apartment_rejections.refresh_exclusions(c, eid)
    c.commit()
    assert _g(c, gid)["apartment_id"] == apartment["id"]
    assert matching.excluded_option_ids(matching.evidence_rows(c, gid)) == {apartment["id"]}


@pytest.mark.parametrize("confirmed", [False, True])
def test_real_member_date_edit_rewrites_offer_preserves_acceptances_and_clock_and_requires_full_review(client, monkeypatch, confirmed):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    _accept_all(client, gid)
    if confirmed:
        r = client.patch(f"/api/groups/{gid}", json={"status": "Group confirmed"})
        assert r.status_code == 200, r.text
    before = _membership(c, gid)
    assert before["group_responses"] and before["group_membership_versions"][-1]["introduced_at"]
    old = _g(c, gid)
    r = client.patch(f"/api/enquiries/{lead['id']}", json={
        "move_in": "2027-02-02", "move_in_flex": "Exact date", "adults_confirmed": True})
    assert r.status_code == 200, r.text
    assert not ai_queue.can_circulate(c, _g(c, gid))["ok"]
    _no_provider(monkeypatch)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 200, r.text
    assert r.json()["rewritten"] >= 1 and r.json()["full_review_queued"] is True
    now = _g(c, gid)
    assert now["apartment_id"] == old["apartment_id"] and now["lease_start"] == "2027-02-02"
    assert now["status"] == old["status"] and now["offer_changed_since_intro_at"]
    assert _membership(c, gid) == before
    assert now["ai_status"] == "pending" and now["ai_refresh"] == 0
    assert all(now[k] is None for k in ("ready_at", "review_verdict", "brief_status", "evidence_hash", "evidence_json"))
    assert not ai_queue.can_circulate(c, now)["ok"]
    assert client.get(f"/api/groups/{gid}").json()["intro"]["current"] is False
    # The former staff dead end is repaired: fresh terms may be selected, but not circulated before review.
    offered = client.patch(f"/api/groups/{gid}", json={"apartment_id": now["apartment_id"]})
    assert offered.status_code == 200, offered.text
    assert not ai_queue.can_circulate(c, _g(c, gid))["ok"]


def test_declined_current_option_moves_to_valid_listed_option_and_alert_is_not_repeated(client, monkeypatch):
    c, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    _legacy_pointer_on_declined_option(c, gid, a)
    before = _membership(c, gid)
    _no_provider(monkeypatch)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 200, r.text
    assert r.json()["apartment_id"] == alt["id"] and r.json()["removed"] == 1
    assert _g(c, gid)["offer_changed_since_intro_at"] and _membership(c, gid) == before
    events = rows(c.execute("SELECT * FROM internal_alert_outbox WHERE kind=? AND record_id=?", (option_recheck.ALERT_KIND, gid)))
    assert len(events) == 1 and events[0]["status"] == "held"
    assert "no longer suits the group" in r.json()["message"]
    assert client.post(f"/api/groups/{gid}/recheck-options").status_code == 200
    assert rows(c.execute("SELECT * FROM internal_alert_outbox WHERE kind=? AND record_id=?", (option_recheck.ALERT_KIND, gid))) == events


def test_real_member_size_requirement_edit_holds_without_offer_or_model_and_preserves_people(client, monkeypatch):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    current = _g(c, gid)["apartment_id"]
    _shelve_other_stock(c, [current])
    before = _membership(c, gid)
    r = client.patch(f"/api/enquiries/{lead['id']}", json={"apt_sizes": ["Studio"]})
    assert r.status_code == 200, r.text
    _no_provider(monkeypatch)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 200, r.text
    assert r.json()["apartment_id"] is None and r.json()["full_review_queued"] is False
    g = _g(c, gid)
    assert all(g[k] is None for k in ("apartment_id", "lease_start", "lease_end", "total_weekly", "allocation_json", "ready_at"))
    assert g["ai_status"] == "failed" and g["held_reason"] == ai_queue.NO_SUITABLE_OFFER_TEXT
    assert ai_queue.is_closed_failure_text(g["ai_error"])
    assert _membership(c, gid) == before and not ai_queue.can_circulate(c, g)["ok"]
    with dbm.retention_guard(c):
        assert ai_queue._payload(c, g) is None


def test_empty_cached_list_recovers_existing_available_stock_and_copies_terms(client, monkeypatch):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    current = _g(c, gid)["apartment_id"]
    _shelve_other_stock(c, [current])
    # Labelled legacy missing-cache fixture, not a production deletion path.
    c.execute("DELETE FROM group_apartments WHERE group_id=?", (gid,))
    c.execute("UPDATE groups SET total_weekly=1 WHERE id=?", (gid,))
    c.commit()
    before = _membership(c, gid)
    _no_provider(monkeypatch)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 200, r.text
    assert r.json()["restocked"] and r.json()["apartment_id"] == current
    option = row(c.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, current)))
    assert _g(c, gid)["total_weekly"] == option["total_weekly"] > 1
    assert _g(c, gid)["offer_changed_since_intro_at"] and _membership(c, gid) == before


def test_pending_stock_work_refuses_recheck_without_writes(client, monkeypatch):
    c, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    with dbm.retention_guard(c):
        sl.record_transition(c, a["id"], "changed", "tester", changed_fields=["rent_6m"])
    before = _snapshot(c)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 409 and "still being re-checked" in r.json()["detail"]
    assert _snapshot(c) == before


def test_unjudgeable_second_option_rolls_back_first_rewrite(client, monkeypatch):
    c, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    c.execute("UPDATE apartments SET rent_6m=rent_6m+1,rent_12m=rent_12m+1 WHERE id=?", (a["id"],))
    c.commit()
    before = _snapshot(c)
    original = sl._option_validity
    seen = []
    def check(conn, apt, option, units):
        assert dbm.guarded(conn)
        seen.append(option["apartment_id"])
        if len(seen) == 2:
            raise sl.OptionsNotJudgeable("injected unavailable rule check")
        verdict = original(conn, apt, option, units)
        assert verdict[0] == "rewrite"
        return verdict
    monkeypatch.setattr(sl, "_option_validity", check)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 409 and "Nothing was changed" in r.json()["detail"]
    assert len(seen) == 2 and _snapshot(c) == before


def test_full_review_queue_failure_rolls_back_all_writes(client, monkeypatch):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    before = _snapshot(c)
    original = ai_queue.enqueue
    def fail(conn, group_ids, **kwargs):
        assert dbm.guarded(conn) and kwargs == {"refresh": False, "commit": False}
        original(conn, group_ids, **kwargs)
        raise RuntimeError("injected queue failure")
    monkeypatch.setattr(ai_queue, "enqueue", fail)
    with pytest.raises(RuntimeError, match="injected queue failure"):
        option_recheck.recheck(c, gid, "tester")
    assert _snapshot(c) == before


def test_attention_failure_rolls_back_loss_switch_and_queue(client, monkeypatch):
    c, gid, a, alt = _introduced_with_alternative(client, monkeypatch)
    _legacy_pointer_on_declined_option(c, gid, a)
    before = _snapshot(c)
    def fail(conn, kind, record_id, **kwargs):
        assert dbm.guarded(conn) and kind == option_recheck.ALERT_KIND and kwargs["in_transaction"]
        return {"status": "error"}
    monkeypatch.setattr(alerts, "raise_attention", fail)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 503 and "Nothing was changed" in r.json()["detail"]
    assert _snapshot(c) == before


@pytest.mark.parametrize("status", ["Leased", "Dissolved", "Awaiting replacement"])
def test_closed_or_replacement_workflow_is_not_changed_by_recheck(client, monkeypatch, status):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    c.execute("UPDATE groups SET status=? WHERE id=?", (status, gid))
    c.commit()
    before = _snapshot(c)
    assert client.post(f"/api/groups/{gid}/recheck-options").status_code == 409
    assert _snapshot(c) == before


def test_recheck_requires_staff_and_missing_group_is_404(client):
    anon = TestClient(client.app)
    assert anon.post("/api/groups/999999/recheck-options").status_code == 401
    assert client.post("/api/groups/999999/recheck-options").status_code == 404


def test_post_commit_wake_and_guard_failure_use_existing_truthful_handler(client, monkeypatch):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    wakes = []
    monkeypatch.setattr(ai_queue, "kick", lambda: wakes.append(dbm.guarded(c)))
    assert client.post(f"/api/groups/{gid}/recheck-options").status_code == 200
    assert wakes == [True, False]
    before = _snapshot(c)
    def lost(*a, **kw):
        raise dbm.ConnectionLostMidOperation("test lost connection", outcome_unknown=True)
    monkeypatch.setattr(option_recheck, "recheck", lost)
    r = client.post(f"/api/groups/{gid}/recheck-options")
    assert r.status_code == 503
    assert r.json()["detail"] == "The database connection was lost while saving; reload and check what was recorded before trying again."
    assert _snapshot(c) == before


def test_same_people_copy_refresh_keeps_row_ids_and_first_lead_occurrence(client, monkeypatch):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    before = _membership(c, gid)
    ids = [m["enquiry_id"] for m in before["members"]]
    allocation = [{"room": 2, "occupants": [
        {"person_id": str(eid), "share": 121 + i, "bed": "single", "arrangement": ["updated", "copy"]}
        for i, eid in enumerate(ids)
    ]}]
    # Duplicate lead and non-lead occupants must keep the old first-wins/lead-only semantics.
    allocation[0]["occupants"] += [
        {"person_id": str(ids[0]), "share": 999, "bed": "ignored", "arrangement": []},
        {"person_id": f"{ids[0]}m1", "share": 888, "bed": "ignored", "arrangement": []},
    ]
    with dbm.retention_guard(c):
        matching._write_members(c, gid, allocation)
    assert _membership(c, gid) == before
    copies = rows(c.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY id", (gid,)))
    assert [m["weekly_share"] for m in copies] == [121 + i for i in range(len(ids))]
    assert all(m["room"] == 2 and m["bed"] == "single" and m["arrangement"] == "updated, copy" for m in copies)


@pytest.mark.parametrize("keep", [0, 1])
def test_different_people_copy_path_retains_original_delete_and_insert(client, monkeypatch, keep):
    c, lead, solo, gid = _introduced(client, monkeypatch)
    before = rows(c.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY id", (gid,)))
    allocation = [{"room": 1, "occupants": [
        {"person_id": str(m["enquiry_id"]), "share": 200, "bed": "single", "arrangement": []}
        for m in before
    ]}]
    # Low-level writer contract, not a staff membership-change workflow.
    selected = [{"room": 1, "occupants": allocation[0]["occupants"][:keep]}]
    with dbm.retention_guard(c):
        matching._write_members(c, gid, selected)
    changed = rows(c.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY id", (gid,)))
    assert len(changed) == keep
    assert [m["enquiry_id"] for m in changed] == [m["enquiry_id"] for m in before[:keep]]
    assert not {m["id"] for m in changed} & {m["id"] for m in before}
    with dbm.retention_guard(c):
        matching._write_members(c, gid, allocation)
    restored = rows(c.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY id", (gid,)))
    assert [m["enquiry_id"] for m in restored] == [m["enquiry_id"] for m in before]
    assert not {m["id"] for m in restored} & {m["id"] for m in before + changed}
    assert all(m["weekly_share"] == 200 and m["room"] == 1 and m["bed"] == "single" and m["arrangement"] == "" for m in restored)
