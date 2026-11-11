"""Astra A10 foundation: exact authority, freshness, guard and retention controls.

No live provider calls. The inherited group fixture uses a deterministic AI double.
Integration with candidate selection/queue/UI is tested separately.
"""
from datetime import date

import pytest

from server import exit_exceptions as xx
from server.db import RetentionGuardError, retention_guard, row
from tests.test_gp14_matebot_rejections import _real_group
from tests.test_data_and_api import appdb, client  # noqa: F401


def _world(db, monkeypatch):
    apartments, ids, group = _real_group(db, monkeypatch)
    aid = min(apartments)
    db.execute("UPDATE apartments SET rent_12m=600,rent_6m=675 WHERE id=?", (aid,))
    db.commit()
    return aid, ids, group["id"]


def _propose(db, gid, aid, total="610"):
    with retention_guard(db):
        return xx.propose(db, "matebot", gid, aid, "2027-02-01", "2027-08-15", total, "tester")


def test_write_without_guard_refuses_before_mutation(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    with pytest.raises(RetentionGuardError):
        xx.propose(db, "matebot", gid, aid, "2027-02-01", "2027-08-15", "610", "tester")
    assert db.execute("SELECT COUNT(*) FROM exit_exceptions").fetchone()[0] == 0


def test_held_and_withdrawn_cannot_supply_authority(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    held = _propose(db, gid, aid, "5000")
    assert held["status"] == xx.HELD
    assert held["readiness"]["state"] == "held"
    assert xx.authorised_offers(db, group_id=gid, apartment_id=aid) == ()
    with retention_guard(db):
        agreed = xx.record_agreement(db, held["id"], "tester", "customer confirmed exact total", "5000")
    assert agreed["readiness"]["state"] == "authorised_now"
    offers = xx.authorised_offers(db, group_id=gid, apartment_id=aid)
    assert len(offers) == 1 and offers[0].exception_total == 5000
    assert offers[0].move_out == date(2027, 8, 15)
    with retention_guard(db):
        withdrawn = xx.withdraw(db, held["id"], "tester")
    assert withdrawn["readiness"]["state"] == "withdrawn"
    assert xx.authorised_offers(db, group_id=gid, apartment_id=aid) == ()


def test_requirement_identity_ignores_update_timestamp_not_budget(client, monkeypatch):
    from server.group_responses import current_version
    db = appdb(client)
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid)
    assert rec["readiness"]["state"] == "authorised_now"
    version = current_version(db, gid)["id"]
    fingerprint = row(db.execute("SELECT adults_confirmed_party FROM enquiries WHERE id=?", (ids[0],)))["adults_confirmed_party"]
    response = client.patch(f"/api/enquiries/{ids[0]}",
                            json={"phone": "0411111222", "intro_sex": "Female", "intro_age": 27})
    assert response.status_code == 200
    # The real review timestamp is notes_updated_at. Enquiries has no updated_at.
    db.execute("UPDATE enquiries SET notes_updated_at=? WHERE id=?", ("2030-01-01T00:00:00+00:00", ids[0]))
    db.commit()
    fresh = xx.matebot_context(db, gid, aid, rec["move_in"], rec["move_out"])
    assert fresh["context_ref"] == rec["context_ref"]
    assert fresh["membership_hash"] == rec["membership_hash"]
    assert current_version(db, gid)["id"] == version
    assert row(db.execute("SELECT adults_confirmed_party FROM enquiries WHERE id=?", (ids[0],)))["adults_confirmed_party"] == fingerprint
    assert xx.get(db, rec["id"])["readiness"]["state"] == "authorised_now"
    db.execute("UPDATE enquiries SET budget_max=budget_max+1 WHERE id=?", (ids[0],))
    db.commit()
    assert xx.get(db, rec["id"])["readiness"] == {"state": "stale", "reason": "requirements_changed"}
    assert xx.authorised_offers(db, group_id=gid, apartment_id=aid) == ()


def test_fresh_rate_changes_not_identity_and_never_rewrite_status(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid)
    for rent, expected in ((605, "authorised_now"), (610, "not_applicable"), (599, "authorised_now")):
        db.execute("UPDATE apartments SET rent_12m=? WHERE id=?", (rent, aid))
        db.commit()
        current = xx.get(db, rec["id"])
        assert current["context_ref"] == rec["context_ref"]
        assert current["status"] == xx.PASSES
        assert current["readiness"]["state"] == expected


def test_former_member_retention_scrubs_already_withdrawn_agreement(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid, "5000")
    with retention_guard(db):
        xx.record_agreement(db, rec["id"], "tester", "private scope prose", "5000")
        xx.withdraw(db, rec["id"], "tester")
        db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, ids[0]))
        assert xx.forget_people(db, [ids[0]]) == 1
    current = xx.get(db, rec["id"])
    assert current["agreement_scope"] is None
    assert current["status"] == xx.WITHDRAWN
    assert current["withdrawal_reason"] == "staff_withdrawn"
    assert row(db.execute("SELECT COUNT(*) AS n FROM audit_log WHERE detail LIKE '%private scope prose%'"))["n"] == 0
    audit = row(db.execute("SELECT detail FROM audit_log WHERE action='exit_exception_personal_text_cleared' ORDER BY id DESC LIMIT 1"))
    assert audit["detail"] == "1 exception records scrubbed for 1 enquiries"


