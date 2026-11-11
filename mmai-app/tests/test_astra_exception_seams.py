"""A10 MateBot composition controls; deterministic inherited AI double, no providers."""
import json

from server import ai_queue, apartment_rejections, exit_exceptions as xx, facts, matching, stock_lifecycle
from server.db import retention_guard, row, rows
from tests.test_astra_exit_authority import _world, _propose


def _booking(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    for eid in ids:
        db.execute("UPDATE enquiries SET move_out='2027-08-15',move_out_flex='± 1 month' WHERE id=?", (eid,))
    db.commit()
    record = _propose(db, gid, aid)
    return aid, ids, gid, record


def _group(db, gid):
    return row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))


def _units(db, ids):
    return [matching.unit_from_row(row(db.execute("SELECT * FROM enquiries WHERE id=?", (eid,))),
                                  rows(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id", (eid,))))
            for eid in ids]


def test_full_run_recomputes_existing_option_without_transferring_group_authority(db, monkeypatch):
    aid, ids, gid, record = _booking(db, monkeypatch)
    before = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    assert before["exception_ref"] is None
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    after = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    assert after is not None
    assert after["lease_end"] == "2027-08-15"
    assert after["exception_ref"] == record["context_ref"]
    assert after["exception_total"] == "610"
    assert after["weekly_rent"] == 600 and after["total_weekly"] == 610
    others = rows(db.execute("SELECT * FROM group_apartments WHERE group_id<>? AND exception_ref IS NOT NULL", (gid,)))
    assert others == []
    current = _group(db, gid)
    if current["apartment_id"] == aid:
        assert (current["exception_ref"], current["exception_total"]) == (record["context_ref"], "610")


def test_queue_capture_pins_exact_authority_and_exposes_no_agreement_prose(db, monkeypatch):
    aid, ids, gid, record = _booking(db, monkeypatch)
    with retention_guard(db):
        xx.withdraw(db, record["id"], "tester")
        record = xx.propose(db, "matebot", gid, aid, "2027-02-01", "2027-08-15", "5000", "tester")
        xx.record_agreement(db, record["id"], "tester", "NEVER SEND THIS PRIVATE AGREEMENT", "5000")
        capture = matching.evidence_rows(db, gid)
        packet = ai_queue._payload(db, _group(db, gid), capture)
        assert packet is not None
        offer = next(a for a in packet["apartments"] if a["apartment_id"] == aid)
        assert offer["total_weekly"] == 5000
        assert offer["exit_exception"]["total_weekly"] == "5000"
        assert offer["exit_exception"]["customer_agreement_recorded"] is True
        assert "NEVER SEND" not in json.dumps(packet)
        assert "tester" not in json.dumps(packet)
        catalogue = facts.catalogue(packet)
        assert catalogue[f"R.A{aid}.exit_exception"]["source"] == "current booking-specific staff authority"
        assert packet["evidence_hash"] == matching.evidence_from_rows(capture)[0]
        assert ai_queue._hard_rules_still_pass(db, _group(db, gid))[0] is True
        old_hash = packet["evidence_hash"]
        xx.withdraw(db, record["id"], "tester")
        assert matching.group_evidence(db, gid)[0] != old_hash
        assert matching.evidence_from_rows(capture)[0] == old_hash
        assert ai_queue._hard_rules_still_pass(db, _group(db, gid))[0] is False


def test_stock_recheck_rewrites_stale_exception_to_ordinary_terms(db, monkeypatch):
    aid, ids, gid, record = _booking(db, monkeypatch)
    with retention_guard(db):
        packet = ai_queue._payload(db, _group(db, gid))
        assert packet is not None
        option = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
        apartment = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
        units = _units(db, ids)
        assert stock_lifecycle._option_validity(db, apartment, option, units) == ("ok", None)
        xx.withdraw(db, record["id"], "tester")
        verdict, check = stock_lifecycle._option_validity(db, apartment, option, units)
        assert verdict == "rewrite" and check.exception_ref is None
        stock_lifecycle._rewrite_option(db, option, apartment, units, check)
        fresh = row(db.execute("SELECT * FROM group_apartments WHERE id=?", (option["id"],)))
        assert fresh["exception_ref"] is None and fresh["exception_total"] is None
        assert fresh["lease_end"] == "2027-07-20"


def test_approved_exception_never_overrides_member_apartment_decline(db, monkeypatch):
    aid, ids, gid, record = _booking(db, monkeypatch)
    apartment_rejections.record(db, ids[0], "apartment", aid, "tester")
    units = _units(db, ids)
    apartment = row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,)))
    entry = {"units": units, "apartments": [],
             "exception_offers": {aid: xx.authorised_offers(db, group_id=gid, apartment_id=aid)}}
    matching._complete_options(entry, [(apartment, matching.apartment_from_row(apartment))])
    assert entry["apartments"] == []
    capture = matching.evidence_rows(db, gid)
    assert matching.effective_exception_records(capture) == []
    matching.full_run(db, "tester", dataset="test", use_ai=False)
    assert _group(db, gid)["status"] == "Proposed"
    assert row(db.execute("SELECT id FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid))) is None
    assert db.execute("""SELECT COUNT(*) FROM group_apartments ga JOIN group_members gm ON gm.group_id=ga.group_id
                         WHERE gm.enquiry_id=? AND ga.apartment_id=?""", (ids[0], aid)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM group_apartments WHERE apartment_id=? AND exception_ref IS NOT NULL",
                      (aid,)).fetchone()[0] == 0


def test_candidate_save_refuses_authority_change_since_computation(db, monkeypatch):
    aid, ids, gid, record = _booking(db, monkeypatch)
    entry = {"key": _group(db, gid)["member_key"], "_exception_owner": gid,
             "_exception_basis": xx.capture_for_group(db, gid)}
    with retention_guard(db):
        xx.withdraw(db, record["id"], "tester")
        assert matching._candidate_stale_reason(db, entry, 1, set(), 10) == "booking exception authority changed during the run"
