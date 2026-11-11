"""Staff-only authority and same-step recheck requests, with rollback controls."""
import pytest

from server import ai_queue, db as dbm, exit_exceptions as xx, homebot_shortlist as hs, matching
from server.db import row, rows
from tests.test_astra_exit_authority import _world
from tests.test_homebot import client, body, post  # noqa: F401
from tests.test_homebot_shortlist import CUR, _apt, job


def _payload(aid, total="610"):
    return {"apartment_id": aid, "move_in": "2027-02-01", "move_out": "2027-08-15",
            "exception_total_weekly_rent": total}


def test_staff_proposal_is_listed_and_queues_recheck_in_same_guard(client, monkeypatch):
    aid, ids, gid = _world(client.db, monkeypatch)
    original = ai_queue.enqueue
    calls = []

    def observed(c, group_ids, **kwargs):
        assert dbm.guarded(c) and kwargs["commit"] is False
        calls.append(list(group_ids))
        return original(c, group_ids, **kwargs)

    monkeypatch.setattr(ai_queue, "enqueue", observed)
    response = client.post(f"/api/groups/{gid}/exit-exception", json=_payload(aid))
    assert response.status_code == 201
    rec = response.json()
    assert rec["readiness"]["state"] == "authorised_now" and calls == [[gid]]
    assert client.get(f"/api/groups/{gid}/exit-exceptions").json()["items"][0]["id"] == rec["id"]
    assert client.post(f"/api/groups/{gid}/exit-exception", json=_payload(aid)).status_code == 409
    assert calls == [[gid]]


def test_staff_wrong_agreement_is_nonmutating_exact_agreement_and_withdraw_work(client, monkeypatch):
    aid, ids, gid = _world(client.db, monkeypatch)
    response = client.post(f"/api/groups/{gid}/exit-exception", json=_payload(aid, "5000"))
    assert response.status_code == 201
    xid = response.json()["id"]
    before = row(client.db.execute("SELECT * FROM exit_exceptions WHERE id=?", (xid,)))
    response = client.post(f"/api/exit-exceptions/{xid}/agreement", json={"total": "4999", "agreement_scope": "Synthetic caller"})
    assert response.status_code == 422
    assert row(client.db.execute("SELECT * FROM exit_exceptions WHERE id=?", (xid,))) == before
    response = client.post(f"/api/exit-exceptions/{xid}/agreement", json={"total": "5000", "agreement_scope": "Synthetic caller"})
    assert response.status_code == 200 and response.json()["readiness"]["state"] == "authorised_now"
    response = client.post(f"/api/exit-exceptions/{xid}/withdraw")
    assert response.status_code == 200 and response.json()["readiness"]["state"] == "withdrawn"
    assert xx.authorised_offers(client.db, group_id=gid, apartment_id=aid) == ()
    assert client.post(f"/api/exit-exceptions/{xid}/withdraw").status_code == 409


