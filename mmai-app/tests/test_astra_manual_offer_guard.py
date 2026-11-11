"""Separate A3/GP8 manual-offer atomicity repair; no provider calls."""
import pytest

from server import apartment_rejections, matching, stock_lifecycle
from server import db as dbm
from server.db import row, rows
from tests.test_gp14_matebot_rejections import _real_group
from tests.test_data_and_api import appdb, client  # noqa: F401


def _world(db, monkeypatch):
    apartments, ids, group = _real_group(db, monkeypatch)
    target = (set(apartments) - {group["apartment_id"]}).pop()
    return group["id"], target


def _snapshot(db, gid):
    return {
        "group": row(db.execute("SELECT * FROM groups WHERE id=?", (gid,))),
        "members": rows(db.execute("SELECT * FROM group_members WHERE group_id=? ORDER BY id", (gid,))),
        "versions": rows(db.execute("SELECT * FROM group_membership_versions WHERE group_id=? ORDER BY id", (gid,))),
        "offers": rows(db.execute("SELECT * FROM audit_log WHERE action='offer_apartment' ORDER BY id")),
    }


def test_valid_manual_offer_switch_is_guarded_and_preserves_membership_evidence(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)
    evidence = matching.group_evidence(db, gid)
    original = matching._switch_current
    seen = []

    def switch(c, group_id, apartment_id):
        seen.append(dbm.guarded(c))
        return original(c, group_id, apartment_id)

    monkeypatch.setattr(matching, "_switch_current", switch)
    matching.offer_apartment(db, gid, target, "tester")
    after = _snapshot(db, gid)
    assert seen == [True]
    assert after["group"]["apartment_id"] == target
    assert after["versions"] == before["versions"]
    assert after["group"]["status"] == before["group"]["status"]
    assert matching.group_evidence(db, gid) == evidence
    assert len(after["offers"]) == len(before["offers"]) + 1


@pytest.mark.parametrize("change", ["leased", "withdrawn"])
def test_unavailable_manual_offer_refuses_without_pointer_or_audit_mutation(db, monkeypatch, change):
    gid, target = _world(db, monkeypatch)
    # Labelled stale-cache setup: availability moved but stock follow-up has not run.
    if change == "leased":
        db.execute("UPDATE apartments SET leased_at='2026-11-11T01:00:00+00:00' WHERE id=?", (target,))
    else:
        db.execute("UPDATE apartments SET available_from=NULL WHERE id=?", (target,))
    db.commit()
    before = _snapshot(db, gid)
    with pytest.raises(apartment_rejections.OfferRefused, match="no longer available"):
        matching.offer_apartment(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_changed_offer_terms_require_refresh_not_a_silent_reprice(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    db.execute("UPDATE apartments SET rent_12m=rent_12m+1,rent_6m=rent_6m+1 WHERE id=?", (target,))
    db.commit()
    before = _snapshot(db, gid)
    with pytest.raises(apartment_rejections.OfferRefused, match="current requirements and terms"):
        matching.offer_apartment(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_pending_stock_work_refuses_manual_switch(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    with dbm.retention_guard(db):
        transition = stock_lifecycle.record_transition(db, target, "withdrawn", "tester")
    assert gid in transition["group_ids"]
    before = _snapshot(db, gid)
    with pytest.raises(apartment_rejections.OfferRefused, match="still being re-checked"):
        matching.offer_apartment(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_failure_after_switch_rolls_back_the_entire_manual_step(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)
    original = matching._switch_current

    def fail_after_switch(c, group_id, apartment_id):
        assert dbm.guarded(c)
        original(c, group_id, apartment_id)
        raise RuntimeError("injected post-switch failure")

    monkeypatch.setattr(matching, "_switch_current", fail_after_switch)
    with pytest.raises(RuntimeError, match="injected"):
        matching.offer_apartment(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_private_manual_writer_refuses_without_guard(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)
    with pytest.raises(dbm.RetentionGuardError):
        matching._offer_apartment_guarded(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_lease_only_selector_refuses_without_the_lease_guard(db, monkeypatch):
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)
    with pytest.raises(dbm.RetentionGuardError, match="requires the lease guard"):
        matching._select_listed_apartment_for_lease(db, gid, target, "tester")
    assert _snapshot(db, gid) == before


def test_manual_offer_route_reports_busy_without_a_false_success(client, monkeypatch):
    db = appdb(client)
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)

    def busy(*args, **kwargs):
        raise dbm.RetentionLockTimeout("injected lock timeout")

    monkeypatch.setattr(matching, "offer_apartment", busy)
    response = client.patch(f"/api/groups/{gid}", json={"apartment_id": target})
    assert response.status_code == 503
    assert "busy finishing another update" in response.json()["detail"]
    assert _snapshot(db, gid) == before


def test_manual_offer_route_preserves_unknown_commit_outcome_warning(client, monkeypatch):
    db = appdb(client)
    gid, target = _world(db, monkeypatch)
    before = _snapshot(db, gid)

    def lost(*args, **kwargs):
        raise dbm.ConnectionLostMidOperation("injected connection loss", outcome_unknown=True)

    monkeypatch.setattr(matching, "offer_apartment", lost)
    response = client.patch(f"/api/groups/{gid}", json={"apartment_id": target})
    assert response.status_code == 503
    assert response.json()["detail"] == "The database connection was lost while saving; reload and check what was recorded before trying again."
    assert _snapshot(db, gid) == before
