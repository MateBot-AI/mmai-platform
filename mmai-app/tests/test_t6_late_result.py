"""T5 late-result guard: generated text about a person who was deleted while the model was working must be DROPPED before any
history row is written - for review rounds, staff briefs and introduction drafts alike. Deterministic (no threads): the
deletion happens inside the patched model call, so the result arrives after the person is gone. The member_key is left
untouched on purpose (the stored key alone does not reveal a deleted enquiry row), which is exactly the gap this guards."""
import pytest

from server import ai, ai_queue, matching
from server.db import rows, row
from tests.conftest import fake_staff_briefs
from tests.test_launch_neutral import _apt, _pool
from tests.test_t6_fail_closed import _reply_builder, _good


def _primary(db):
    a = _apt(db)
    _pool(db)
    matching.run_match(db, a["id"], "tester", dataset="test")
    g = row(db.execute("SELECT * FROM groups WHERE is_primary=1"))
    members = [r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id", (g["id"],)))]
    assert len(members) == 2
    return g, members


def _delete_enquiry_row_only(db, eid):
    """Simulates the reviewer's deletion landing mid-flight: the enquiry row goes, the group's member_key is NOT touched."""
    db.execute("DELETE FROM party_members WHERE enquiry_id=?", (eid,))
    db.execute("DELETE FROM enquiries WHERE id=?", (eid,))
    db.commit()


def test_people_intact_reads_rows_not_member_key(db):
    g, members = _primary(db)
    refs = {str(members[0]): "U1", str(members[1]): "U2"}
    assert ai_queue.people_intact(db, g["id"], refs)
    assert not ai_queue.people_intact(db, g["id"], {})                                     # nobody described: never "intact"
    assert not ai_queue.people_intact(db, g["id"], {str(members[0]): "U1"})                # membership differs from the output
    assert not ai_queue.people_intact(db, g["id"], {**refs, f"{members[0]}m1": "U1-m1"})   # party member slot that does not exist
    _delete_enquiry_row_only(db, members[1])
    assert row(db.execute("SELECT member_key FROM groups WHERE id=?", (g["id"],)))["member_key"] == g["member_key"]
    assert not ai_queue.people_intact(db, g["id"], refs)


def test_late_review_about_deleted_person_writes_no_history(db, monkeypatch):
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    inner = _reply_builder(_good)

    def ask_then_delete(*a, **k):
        out = inner(*a, **k)
        _delete_enquiry_row_only(db, members[1])   # the person is deleted while the review is "in flight"
        return out
    monkeypatch.setattr(ai, "_ask", ask_then_delete)
    ai_queue.process_pending(db)
    assert row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (g["id"],)))["n"] == 0, "old text about a deleted person must not be persisted"
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after["ai_status"] == "pending" and after["ai_rationale"] is None and after["evidence_json"] is None
    assert "deleted" in (after["ai_error"] or "")
    assert ai_queue.is_ready(db, after)["ready"] is False


def test_late_brief_about_deleted_person_writes_no_rows(db, monkeypatch):
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    assert row(db.execute("SELECT ai_status FROM groups WHERE id=?", (g["id"],)))["ai_status"] == "ok"
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=?", (g["id"],)))["n"] >= 1   # the briefs run inside process_pending
    db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],))
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],))
    db.commit()

    def brief_then_delete(packet):
        out = fake_staff_briefs(packet)
        _delete_enquiry_row_only(db, members[1])
        return out
    monkeypatch.setattr(ai, "staff_briefs", brief_then_delete)
    ai_queue.process_briefs(db)
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=?", (g["id"],)))["n"] == 0
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after["brief_status"] == "stale" and after["ai_status"] == "pending" and "deleted" in (after["held_reason"] or "")
    assert ai_queue.is_ready(db, after)["ready"] is False


def test_intro_draft_about_deleted_person_is_refused(db, monkeypatch):
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    people = ai_queue.intro_members(db, g["id"])
    assert len(people) == 2
    ai_queue.record_intro_draft(db, g["id"], "draft one", "AI", "tester", members=people)   # intact: written
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))["n"] == 1
    _delete_enquiry_row_only(db, members[1])
    with pytest.raises(ValueError):
        ai_queue.record_intro_draft(db, g["id"], "draft two (names the deleted person)", "AI", "tester", members=people)
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=? AND kind='intro'", (g["id"],)))["n"] == 1
    # the earlier draft no longer counts as current once the group's own evidence moves on; and nothing about the deleted person was added
    assert ai_queue.intro_state(db, g["id"])["drafted_at"] is not None