def test_matebot_queue_failure_rolls_back_authority_audit_and_group_changes(client, monkeypatch):
    aid, ids, gid = _world(client.db, monkeypatch)
    group = row(client.db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    audit = rows(client.db.execute("SELECT * FROM audit_log ORDER BY id"))
    original = ai_queue.enqueue

    def fail_after_queue(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic queue failure")

    monkeypatch.setattr(ai_queue, "enqueue", fail_after_queue)
    with pytest.raises(RuntimeError, match="synthetic queue"):
        client.post(f"/api/groups/{gid}/exit-exception", json=_payload(aid))
    assert rows(client.db.execute("SELECT * FROM exit_exceptions")) == []
    assert row(client.db.execute("SELECT * FROM groups WHERE id=?", (gid,))) == group
    assert rows(client.db.execute("SELECT * FROM audit_log ORDER BY id")) == audit


def _homebot(client):
    client.db.execute("DELETE FROM apartments")
    client.db.commit()
    aid = _apt(client.db, 1, rent12=900, rent6=900, avail="2027-02-01")
    response = post(client, body(move_in="2027-02-01", move_in_flex="Exact date",
                                 move_out="2027-08-15", move_out_flex="Exact date"))
    assert response.status_code == 201
    return response.json()["id"], aid


def test_homebot_route_prepares_identity_while_off_and_requeues_when_enabled(client, monkeypatch):
    monkeypatch.setenv(hs.GATE_ENV, "0")
    eid, aid = _homebot(client)
    assert hs._current_version(client.db, eid, CUR) is None
    response = client.post(f"/api/homebot/enquiries/{eid}/exit-exception", json=_payload(aid, "950"))
    assert response.status_code == 201
    rec = response.json()
    assert xx.get(client.db, rec["id"])["readiness"]["state"] == "authorised_now"
    assert hs._current_version(client.db, eid, CUR) is not None
    assert job(client, eid) is None
    monkeypatch.setenv(hs.GATE_ENV, "1")
    response = client.post(f"/api/exit-exceptions/{rec['id']}/withdraw")
    assert response.status_code == 200
    assert job(client, eid)["status"] == hs.QUEUED and job(client, eid)["reason"] == "exit_exception"


def test_homebot_queue_failure_rolls_back_authority_and_job_generation(client, monkeypatch):
    monkeypatch.setenv(hs.GATE_ENV, "1")
    eid, aid = _homebot(client)
    before = job(client, eid)
    original = hs._ensure_job

    def fail_after_queue(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("synthetic HomeBot queue failure")

    monkeypatch.setattr(hs, "_ensure_job", fail_after_queue)
    with pytest.raises(RuntimeError, match="synthetic HomeBot"):
        client.post(f"/api/homebot/enquiries/{eid}/exit-exception", json=_payload(aid, "950"))
    assert rows(client.db.execute("SELECT * FROM exit_exceptions")) == []
    assert job(client, eid) == before


def test_match_run_booking_change_is_conflict_not_missing_apartment(client, monkeypatch):
    def changed(*args, **kwargs):
        raise matching.BookingChanged("The booking changed. Run it again.")

    monkeypatch.setattr(matching, "run_match", changed)
    response = client.post("/api/match/run", json={"apartment_id": 1})
    assert response.status_code == 409
    assert response.json()["detail"] == "The booking changed. Run it again."


def test_exception_routes_require_staff_and_reject_unknown_owner(client):
    assert client.anon.get("/api/groups/1/exit-exceptions").status_code == 401
    assert client.anon.post("/api/groups/1/exit-exception", json=_payload(1)).status_code == 401
    assert client.get("/api/groups/999999/exit-exceptions").status_code == 404
    assert client.get("/api/homebot/enquiries/999999/exit-exceptions").status_code == 404


@pytest.mark.parametrize("bad_id", [True, 1.5, -1, "not-an-id"])
def test_exception_route_rejects_non_integer_apartment_identity_without_writing(client, monkeypatch, bad_id):
    aid, ids, gid = _world(client.db, monkeypatch)
    payload = _payload(bad_id)
    response = client.post(f"/api/groups/{gid}/exit-exception", json=payload)
    assert response.status_code == 422
    assert "apartment_id" in response.json()["errors"]
    assert rows(client.db.execute("SELECT * FROM exit_exceptions")) == []


def test_withdrawing_closed_homebot_authority_does_not_restart_matching(client, monkeypatch):
    monkeypatch.setenv(hs.GATE_ENV, "0")
    eid, aid = _homebot(client)
    response = client.post(f"/api/homebot/enquiries/{eid}/exit-exception", json=_payload(aid, "950"))
    assert response.status_code == 201
    xid = response.json()["id"]
    assert client.patch(f"/api/homebot/enquiries/{eid}", json={"status": "Closed"}).status_code == 200
    monkeypatch.setenv(hs.GATE_ENV, "1")
    response = client.post(f"/api/exit-exceptions/{xid}/withdraw")
    assert response.status_code == 200 and response.json()["readiness"]["state"] == "withdrawn"
    assert job(client, eid) is None
