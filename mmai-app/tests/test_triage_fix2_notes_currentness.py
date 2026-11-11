"""Triage fix 2, currentness (9 Nov 2026): an ACCEPTED notes projection made under an earlier extraction contract (its stored
input key is no longer the cache key of the same text) must not silently stay current. Read time: is_ready refuses the group;
worker sweep: withdraw_outdated_notes -> 'pending' (packet withdrawn), and requeue_interrupted's unread path re-queues the
approved group (brief stale, not ready). No provider call; fabricated doubles (the 'Very tidy' fact is planted as the old bundle
would have stored it)."""
import json

from server import ai_queue, matching, notes_facts
from server.db import row, rows
from tests.test_t6_grounding import _run_and_approve

OLD_FACT = [{"category": "lifestyle_self_description", "strength": "stated", "value": {"field": "tidy", "answer": "Very tidy"}, "basis": notes_facts.BASIS, "source_field": "intro"}]


def _plant_old_projection(db, enquiry_id):
    """what the earlier bundle left behind: status ok, the accepted fact bound in notes_packet_json, the key of the OLD contract"""
    e = row(db.execute("SELECT * FROM enquiries WHERE id=?", (enquiry_id,)))
    old_tuple = dict(notes_facts.input_tuple({k: e.get(k) for k in notes_facts.SOURCE_FIELDS}, e.get("party_size") or 0),
                     prompt_version="notes-extract-2", catalogue_version="t4c-v2.1")
    import hashlib
    old_key = hashlib.sha256(json.dumps(old_tuple, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    db.execute("UPDATE enquiries SET notes_status='ok', notes_packet_json=?, notes_input_key=?, notes_residual=0 WHERE id=?", (json.dumps(OLD_FACT), old_key, enquiry_id))
    db.commit()
    return old_key


def test_outdated_accepted_notes_block_readiness_and_are_withdrawn(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    assert ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"]
    members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
    victim = members[0]
    _plant_old_projection(db, victim)
    # a CURRENT ok row (key under the present contract) is left alone throughout
    current = members[1]
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (current,)))["notes_status"] == "ok"
    # the stale fact is what members_for_ai would bind today ...
    assert ai_queue.outdated_notes_members(db, g["id"]) == [victim]
    # ... so the group is not ready at read time, whatever its stored review says
    rd = ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    assert not rd["ready"] and any("earlier extraction contract" in r for r in rd["reasons"])
    # worker sweep: withdrawn to pending (packet gone), group re-queued and brief stale; the current member untouched
    n = ai_queue.requeue_interrupted(db)
    assert n >= 1
    e = row(db.execute("SELECT notes_status, notes_packet_json FROM enquiries WHERE id=?", (victim,)))
    assert e["notes_status"] == "pending" and e["notes_packet_json"] is None
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (current,)))["notes_status"] == "ok"
    g2 = row(db.execute("SELECT ai_status, brief_status, ready_at FROM groups WHERE id=?", (g["id"],)))
    assert g2["ai_status"] == "pending" and g2["brief_status"] == "stale" and g2["ready_at"] is None
    # idempotent: a second sweep withdraws nothing more
    assert ai_queue.withdraw_outdated_notes(db) == 0
    # the earlier review row is history, not deleted
    assert rows(db.execute("SELECT id FROM review_rounds WHERE group_id=?", (g["id"],)))


def test_current_projection_is_never_withdrawn(db, monkeypatch):
    a, ids, g = _run_and_approve(db, monkeypatch)
    before = rows(db.execute("SELECT id, notes_status, notes_input_key FROM enquiries ORDER BY id"))
    assert ai_queue.withdraw_outdated_notes(db) == 0 and ai_queue.outdated_notes_members(db, g["id"]) == []
    assert rows(db.execute("SELECT id, notes_status, notes_input_key FROM enquiries ORDER BY id")) == before


def test_queue_off_stale_accepted_notes_never_enter_packets_or_staff_review(db, monkeypatch):
    """worker OFF (no sweep ran): the read paths alone must keep a stale accepted projection out of every packet and view"""
    from server import enquiries
    a, ids, g = _run_and_approve(db, monkeypatch)
    members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))]
    victim, current = members[0], members[1]
    _plant_old_projection(db, victim)
    db.execute("UPDATE enquiries SET notes_packet_json=? WHERE id=?", (json.dumps(OLD_FACT), current))   # same fact, CURRENT key: stays bound
    db.commit()
    e_v = row(db.execute("SELECT * FROM enquiries WHERE id=?", (victim,)))
    e_c = row(db.execute("SELECT * FROM enquiries WHERE id=?", (current,)))
    assert not notes_facts.is_current(e_v) and notes_facts.is_current(e_c)
    # the review/brief/intro packet binds NOTHING from the stale row (no sweep has run; the row still says 'ok')
    units = {int(u.id): u for u in matching.expand(matching.load_pool(db, "test"))}
    pk = matching.members_for_ai([units[victim], units[current]])
    assert pk[0]["notes_facts"] == [] and pk[1]["notes_facts"] and pk[1]["notes_facts"][0]["category"] == "lifestyle_self_description"
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (victim,)))["notes_status"] == "ok"   # read paths wrote nothing
    # the staff view of the person shows the notes as awaiting a re-read, with no item marked as in the review
    v = enquiries.notes_review_view(db, e_v)
    assert v["status"] == "pending" and v["current"] is False and not any(it["in_review"] for it in v["items"])
    assert enquiries.notes_review_view(db, e_c)["current"] is True
    # draft/read APIs: the group is refused as not ready
    assert not ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"]
    # and when the worker does reach the group, the notes gate withdraws the stale row itself before any review (single writer)
    db.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (g["id"],)); db.commit()
    assert ai_queue._notes_gate_groups(db, [row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))]) == []
    e2 = row(db.execute("SELECT notes_status, notes_packet_json FROM enquiries WHERE id=?", (victim,)))
    assert e2["notes_status"] == "pending" and e2["notes_packet_json"] is None
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (current,)))["notes_status"] == "ok"