def test_group_deletion_cascades_authority_and_member_links(db, monkeypatch):
    from server import stock_lifecycle
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid)
    with retention_guard(db):
        stock_lifecycle.delete_groups(db, [gid])
    assert row(db.execute("SELECT id FROM exit_exceptions WHERE id=?", (rec["id"],))) is None
    assert db.execute("SELECT COUNT(*) FROM exit_exception_members WHERE exception_id=?", (rec["id"],)).fetchone()[0] == 0


def test_wrong_agreed_total_is_refused_without_consuming_write_once_agreement(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid, "5000")
    before = row(db.execute("SELECT * FROM exit_exceptions WHERE id=?", (rec["id"],)))
    with pytest.raises(xx.ExitExceptionError) as error:
        with retention_guard(db):
            xx.record_agreement(db, rec["id"], "tester", "wrong amount", "5001")
    assert error.value.status == 422
    assert row(db.execute("SELECT * FROM exit_exceptions WHERE id=?", (rec["id"],))) == before
    with retention_guard(db):
        accepted = xx.record_agreement(db, rec["id"], "tester", "exact amount", "5000")
    assert accepted["status"] == xx.PASSES


def test_effective_declaration_lapse_changes_identity_and_same_renewal_restores_it(client, monkeypatch):
    db = appdb(client)
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid)
    assert not {"adults_confirmed", "adults_confirmed_move_in"} & set(xx.EXCEPTION_IDENTITY_FIELDS)
    assert client.patch(f"/api/enquiries/{ids[0]}", json={"adults_confirmed": False}).status_code == 200
    assert xx.get(db, rec["id"])["readiness"]["state"] == "stale"
    assert client.patch(f"/api/enquiries/{ids[0]}", json={"adults_confirmed": True}).status_code == 200
    assert row(db.execute("SELECT adults_confirmed_by FROM enquiries WHERE id=?", (ids[0],)))["adults_confirmed_by"].startswith("staff:")
    renewed = xx.matebot_context(db, gid, aid, rec["move_in"], rec["move_out"])
    assert renewed["context_ref"] == rec["context_ref"]
    assert xx.get(db, rec["id"])["readiness"]["state"] == "authorised_now"


def test_persisted_authority_and_agreement_keep_exact_subcent_identity(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid, "5000.001")
    assert rec["exception_total_weekly_rent"] == "5000.001"
    with pytest.raises(xx.ExitExceptionError):
        with retention_guard(db):
            xx.record_agreement(db, rec["id"], "tester", "not the same total", "5000.002")
    assert xx.get(db, rec["id"])["agreement_total"] is None
    with retention_guard(db):
        agreed = xx.record_agreement(db, rec["id"], "tester", "exact total", "5000.001")
    assert agreed["agreement_total"] == "5000.001"
    assert agreed["readiness"]["state"] == "authorised_now"


def test_real_email_edit_changes_membership_and_stales_booking_authority(client, monkeypatch):
    from server.group_responses import current_version
    db = appdb(client)
    aid, ids, gid = _world(db, monkeypatch)
    rec = _propose(db, gid, aid)
    version = current_version(db, gid)["id"]
    response = client.patch(f"/api/enquiries/{ids[0]}", json={"email": "corrected@example.invalid"})
    assert response.status_code == 200
    assert current_version(db, gid)["id"] != version
    assert xx.get(db, rec["id"])["readiness"] == {"state": "stale", "reason": "membership_changed"}
    assert xx.authorised_offers(db, group_id=gid, apartment_id=aid) == ()
