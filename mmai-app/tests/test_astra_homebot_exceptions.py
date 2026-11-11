"""A10 HomeBot exact offers and captured authority; no provider calls."""
from datetime import date
from decimal import Decimal

from server import exit_exceptions as xx, homebot, homebot_match as hm, homebot_shortlist as hs
from server.db import pj, retention_guard, row, rows
from tests.test_homebot import client  # noqa: F401
from tests.test_homebot_shortlist import CUR, _apt, current, job, on, submit  # noqa: F401

START, END = date(2027, 2, 1), date(2027, 8, 15)


def _world(on, total="950"):
    db = on.db
    db.execute("DELETE FROM apartments")
    db.commit()
    aid = _apt(db, 1, rent12=900, rent6=900, avail=START.isoformat())
    eid = submit(on, move_in=START.isoformat(), move_in_flex="Exact date",
                 move_out=END.isoformat(), move_out_flex="Exact date")
    with retention_guard(db):
        record = xx.propose(db, xx.BOT_HOMEBOT, eid, aid, START.isoformat(), END.isoformat(), total,
                            "tester", scenario=CUR)
    return eid, aid, record


def _search(db, eid, aid):
    req = hs._requirements(db, homebot.get_record(db, eid), CUR)
    apt = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    exact = xx.offers_from_capture(xx.capture_for_homebot(db, eid, CUR), aid)
    return hm.search_offers(req, apt, START, exception_offers=exact)


def test_homebot_exact_exception_keeps_inventory_base_and_whole_total(on):
    eid, aid, rec = _world(on)
    req = hs._requirements(on.db, homebot.get_record(on.db, eid), CUR)
    apt = row(on.db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    assert hm.search_offers(req, apt, START).offers == ()
    offers = _search(on.db, eid, aid).affordable
    assert len(offers) == 1
    offer = offers[0]
    assert (offer.start, offer.end) == (START, END)
    assert offer.weekly_rent == Decimal("900") and offer.total_weekly_rent == Decimal("950")
    assert offer.exception_ref == rec["context_ref"]
    assert offer.as_dict()["exception_total"] == "950"
    assert offer.exception_customer_agreed is False


def test_homebot_held_never_offered_and_explicit_exact_agreement_enables_it(on):
    eid, aid, rec = _world(on, "1200")
    assert rec["status"] == xx.HELD
    assert _search(on.db, eid, aid).offers == ()
    with retention_guard(on.db):
        xx.record_agreement(on.db, rec["id"], "tester", "Synthetic customer explicitly agrees", "1200")
    offers = _search(on.db, eid, aid).affordable
    assert len(offers) == 1 and offers[0].total_weekly_rent == Decimal("1200")
    assert offers[0].exception_customer_agreed is True


def test_homebot_contact_edits_do_not_change_booking_but_position_name_swap_does(on):
    eid, aid, rec = _world(on)
    version = hs._current_version(on.db, eid, CUR)["id"]
    on.db.execute("UPDATE homebot_enquiries SET phone=?,staff_notes=?,updated_at=? WHERE id=?",
                  ("0411111222", "Staff called", "2030-01-01T00:00:00+00:00", eid))
    on.db.commit()
    fresh = xx.homebot_context(on.db, eid, aid, START.isoformat(), END.isoformat(), CUR)
    assert fresh["context_ref"] == rec["context_ref"]
    assert fresh["membership_hash"] == rec["membership_hash"]
    assert hs._current_version(on.db, eid, CUR)["id"] == version
    assert xx.get(on.db, rec["id"])["readiness"]["state"] == "authorised_now"
    people = rows(on.db.execute("SELECT position,name FROM homebot_people WHERE enquiry_id=? ORDER BY position", (eid,)))
    left, right = people[-2:]
    assert left["name"] != right["name"]
    on.db.execute("UPDATE homebot_people SET name=? WHERE enquiry_id=? AND position=?", (right["name"], eid, left["position"]))
    on.db.execute("UPDATE homebot_people SET name=? WHERE enquiry_id=? AND position=?", (left["name"], eid, right["position"]))
    on.db.commit()
    assert xx.get(on.db, rec["id"])["readiness"] == {"state": "stale", "reason": "membership_changed"}
    assert _search(on.db, eid, aid).offers == ()


def test_homebot_raw_requirement_change_is_stale_before_version_hook(on):
    eid, aid, rec = _world(on)
    version = hs._current_version(on.db, eid, CUR)["id"]
    on.db.execute("UPDATE homebot_enquiries SET budget_total_weekly=budget_total_weekly+1 WHERE id=?", (eid,))
    on.db.commit()
    assert hs._current_version(on.db, eid, CUR)["id"] == version
    assert xx.get(on.db, rec["id"])["readiness"] == {"state": "stale", "reason": "requirements_changed"}
    assert _search(on.db, eid, aid).offers == ()


def test_homebot_job_stores_exception_and_read_side_detects_withdrawal(on):
    eid, aid, rec = _world(on)
    result = hs.run_job(on.db, job(on, eid)["id"], today=START)
    assert result["outcome"] == hs.COMPLETE
    sl = current(on, eid)
    assert hs.freshness(on.db, sl) is None
    stored = pj(sl["result_json"])
    assert stored["exception_authority_hash"] == hs._hash(xx.capture_for_homebot(on.db, eid, CUR))
    assert rec["context_ref"] in sl["result_json"]
    assert '"exception_total": "950"' in sl["result_json"] or '"exception_total":"950"' in sl["result_json"]
    with retention_guard(on.db):
        xx.withdraw(on.db, rec["id"], "tester")
    assert "booking exception authority changed" in hs.freshness(on.db, sl)


def test_homebot_withdraw_during_compute_is_preserved_as_superseded_not_current(on, monkeypatch):
    eid, aid, rec = _world(on)
    original = hm.build_shortlist

    def withdraw_after_compute(*args, **kwargs):
        result = original(*args, **kwargs)
        with retention_guard(on.db):
            xx.withdraw(on.db, rec["id"], "tester")
        return result

    monkeypatch.setattr(hm, "build_shortlist", withdraw_after_compute)
    result = hs.run_job(on.db, job(on, eid)["id"], today=START)
    assert result["outcome"] == "superseded"
    assert current(on, eid) is None
    saved = row(on.db.execute("SELECT * FROM homebot_shortlists WHERE id=?", (result["shortlist_id"],)))
    assert saved["status"] == hs.SUPERSEDED and saved["is_current"] == 0
    assert job(on, eid)["status"] == hs.QUEUED


def test_homebot_exception_never_bypasses_actual_unavailability(on):
    eid, aid, rec = _world(on)
    req = hs._requirements(on.db, homebot.get_record(on.db, eid), CUR)
    apt = row(on.db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    exact = xx.offers_from_capture(xx.capture_for_homebot(on.db, eid, CUR), aid)
    assert exact
    apt["leased_at"] = "2026-11-11T00:00:00+00:00"
    candidate = hm.evaluate(req, apt, START, exception_offers=exact)
    assert candidate.klass == hm.EXCLUDED
    assert hm.LEASED in candidate.exclusion_reasons and candidate.offer is None