# ---------------------------------------------------------------- refs_json provenance (root, 8 Nov 2026)
def test_refs_json_is_captured_before_the_call_and_written_on_every_history_row(db, monkeypatch):
    import json
    from tests.test_t6_grounding import _party_group
    a, lead, solo, g = _party_group(db, monkeypatch)   # Astrid + Bo + Cy (party) and Devi: approved + briefed
    pms = rows(db.execute("SELECT id FROM party_members WHERE enquiry_id=? ORDER BY id", (lead["id"],)))
    want = {"enquiry_id": lead["id"], "party_member_id": None}
    rr = rows(db.execute("SELECT refs_json FROM review_rounds WHERE group_id=?", (g["id"],)))
    assert rr and all(r["refs_json"] for r in rr)
    m = json.loads(rr[-1]["refs_json"])
    lead_ref = next(k for k, v in m.items() if {kk: v.get(kk) for kk in want} == want)
    # 9 Nov 2026 (takeover source-integrity): each ident also carries the immutable person_uid of the person behind the row
    assert {k: m[f"{lead_ref}-m1"][k] for k in ("enquiry_id", "party_member_id")} == {"enquiry_id": lead["id"], "party_member_id": pms[0]["id"]}
    assert {k: m[f"{lead_ref}-m2"][k] for k in ("enquiry_id", "party_member_id")} == {"enquiry_id": lead["id"], "party_member_id": pms[1]["id"]}
    assert set(m[f"{lead_ref}-m1"]) == {"enquiry_id", "party_member_id", "person_uid"} and m[f"{lead_ref}-m1"]["person_uid"]
    assert {v["enquiry_id"] for v in m.values()} == {lead["id"], solo["id"]} and len(m) == 4
    briefs = rows(db.execute("SELECT kind, person_ref, refs_json FROM group_briefs WHERE group_id=?", (g["id"],)))
    assert briefs and all(b["refs_json"] for b in briefs)
    assert all(json.loads(b["refs_json"]) == m for b in briefs)   # same map on group and member rows of that round
    # the prompt never carries it
    p = ai_queue._payload(db, g)
    db.commit()   # _payload refreshes rule ranks; in production it runs inside the capture step, here the test is the step
    assert "refs_json" in p
    prompt_text = json.dumps(ai.review_packet([p]), default=str)   # exactly what assess_groups_and_apartments sends
    assert "party_member_id" not in prompt_text and "refs_json" not in prompt_text
    # intro rows carry the I-refs of the members snapshot
    people = ai_queue.intro_members(db, g["id"])
    ai_queue.record_intro_draft(db, g["id"], "draft", "AI", "tester", members=people)
    im = json.loads(row(db.execute("SELECT refs_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (g["id"],)))["refs_json"])
    assert set(im) == {x["ref"] for x in people} and im["I1-m1"]["party_member_id"] in {pms[0]["id"], pms[1]["id"]}


def test_party_member_replaced_in_the_same_slot_during_the_call_is_dropped(db, monkeypatch):
    """people_intact alone sees the same enquiry and the same number of party slots; refs_json pins the party_members ROW."""
    from tests.test_t6_grounding import _party_group
    a, lead, solo, g = _party_group(db, monkeypatch)
    db.execute("DELETE FROM review_rounds WHERE group_id=?", (g["id"],))
    db.execute("UPDATE groups SET ai_status='pending', ai_refresh=0, brief_status=NULL WHERE id=?", (g["id"],)); db.commit()
    p0 = ai_queue._payload(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    inner = _reply_builder(_good)

    def ask_then_swap(*a_, **k):
        out = inner(*a_, **k)
        old = row(db.execute("SELECT * FROM party_members WHERE enquiry_id=? ORDER BY id LIMIT 1", (lead["id"],)))
        db.execute("DELETE FROM party_members WHERE id=?", (old["id"],))
        db.execute("INSERT INTO party_members(enquiry_id, name, email) VALUES (?,?,?)", (lead["id"], "Zed New", "zed@example.com"))
        db.commit()
        return out
    monkeypatch.setattr(ai, "_ask", ask_then_swap)
    ai_queue.process_pending(db)
    assert ai_queue.people_intact(db, g["id"], p0["person_refs"]) and not ai_queue.refs_intact(db, p0["refs_json"])   # same shape, different row ...
    assert row(db.execute("SELECT COUNT(*) n FROM review_rounds WHERE group_id=?", (g["id"],)))["n"] == 0   # ... but the output is about a different person: dropped
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after["ai_status"] == "pending" and "deleted" in (after["ai_error"] or "")


def test_brief_failure_after_a_deletion_writes_no_failure_row_either(db, monkeypatch):
    """Root (8 Nov 2026): the T5 late-writer contract covers FAILURE history too - a deletion during the brief call followed by
    an AI failure must not leave a new 'failed' row carrying that snapshot's refs/error text."""
    g, members = _primary(db)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    ai_queue.process_pending(db)
    db.execute("DELETE FROM group_briefs WHERE group_id=?", (g["id"],))
    db.execute("UPDATE groups SET brief_status='pending' WHERE id=?", (g["id"],)); db.commit()

    def delete_then_fail(packet):
        _delete_enquiry_row_only(db, members[1])
        raise ai.AIUnavailable("credentials: simulated", kind="credentials")   # non-retryable by trusted kind
    monkeypatch.setattr(ai, "staff_briefs", delete_then_fail)
    ai_queue.process_briefs(db)
    assert row(db.execute("SELECT COUNT(*) n FROM group_briefs WHERE group_id=?", (g["id"],)))["n"] == 0
    after = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert after["brief_status"] == "stale" and after["ai_status"] == "pending" and "simulated" not in (after["held_reason"] or "")
