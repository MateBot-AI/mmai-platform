"""Authority retention follows immutable people links and the existing deletion guard."""
from datetime import date

import pytest

from server import db as dbm, enquiries, exit_exceptions as xx, homebot, homebot_shortlist as hs, submissions
from server.db import RetentionGuardError, retention_guard, row, rows
from tests.test_astra_exit_authority import _world, _propose
from tests.test_homebot import client  # noqa: F401
from tests.test_astra_exception_routes import _homebot, _payload


def _agreed(c, monkeypatch):
    aid, ids, gid = _world(c, monkeypatch)
    rec = _propose(c, gid, aid, "5000")
    with retention_guard(c):
        rec = xx.record_agreement(c, rec["id"], "tester", "Synthetic personal agreement detail", "5000")
    return aid, ids, gid, rec


def _stored(c, xid):
    return row(c.execute("SELECT * FROM exit_exceptions WHERE id=?", (xid,)))


def test_actual_member_delete_scrubs_authority_without_erasing_lease_amounts(client, monkeypatch):
    aid, ids, gid, rec = _agreed(client.db, monkeypatch)
    response = client.delete(f"/api/enquiries/{ids[0]}")
    assert response.status_code == 200
    assert response.json()["generated_text_removed"]["exception_records_scrubbed"] == 1
    after = _stored(client.db, rec["id"])
    assert after is not None and after["status"] == xx.WITHDRAWN and after["agreement_scope"] is None
    assert after["withdrawal_reason"] == "member_removed"
    for key in ("agreement_total", "exception_total_weekly_rent", "move_in", "move_out"):
        assert after[key] == rec[key]
    assert xx.get(client.db, rec["id"])["readiness"]["state"] == "withdrawn"


def test_former_member_delete_follows_original_link_and_preserves_withdrawal_reason(client, monkeypatch):
    aid, ids, gid, rec = _agreed(client.db, monkeypatch)
    with retention_guard(client.db):
        xx.withdraw(client.db, rec["id"], "tester")
        # Labelled historical-membership fixture: the person left before their later deletion.
        client.db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (gid, ids[0]))
        remaining = rows(client.db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (gid,)))
        client.db.execute("UPDATE groups SET member_key=? WHERE id=?", (",".join(str(r["enquiry_id"]) for r in remaining), gid))
    response = client.delete(f"/api/enquiries/{ids[0]}")
    assert response.status_code == 200
    after = _stored(client.db, rec["id"])
    assert after["agreement_scope"] is None and after["withdrawal_reason"] == "staff_withdrawn"
    assert row(client.db.execute("SELECT 1 FROM exit_exception_members WHERE exception_id=? AND enquiry_id=?",
                                 (rec["id"], ids[0]))) is not None


def test_retention_authority_scrub_is_idempotent_and_counts_only(client, monkeypatch):
    aid, ids, gid, rec = _agreed(client.db, monkeypatch)
    with retention_guard(client.db):
        assert xx.forget_people(client.db, ids) == 1
    before = _stored(client.db, rec["id"])
    audits = rows(client.db.execute("SELECT * FROM audit_log ORDER BY id"))
    with retention_guard(client.db):
        assert xx.forget_people(client.db, ids) == 0
    assert _stored(client.db, rec["id"]) == before
    assert rows(client.db.execute("SELECT * FROM audit_log ORDER BY id")) == audits
    detail = rows(client.db.execute("SELECT detail FROM audit_log WHERE action='exit_exception_personal_text_cleared'"))
    assert detail == [{"detail": f"1 exception records scrubbed for {len(set(ids))} enquiries"}]
    assert "Synthetic personal" not in detail[0]["detail"]


def test_unguarded_retention_with_authority_refuses_before_any_cleanup_write(client, monkeypatch):
    aid, ids, gid, rec = _agreed(client.db, monkeypatch)
    before = _stored(client.db, rec["id"])
    audits = rows(client.db.execute("SELECT * FROM audit_log ORDER BY id"))
    with pytest.raises(RetentionGuardError):
        enquiries.forget_generated_text(client.db, [ids[0]])
    assert not client.db.in_transaction
    assert _stored(client.db, rec["id"]) == before
    assert rows(client.db.execute("SELECT * FROM audit_log ORDER BY id")) == audits


def test_later_delete_failure_rolls_back_authority_scrub_and_audit(client, monkeypatch):
    aid, ids, gid, rec = _agreed(client.db, monkeypatch)
    before = _stored(client.db, rec["id"])
    audits = rows(client.db.execute("SELECT * FROM audit_log ORDER BY id"))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic late delete failure")

    monkeypatch.setattr(submissions, "tombstone", fail)
    with pytest.raises(RuntimeError, match="synthetic late delete"):
        client.delete(f"/api/enquiries/{ids[0]}")
    assert _stored(client.db, rec["id"]) == before
    assert rows(client.db.execute("SELECT * FROM audit_log ORDER BY id")) == audits
    assert row(client.db.execute("SELECT id FROM enquiries WHERE id=?", (ids[0],))) is not None


@pytest.mark.parametrize("operation", ["delete", "purge"])
def test_homebot_deletion_uses_retention_guard_and_cascades_authority(client, monkeypatch, operation):
    monkeypatch.setenv(hs.GATE_ENV, "0")
    eid, aid = _homebot(client)
    response = client.post(f"/api/homebot/enquiries/{eid}/exit-exception", json=_payload(aid, "950"))
    assert response.status_code == 201
    xid = response.json()["id"]
    original = hs.forget
    seen = []

    def observed(c, ids):
        assert dbm.guarded(c)
        seen.append(list(ids))
        return original(c, ids)

    monkeypatch.setattr(hs, "forget", observed)
    if operation == "delete":
        assert client.delete(f"/api/homebot/enquiries/{eid}").status_code == 200
    else:
        assert homebot.purge_expired(client.db, today=date(2030, 1, 1)) == 1
    assert seen == [[eid]]
    assert _stored(client.db, xid) is None
    assert row(client.db.execute("SELECT id FROM homebot_enquiries WHERE id=?", (eid,))) is None


@pytest.mark.parametrize("operation", ["delete", "purge"])
def test_homebot_late_audit_failure_rolls_back_enquiry_and_authority(client, monkeypatch, operation):
    monkeypatch.setenv(hs.GATE_ENV, "0")
    eid, aid = _homebot(client)
    response = client.post(f"/api/homebot/enquiries/{eid}/exit-exception", json=_payload(aid, "950"))
    assert response.status_code == 201
    xid = response.json()["id"]
    before = _stored(client.db, xid)
    audits = rows(client.db.execute("SELECT * FROM audit_log ORDER BY id"))

    def fail(*args, **kwargs):
        raise RuntimeError("synthetic final HomeBot audit failure")

    monkeypatch.setattr(homebot, "audit", fail)
    with pytest.raises(RuntimeError, match="synthetic final HomeBot"):
        if operation == "delete":
            client.delete(f"/api/homebot/enquiries/{eid}")
        else:
            homebot.purge_expired(client.db, today=date(2030, 1, 1))
    assert _stored(client.db, xid) == before
    assert row(client.db.execute("SELECT id FROM homebot_enquiries WHERE id=?", (eid,))) is not None
    assert rows(client.db.execute("SELECT * FROM audit_log ORDER BY id")) == audits
