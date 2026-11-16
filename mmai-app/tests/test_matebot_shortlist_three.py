"""GP6(d) for MateBot staff output: at most three of a group's stored apartments are its shortlist. Every stored candidate
stays listed in its stored order; nothing is deleted, re-ranked or written. Travel is checked live and last (tests/test_matebot_live.py); the
stored list never carries a travel result and the view says the candidates are provisional. No AI call is made."""
from pathlib import Path

from server import access_facts, apartment_rejections, app, matebot_travel
from server.db import row, rows
from tests.test_launch_neutral import _apt
from tests.test_t3d_access_facts import _held_for_step_free

STAFF_JS = (Path(__file__).resolve().parents[1] / "client" / "staff.js").read_text(encoding="utf-8")


def _six(db, monkeypatch):
    gid, aid, _, _ = _held_for_step_free(db, monkeypatch)
    offer = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    offer.pop("id", None)
    ids = [aid]
    for rank in range(2, 7):
        extra = _apt(db, address=f"{rank} Synthetic Road")
        offer.update(apartment_id=extra["id"], rank=rank)
        db.execute(f"INSERT INTO group_apartments ({','.join(offer)}) VALUES ({','.join('?' for _ in offer)})", list(offer.values()))
        ids.append(extra["id"])
    db.commit()
    monkeypatch.setattr(app, "db", db)
    return gid, ids


def _view(db, gid):
    return app._group_out(row(db.execute("SELECT * FROM groups WHERE id=?", (gid,))), circ={"ok": False})


def test_three_are_shortlisted_the_rest_are_reserves_and_nothing_is_written(db, monkeypatch):
    gid, ids = _six(db, monkeypatch)
    before = rows(db.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,)))
    audits = db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    out = _view(db, gid)
    assert [a["apartment_id"] for a in out["apartments"]] == ids                       # all six still listed, stored order
    assert [a["shortlist_role"] for a in out["apartments"]] == ["shortlist"] * 3 + ["reserve"] * 3
    assert out["shortlist_max"] == 3 == matebot_travel.MAX_SHORTLIST and out["travel_check"] == "off" and out["travel_live"]["enabled"] is False
    assert rows(db.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))) == before
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == audits


def test_a_reserve_moves_up_when_a_shortlisted_apartment_is_leased_to_another_group(db, monkeypatch):
    gid, ids = _six(db, monkeypatch)
    # GP8: an apartment is taken only when reservations marked it Leased
    db.execute("UPDATE apartments SET leased_at='2026-11-16T00:00:00+00:00', leased_group_id=?, leased_by='tester' WHERE id=?", (gid + 1000, ids[1]))
    db.commit()
    out = _view(db, gid)
    by = {a["apartment_id"]: a for a in out["apartments"]}
    assert by[ids[1]]["taken_by"] and by[ids[1]]["shortlist_role"] == "unavailable"    # stays listed as history, takes no place
    assert [a["shortlist_role"] for a in out["apartments"]] == ["shortlist", "unavailable", "shortlist", "shortlist", "reserve", "reserve"]


def test_the_staff_page_labels_reserves_offers_only_shortlisted_ones_and_does_not_imply_a_travel_result():
    assert "a.shortlist_role !== 'reserve' &&" in STAFF_JS                             # no offer button on a reserve
    assert 'data-role="apartment-reserve"' in STAFF_JS and 'data-role="shortlist-limit"' in STAFF_JS
    assert 'data-role="travel-provisional"' in STAFF_JS and "they are candidates and not a confirmed shortlist" in STAFF_JS
    assert "Other apartment candidates" in STAFF_JS                                    # the existing section, not a new workflow
    for gone in ("km to campus is within", "travel check passed", "within travel time"):
        assert gone not in STAFF_JS


def test_next_best_is_never_a_reserve_a_declined_or_a_leased_apartment_and_the_declines_are_read_once(db, monkeypatch):
    gid, ids = _six(db, monkeypatch)
    member = db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (gid,)).fetchone()[0]
    calls = []
    real = apartment_rejections.declined_in_group

    def declined(c, group_id):
        calls.append(group_id)
        return {**real(c, group_id), ids[1]: [{"name": "Synthetic", "at": "2026-11-16T00:00:00+00:00", "scope": "apartment"}]}

    monkeypatch.setattr(apartment_rejections, "declined_in_group", declined)
    out = _view(db, gid)
    assert calls == [gid] and member                                                   # one read per group view
    assert out["declined_apartments"][str(ids[1])][0]["name"] == "Synthetic"
    roles = {a["apartment_id"]: a["shortlist_role"] for a in out["apartments"]}
    assert roles[ids[1]] == "unavailable" and [roles[i] for i in ids] == ["shortlist", "unavailable", "shortlist", "shortlist", "reserve", "reserve"]
    # every alternative is still held for the group's essential need, so none is named
    assert out["next_best"] is None
    # the declined apartment and a reserve become confirmed suitable: still neither is named as the next suggestion
    for aid in (ids[1], ids[4]):
        access_facts.record(db, aid, "yes", source="synthetic verification", staff="tester")
    db.commit()
    assert _view(db, gid)["next_best"] is None
    # a shortlisted one becomes confirmed suitable: that one is named
    access_facts.record(db, ids[3], "yes", source="synthetic verification", staff="tester")
    db.commit()
    again = _view(db, gid)
    assert again["next_best"]["apartment_id"] == ids[3] and again["next_best"]["shortlist_role"] == "shortlist"
