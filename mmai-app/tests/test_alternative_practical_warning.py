"""Every ranked option uses the existing essential-need gate, without AI calls."""
from pathlib import Path

from server import access_facts, ai_queue, app
from server.db import row, rows
from tests.test_launch_neutral import _apt
from tests.test_t3d_access_facts import _held_for_step_free


def test_alternative_access_warning_uses_current_bound_record(db, monkeypatch):
    gid, aid, _, other_gid = _held_for_step_free(db, monkeypatch)
    alternative = _apt(db, address="2 Synthetic Road")
    offer = row(db.execute("SELECT * FROM group_apartments WHERE group_id=? AND apartment_id=?", (gid, aid)))
    offer.pop("id", None)
    offer.update(apartment_id=alternative["id"], rank=2)
    db.execute(f"INSERT INTO group_apartments ({','.join(offer)}) VALUES ({','.join('?' for _ in offer)})", list(offer.values()))
    access_facts.record(db, aid, "yes", source="synthetic verification", staff="tester")
    db.commit()
    monkeypatch.setattr(app, "db", db)
    binding_calls = []
    bind = ai_queue.essential_need_members

    def counted(connection, group_id):
        binding_calls.append(group_id)
        return bind(connection, group_id)

    monkeypatch.setattr(ai_queue, "essential_need_members", counted)

    def view(group_id=gid):
        return app._group_out(row(db.execute("SELECT * FROM groups WHERE id=?", (group_id,))), circ={"ok": False})

    original = rows(db.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,)))
    out = view()
    assert binding_calls == [gid]  # one bind for all ranked options
    options = {a["apartment_id"]: a for a in out["apartments"]}
    assert options[aid]["practical_need_hold"] is False
    assert options[alternative["id"]]["practical_need_hold"] is True
    assert "Step-free" in options[alternative["id"]]["practical_need_reason"]
    assert out["next_best"] is None
    assert rows(db.execute("SELECT * FROM group_apartments WHERE group_id=? ORDER BY rank", (gid,))) == original
    assert all(not a["practical_need_hold"] for a in view(other_gid)["apartments"])

    access_facts.record(db, alternative["id"], "no", source="synthetic verification", staff="tester")
    assert "not step-free" in view()["apartments"][1]["practical_need_reason"]
    access_facts.record(db, alternative["id"], "yes", source="synthetic verification", staff="tester")
    assert view()["next_best"]["apartment_id"] == alternative["id"]
    db.execute("UPDATE apartments SET address='3 Different Road' WHERE id=?", (alternative["id"],))
    db.commit()
    assert view()["apartments"][1]["practical_need_hold"] is True
    assert view()["next_best"] is None


def test_staff_warns_and_preserves_selection_for_review():
    client = Path(__file__).parents[1] / "client"
    text = (client / "staff.js").read_text()
    assert "Not a confirmed suitable option." in text
    assert "practicalHoldHtml(a)" in text and "practicalHoldHtml(top)" in text
    assert "esc(a.practical_need_reason" in text
    assert "!a.practical_need_hold &&" not in text
    assert "Select for staff review" in text
    assert "Selecting it keeps the group held for staff review." in text
    assert "Other apartment candidates" in text
    assert "${apts.length} suitable apartment" not in text
    assert "${apts.length} apartment candidate" in text
    assert "<br>Selecting it keeps the group held" in text
    assert "staff.js?v=20261123b" in (client / "index.html").read_text()
