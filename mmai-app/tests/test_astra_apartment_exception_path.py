"""Same-owner A10 authority in the per-apartment run, never inherited by a new group."""
import pytest

from server import exit_exceptions as xx, matching
from server.db import retention_guard, row, rows
from tests.test_astra_exit_authority import _propose, _world
from tests.test_astra_exception_seams import _booking


def test_apartment_run_refreshes_existing_option_with_its_own_exception(db, monkeypatch):
    aid, ids, gid, rec = _booking(db, monkeypatch)
    result = matching.run_match(db, aid, "tester", dataset="test")
    assert gid in result["groups"]
    opt = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    assert (opt["lease_end"], opt["exception_ref"], opt["exception_total"]) == ("2027-08-15", rec["context_ref"], "610")
    assert opt["weekly_rent"] == 600 and opt["total_weekly"] == 610
    assert rows(db.execute("SELECT id FROM group_apartments WHERE group_id<>? AND exception_ref IS NOT NULL", (gid,))) == []
    group = row(db.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    if group["apartment_id"] == aid:
        assert group["exception_ref"] == rec["context_ref"] and group["total_weekly"] == 610


def test_exception_only_pair_is_found_in_per_apartment_run(db, monkeypatch):
    aid, ids, gid = _world(db, monkeypatch)
    for eid in ids:
        db.execute("UPDATE enquiries SET move_out='2027-08-15',move_out_flex='Exact date' WHERE id=?", (eid,))
    db.commit()
    rec = _propose(db, gid, aid)
    apt = matching.apartment_from_row(row(db.execute("SELECT * FROM apartments WHERE id=?", (aid,))))
    pool = matching.load_pool(db, "test")
    ordinary, _ = matching.candidate_groups(apt, pool)
    assert not ordinary
    result = matching.run_match(db, aid, "tester", dataset="test")
    assert gid in result["groups"]
    opt = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    assert opt["exception_ref"] == rec["context_ref"]
    assert opt["lease_end"] == "2027-08-15"


def test_apartment_save_refuses_authority_changed_after_capture(db, monkeypatch):
    aid, ids, gid, rec = _booking(db, monkeypatch)
    original = matching.candidate_groups
    before = rows(db.execute("SELECT * FROM group_apartments ORDER BY id"))
    run_count = db.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0]

    def withdraw_after_compute(*args, **kwargs):
        result = original(*args, **kwargs)
        with retention_guard(db):
            xx.withdraw(db, rec["id"], "tester")
        return result

    monkeypatch.setattr(matching, "candidate_groups", withdraw_after_compute)
    with pytest.raises(ValueError, match="booking exception authority changed"):
        matching.run_match(db, aid, "tester", dataset="test")
    assert rows(db.execute("SELECT * FROM group_apartments ORDER BY id")) == before
    assert db.execute("SELECT COUNT(*) FROM match_runs").fetchone()[0] == run_count
    assert xx.get(db, rec["id"])["status"] == xx.WITHDRAWN


def test_new_group_writer_refuses_foreign_exception_before_any_write(db):
    count = db.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
    with retention_guard(db):
        with pytest.raises(ValueError, match="cannot inherit"):
            matching.persist_group(db, None, {"apartments": [{"exception_ref": "other-booking", "exception_total": "610"}]},
                                   "test", "apartment")
    assert db.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == count
