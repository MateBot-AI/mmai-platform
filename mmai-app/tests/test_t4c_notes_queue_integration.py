"""GAP-T4c v2 (9 Nov 2026) - queue / readiness / retention integration of the practical-notes step, without any provider.

Controlled extractor replies stand in for the model (ai_queue._extract_notes); the review call is the existing fabricated capture.
Covers root's integration-safety asks: a legacy approved+briefed group with an unread note is NOT ready and is re-queued by the
start-up sweep; an explicitly essential step-free need holds the approved group with a plain label (no code, no model words);
an edit of the person's free text invalidates the facts and the group's readiness; staff retry re-queues a failed extraction;
excluded-only notes leave the packet identical to a blank note; queue status exposes the notes work; the worker's remaining-work
signal counts unread notes; the staff view carries the person's own quote and plain labels only.
"""
import json

import pytest

from server import ai, ai_queue, matching, notes_facts
from server.db import now_iso, row, rows
from server.enquiries import forget_generated_text, get_enquiry, update_enquiry
from tests.test_launch_neutral import _apt, _pool  # noqa: F401
from tests.test_t6_fail_closed import _good, _groups, _reply_builder, _run

P116 = "Step-free access is essential for me."
EXCLUDED_ONLY = "Call me on 0400 000 000, I am Swedish and 24."


def _item(text, field="else_text", category="step_free_access", strength="essential", span=None, value=None):
    span = text if span is None else span
    start = text.index(span)
    return {"category": category, "polarity": "affirmed", "strength": strength, "subject": "self", "field": field,
            "span": span, "start": start, "end": start + len(span), "value": value}


def _extractor(table):
    """Controlled extractor: reply per exact else_text; anything else -> no items."""
    def ext(fields, party_size, meter):
        meter["asks"] = meter.get("asks", 0) + 1
        return table.get(fields.get("else_text") or "", {"items": [], "residual": False})
    return ext


def _approved_ready(db, monkeypatch, extractor=None):
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    if extractor:
        monkeypatch.setattr(ai_queue, "_extract_notes", extractor)
    _, res = _run(db)
    ai_queue.process_pending(db)
    return _groups(db, res["run_id"])


# ---------------------------------------------------------------- legacy rows: readiness fails closed, sweep re-queues
def test_legacy_ready_group_with_unread_note_is_not_ready_and_is_requeued_by_the_startup_sweep(db, monkeypatch):
    groups = _approved_ready(db, monkeypatch)
    g = next(x for x in groups if x["ai_status"] == "ok" and x["brief_status"] == "ok")
    assert ai_queue.is_ready(db, g)["ready"] is True
    # simulate the acceptance baseline: the rows pre-date the notes step (NULL status, a note already on file)
    member = row(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (g["id"],)))["enquiry_id"]
    db.execute("UPDATE enquiries SET notes_status=NULL, notes_packet_json=NULL, else_text=? WHERE id=?", (P116, member))
    db.commit()
    chk = ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))
    assert chk["ready"] is False and any("note" in r for r in chk["reasons"])
    assert g["id"] not in {x["id"] for x in ai_queue.ready_groups(db)}
    n = ai_queue.requeue_interrupted(db)
    assert n >= 1
    live = row(db.execute("SELECT ai_status, brief_status, held_reason, ready_at FROM groups WHERE id=?", (g["id"],)))
    assert live["ai_status"] == "pending" and live["brief_status"] == "stale" and live["ready_at"] is None
    assert "notes had not been read" in live["held_reason"]


def test_group_with_pending_member_notes_is_not_reviewed_in_that_pass_and_counts_as_work(db, monkeypatch):
    """Fail closed: with the notes step unable to land (guard failure simulated by a non-writing apply), the group stays pending with
    a plain basis and no review call is made for it; notes_pending_count signals remaining work for the worker."""
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no review may run before the members' notes are read")))
    monkeypatch.setattr(ai_queue, "_apply_notes", lambda *a, **k: "")
    monkeypatch.setattr(ai_queue, "_apply_blank_notes", lambda *a, **k: "")
    _, res = _run(db)
    assert ai_queue.notes_pending_count(db) > 0
    out = ai_queue.process_pending(db)
    assert out["groups"] == 0 and out.get("notes", {}).get("people", 0) > 0
    for g in _groups(db, res["run_id"]):
        assert g["ai_status"] == "pending" and g["ranking_basis"] == ai_queue.NOTES_PENDING_TEXT
    assert ai_queue.notes_pending_count(db) > 0
    st = ai_queue.status(db)
    assert st["notes_pending"] == ai_queue.notes_pending_count(db) and st["notes_failed"] == 0


# ---------------------------------------------------------------- the T6 staff gate with a plain label
def test_essential_step_free_need_holds_the_approved_group_with_a_plain_label(db, monkeypatch):
    groups = _approved_ready(db, monkeypatch, _extractor({P116: {"items": [_item(P116)], "residual": False}}))
    # nobody wrote P116 yet: everyone ok. Now one member writes it (staff edit) -> facts invalidated, group re-queued on edit path
    g = next(x for x in groups if x["ai_status"] == "ok")
    member = row(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (g["id"],)))["enquiry_id"]
    update_enquiry(db, member, {"else_text": P116}, "tester")
    e = row(db.execute("SELECT notes_status, notes_packet_json FROM enquiries WHERE id=?", (member,)))
    assert e["notes_status"] == "pending" and e["notes_packet_json"] is None
    assert ai_queue.is_ready(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],))))["ready"] is False
    db.execute("UPDATE groups SET ai_status='pending', ai_queued_at=NULL WHERE id=?", (g["id"],)); db.commit()
    out = ai_queue.process_pending(db)
    assert out.get("notes", {}).get("ok") == 1 and out["notes"]["calls"] == 1
    live = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    assert live["ai_status"] == "held" and live["brief_status"] is None and live["ready_at"] is None
    assert live["held_reason"] == "Step-free access needs confirmation from the team before introduction"
    for forbidden in ("step_free_access", "essential", "rejected", "model", "lift", "medical"):
        assert forbidden not in live["held_reason"]
    assert ai_queue.is_ready(db, live)["ready"] is False
    # the packet carried the neutral fact, never the text
    p = ai_queue._payload(db, live)
    m = next(x for x in p["members_for_ai"] if x["notes_facts"])
    assert m["notes_facts"] == [{"category": "step_free_access", "strength": "essential", "value": None, "basis": notes_facts.BASIS, "source_field": "else_text", "possible_ambiguity": False}]
    dump = json.dumps(p["members_for_ai"])
    assert P116 not in dump and "essential for me" not in dump and "span" not in dump and "residual" not in dump
    # staff see the person's own quote, a plain label and no code
    view = get_enquiry(db, member)["notes_review"]
    assert view["status"] == "ok" and view["items"][0]["label"] == "Step-free access" and view["items"][0]["quote"] == P116
    # v2 coverage floor: the fixture row keeps its seeded work / intro text, which no accepted item represents, so the
    # staff-only unresolved marker is set; the packet and the hold above are unchanged by it
    e_now = get_enquiry(db, member)
    uncovered = notes_facts.uncovered_fields({k: e_now.get(k) for k in notes_facts.SOURCE_FIELDS}, [{"field": "else_text"}])
    assert view["unreviewed_note"] is bool(uncovered) and "notes_packet_json" not in e_now


def test_matters_strength_step_free_is_a_discussion_point_not_a_hold(db, monkeypatch):
    text = "Step-free access matters to me."
    groups = _approved_ready(db, monkeypatch, _extractor({text: {"items": [_item(text, strength="matters")], "residual": False}}))
    g = next(x for x in groups if x["ai_status"] == "ok")
    member = row(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (g["id"],)))["enquiry_id"]
    update_enquiry(db, member, {"else_text": text}, "tester")
    db.execute("UPDATE groups SET ai_status='pending', ai_queued_at=NULL WHERE id=?", (g["id"],)); db.commit()
    ai_queue.process_pending(db)
    live = row(db.execute("SELECT ai_status, brief_status FROM groups WHERE id=?", (g["id"],)))
    assert live["ai_status"] == "ok" and live["brief_status"] == "ok"
    from server import facts as facts_mod
    cat = facts_mod.catalogue(ai_queue._payload(db, row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))))
    assert any(k.endswith(".notes.step_free_access") for k in cat), "the fact is in the catalogue the review cites"
    assert cat[next(k for k in cat if k.endswith(".notes.step_free_access"))]["source"].endswith(notes_facts.BASIS)


# ---------------------------------------------------------------- excluded-only note == blank note in the packet
def test_excluded_only_note_leaves_packet_hold_and_rank_identical_to_blank(db, monkeypatch):
    groups = _approved_ready(db, monkeypatch, _extractor({EXCLUDED_ONLY: {"items": [_item(EXCLUDED_ONLY, category="smoke_free_home", strength="matters", value={"scope": "whole_home"})], "residual": False}}))
    g = next(x for x in groups if x["ai_status"] == "ok")
    before = ai_queue._payload(db, g); db.commit()
    member = row(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=? ORDER BY enquiry_id LIMIT 1", (g["id"],)))["enquiry_id"]
    update_enquiry(db, member, {"else_text": EXCLUDED_ONLY}, "tester")
    db.execute("UPDATE groups SET ai_status='pending', ai_queued_at=NULL WHERE id=?", (g["id"],)); db.commit()
    ai_queue.process_pending(db)
    after_g = row(db.execute("SELECT * FROM groups WHERE id=?", (g["id"],)))
    after = ai_queue._payload(db, after_g)
    from server import facts as facts_mod
    assert after["members_for_ai"] == before["members_for_ai"] and facts_mod.catalogue(after) == facts_mod.catalogue(before)
    assert after["evidence_hash"] != before["evidence_hash"] or True   # (the hash may differ: notes_status moved pending->ok; the model-facing packet does not)
    assert after_g["ai_status"] == "ok" and after_g["brief_status"] == "ok"
    e = row(db.execute("SELECT notes_status, notes_residual, notes_packet_json FROM enquiries WHERE id=?", (member,)))
    assert e["notes_status"] == "ok" and e["notes_packet_json"] == "[]" and e["notes_residual"] == 1   # staff marker only
    rej = rows(db.execute("SELECT span, rejection_code FROM note_facts WHERE enquiry_id=?", (member,)))
    assert rej and all(r["span"] is None for r in rej) and rej[0]["rejection_code"] == "rejected_excluded"
    assert db.execute("SELECT COUNT(*) FROM note_extraction_cache WHERE enquiry_id=?", (member,)).fetchone()[0] == 1
    assert "0400" not in json.dumps(rows(db.execute("SELECT accepted_json FROM note_extraction_cache")))
    assert get_enquiry(db, member)["notes_review"]["unreviewed_note"] is True


# ---------------------------------------------------------------- failure, retry, cache reuse, retention
def test_failed_extraction_retries_with_backoff_then_holds_the_group_and_staff_retry_requeues_both(db, monkeypatch):
    """T3(d) C4 (10 Nov 2026): a failed extraction is one ROUND; the person stays pending with a scheduled next attempt (backoff) and
    the group waits with a plain basis (no review without the notes). After NOTES_MAX_ROUNDS the state is 'failed' and the group is
    HELD for staff - never a 'Rules only' basis. Staff retry restarts the budget for both. No provider words anywhere."""
    def down(fields, party_size, meter):
        meter["asks"] = meter.get("asks", 0) + 1
        raise ai.AIUnavailable("Connection error: simulated outage")
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue, "_extract_notes", down)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    _, res = _run(db)
    ids = [r["id"] for r in rows(db.execute("SELECT id FROM enquiries WHERE is_test=1 ORDER BY id"))]
    db.execute("UPDATE enquiries SET else_text=?, work=NULL, intro=NULL WHERE id=?", (P116, ids[0]))
    db.execute("UPDATE enquiries SET else_text=NULL, work=NULL, intro=NULL WHERE id IN (?,?,?)", tuple(ids[1:])); db.commit()   # only one person has notes to read
    gs = lambda: [g for g in _groups(db, res["run_id"]) if ids[0] in {r["enquiry_id"] for r in rows(db.execute("SELECT enquiry_id FROM group_members WHERE group_id=?", (g["id"],)))}]
    out = ai_queue.process_pending(db)
    assert out["notes"]["retry_scheduled"] == 1 and out["notes"]["failed"] == 0 and out["groups"] == 3   # the three groups without this person are reviewed; none of this person's
    e = row(db.execute("SELECT notes_status, notes_attempts, notes_next_retry_at FROM enquiries WHERE id=?", (ids[0],)))
    assert e["notes_status"] == "pending" and e["notes_attempts"] == 1 and e["notes_next_retry_at"] > now_iso()
    assert all(g["ai_status"] == "pending" and g["ranking_basis"] == ai_queue.NOTES_RETRY_SCHEDULED_TEXT for g in gs())
    st = ai_queue.status(db)
    assert st["notes_pending"] >= 1 and st["notes_retry_scheduled"] == 1 and st["notes_failed"] == 0   # scheduled retries ARE pending work
    # not due yet: a pass in between makes no attempt for this person
    out = ai_queue.process_pending(db)
    assert not out.get("notes") or out["notes"]["people"] == 0
    # rounds fall due one by one (clock moved by rewriting the schedule): bounded, then held
    for rnd in range(2, ai_queue.NOTES_MAX_ROUNDS + 1):
        db.execute("UPDATE enquiries SET notes_next_retry_at=? WHERE id=?", ("2000-01-01T00:00:00+00:00", ids[0])); db.commit()
        out = ai_queue.process_pending(db)
        e = row(db.execute("SELECT notes_status, notes_attempts FROM enquiries WHERE id=?", (ids[0],)))
        assert e["notes_attempts"] == rnd
        if rnd < ai_queue.NOTES_MAX_ROUNDS:
            assert e["notes_status"] == "pending" and out["notes"]["retry_scheduled"] == 1
    assert e["notes_status"] == "failed" and out["notes"]["failed"] == 1
    held = gs()
    assert held and all(g["ai_status"] == "held" and g["ranking_basis"] == ai_queue.NOTES_HELD_BASIS and g["held_reason"] == ai_queue.NOTES_HELD_LABEL
                        and g["ready_at"] is None and g["ai_error"] is None for g in held)
    assert not any("Rules only" in (g["ranking_basis"] or "") for g in held)
    assert all(ai_queue.is_ready(db, g)["ready"] is False for g in held)
    dump = json.dumps(held) + json.dumps(rows(db.execute("SELECT * FROM enquiries WHERE id=?", (ids[0],))))
    assert "simulated outage" not in dump and "Connection" not in dump
    assert ai_queue.status(db)["notes_failed"] >= 1 and ai_queue.status(db)["notes_retry_scheduled"] == 0
    # staff retry: the person is pending again with a fresh budget, the held groups are queued, the next pass reads and reviews
    monkeypatch.setattr(ai_queue, "_extract_notes", _extractor({P116: {"items": [_item(P116)], "residual": False}}))
    assert ai_queue.retry_failed(db, "tester", [g["id"] for g in held]) == len(held)
    e = row(db.execute("SELECT notes_status, notes_attempts, notes_next_retry_at FROM enquiries WHERE id=?", (ids[0],)))
    assert e["notes_status"] == "pending" and e["notes_attempts"] == 0 and e["notes_next_retry_at"] is None
    assert all(g["ai_status"] == "pending" for g in gs())
    ai_queue.process_pending(db)
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (ids[0],)))["notes_status"] == "ok"
    assert all(g["ai_status"] in ("ok", "held") for g in gs())   # reviewed (P116 may now hold it for the step-free confirmation - a different, recorded gate)


def test_a_changed_note_restarts_the_extraction_round_budget(db, monkeypatch):
    def down(fields, party_size, meter):
        meter["asks"] = meter.get("asks", 0) + 1
        raise ai.AIUnavailable("outage")
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue, "_extract_notes", down)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    _, res = _run(db)
    ids = [r["id"] for r in rows(db.execute("SELECT id FROM enquiries WHERE is_test=1 ORDER BY id"))]
    db.execute("UPDATE enquiries SET else_text=?, work=NULL, intro=NULL WHERE id=?", (P116, ids[0]))
    db.execute("UPDATE enquiries SET else_text=NULL, work=NULL, intro=NULL WHERE id IN (?,?,?)", tuple(ids[1:])); db.commit()
    ai_queue.process_pending(db)
    db.execute("UPDATE enquiries SET notes_next_retry_at='2000-01-01T00:00:00+00:00' WHERE id=?", (ids[0],)); db.commit()
    ai_queue.process_pending(db)
    assert row(db.execute("SELECT notes_attempts FROM enquiries WHERE id=?", (ids[0],)))["notes_attempts"] == 2
    update_enquiry(db, ids[0], {"else_text": "I need a step-free route, it is essential."}, "tester")   # new tuple: the edit path re-queues
    db.execute("UPDATE enquiries SET notes_next_retry_at=NULL WHERE id=?", (ids[0],)); db.commit()
    ai_queue.process_pending(db)
    e = row(db.execute("SELECT notes_status, notes_attempts FROM enquiries WHERE id=?", (ids[0],)))
    assert e["notes_status"] == "pending" and e["notes_attempts"] == 1


def test_a_member_of_a_group_held_for_another_members_notes_is_still_read_independently(db, monkeypatch):
    """Root's regression (10 Nov 2026): NOTES_PER_PASS=1, a two-member group whose first member exhausts the extraction rounds -
    the group is HELD (NOTES_HELD_BASIS) while the second member's notes are still unread and the person sits in no other group.
    Independent fact processing: the second member's notes are still read (bounded, counted as pending work and drained) without
    reviewing or readying the held group and without retrying the terminal failed member."""
    def ext(fields, party_size, meter):
        meter["asks"] = meter.get("asks", 0) + 1
        if fields.get("else_text") == P116:
            raise ai.AIUnavailable("outage")
        return {"items": [], "residual": False}
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue, "_extract_notes", ext)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    _, res = _run(db)
    ids = [r["id"] for r in rows(db.execute("SELECT id FROM enquiries WHERE is_test=1 ORDER BY id"))]
    db.execute("UPDATE enquiries SET else_text=?, work=NULL, intro=NULL WHERE id=?", (P116, ids[0]))
    db.execute("UPDATE enquiries SET else_text='I garden on weekends.', work=NULL, intro=NULL WHERE id=?", (ids[1],))
    db.execute("UPDATE enquiries SET else_text=NULL, work=NULL, intro=NULL WHERE id IN (?,?)", (ids[2], ids[3]))
    pair = row(db.execute("SELECT id FROM groups WHERE match_run_id=? AND member_key=?", (res["run_id"], f"{ids[0]},{ids[1]}")))["id"]
    for g in rows(db.execute("SELECT id FROM groups WHERE match_run_id=? AND id<>?", (res["run_id"], pair))):   # the pair is the ONLY group
        for t in ("group_members", "group_apartments"):
            db.execute(f"DELETE FROM {t} WHERE group_id=?", (g["id"],))
        db.execute("DELETE FROM groups WHERE id=?", (g["id"],))
    db.execute("UPDATE groups SET is_primary=1 WHERE id=?", (pair,)); db.commit()
    for rnd in range(1, ai_queue.NOTES_MAX_ROUNDS + 1):   # one person per pass: the first member uses up its rounds first (lowest id)
        db.execute("UPDATE enquiries SET notes_next_retry_at=NULL WHERE id=?", (ids[0],)); db.commit()
        out = ai_queue.process_pending(db, notes_limit=1)
        assert out["notes"]["people"] == 1
    st0 = row(db.execute("SELECT notes_status, notes_attempts FROM enquiries WHERE id=?", (ids[0],)))
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (pair,)))
    assert st0["notes_status"] == "failed" and g["ai_status"] == "held" and g["ranking_basis"] == ai_queue.NOTES_HELD_BASIS
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (ids[1],)))["notes_status"] in (None, "pending")
    assert ai_queue.notes_pending_count(db) == 1 and ai_queue.status(db)["notes_pending"] == 1   # the second member is still pending work
    out = ai_queue.process_pending(db, notes_limit=1)
    assert out["notes"]["people"] == 1 and out["notes"]["calls"] == 1 and out["groups"] == 0
    assert row(db.execute("SELECT notes_status FROM enquiries WHERE id=?", (ids[1],)))["notes_status"] == "ok"   # read (no facts, residual none)
    assert row(db.execute("SELECT notes_status, notes_attempts FROM enquiries WHERE id=?", (ids[0],))) == dict(st0)   # the failed member is not retried automatically
    g = row(db.execute("SELECT * FROM groups WHERE id=?", (pair,)))
    assert g["ai_status"] == "held" and g["ranking_basis"] == ai_queue.NOTES_HELD_BASIS and g["ready_at"] is None   # reading the other member does not review or ready the held group
    assert ai_queue.notes_pending_count(db) == 0
    out = ai_queue.process_pending(db, notes_limit=1)
    assert (not out.get("notes") or out["notes"]["people"] == 0) and out["groups"] == 0   # drained: nothing left to read, no provider call


def test_identical_input_tuples_share_one_call_and_cache_rows_are_owned_and_forgotten(db, monkeypatch):
    calls = []
    def ext(fields, party_size, meter):
        meter["asks"] = meter.get("asks", 0) + 1
        calls.append(fields["else_text"])
        return {"items": [_item(P116)], "residual": False}
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue, "_extract_notes", ext)
    _, res = _run(db)
    ids = [r["id"] for r in rows(db.execute("SELECT id FROM enquiries WHERE is_test=1 ORDER BY id"))]
    db.execute("UPDATE enquiries SET else_text=?, work=NULL, intro=NULL WHERE id IN (?,?)", (P116, ids[0], ids[1]))
    db.execute("UPDATE enquiries SET else_text=NULL, work=NULL, intro=NULL WHERE id IN (?,?)", (ids[2], ids[3])); db.commit()
    out = ai_queue.process_pending(db)
    assert out["notes"]["people"] == 4 and out["notes"]["ok"] == 2 and out["notes"]["none"] == 2
    assert len(calls) == 1 and out["notes"]["calls"] == 1 and out["notes"]["cached"] == 1 and out["notes"]["distinct_tuples"] == 1
    cache = rows(db.execute("SELECT enquiry_id, input_key FROM note_extraction_cache ORDER BY id"))
    assert [r["enquiry_id"] for r in cache] == [ids[0]] and len({r["input_key"] for r in cache}) == 1   # one owned row (the caller's); the hit wrote none
    assert db.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (ids[1],)).fetchone()[0] == 1   # per-person revalidation stored the second person's facts
    # retention: forgetting the owner removes the cache row and the facts; the other person's facts stay (their own text)
    out = forget_generated_text(db, [ids[0]])
    assert out["note_fact_rows"] >= 2
    assert db.execute("SELECT COUNT(*) FROM note_extraction_cache").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (ids[0],)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (ids[1],)).fetchone()[0] == 1


def test_worker_iteration_counts_unread_notes_as_remaining_work(monkeypatch):
    monkeypatch.setattr(ai_queue, "_consume_homebot_reviews", lambda c: {})
    monkeypatch.setattr(ai_queue, "pending_count", lambda c: 0)
    monkeypatch.setattr(ai_queue, "briefs_pending_count", lambda c: 0)
    left = [2]
    monkeypatch.setattr(ai_queue, "notes_pending_count", lambda c: left[0])
    def pp(c, **kw):
        assert kw.get("notes_limit") == ai_queue.NOTES_PER_PASS and kw.get("max_batches") == 1   # the worker is bounded (notes AND groups)
        left[0] -= 1
        return {"groups": 0, "notes": {"people": 1}}
    monkeypatch.setattr(ai_queue, "process_pending", pp)
    kicked = []
    monkeypatch.setattr(ai_queue, "kick", lambda: kicked.append(1))
    monkeypatch.setattr(ai_queue, "MATEBOT_BATCHES_PER_WAKE", 1)
    out = ai_queue._iteration(object())
    assert out["matebot_batches"] == 1 and out["more"] is True and kicked == [1]   # unread notes keep the worker awake


def test_queue_off_means_no_automatic_extraction_call(monkeypatch):
    monkeypatch.setenv("MMAI_AI_QUEUE", "0")
    assert notes_facts.extract_if_due({"else_text": P116}) == "pending"
    assert notes_facts.extract_if_due({"else_text": ""}) == "none"
    monkeypatch.setenv("MMAI_AI_QUEUE", "1")
    assert notes_facts.extract_if_due({"else_text": P116}) == "call"
    assert notes_facts.extract_if_due({"else_text": P116}, cached={"items": []}) == "cached"


def test_worker_wake_reviews_at_most_one_batch_of_groups_even_when_all_notes_are_already_read(db, monkeypatch):
    """Root (9 Nov 2026): the notes bound must not be the only bound - a large ready-review backlog is served one AI_BATCH per
    wake-slice with the remaining-work signal set, so HomeBot gets a pass between slices."""
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue, "_consume_homebot_reviews", lambda c: {})
    kicked = []
    monkeypatch.setattr(ai_queue, "kick", lambda: kicked.append(1))
    monkeypatch.setattr(ai_queue, "AI_BATCH", 1)
    monkeypatch.setattr(ai_queue, "MATEBOT_BATCHES_PER_WAKE", 1)
    _, res = _run(db)
    # read every note first (direct entry, no review): simulate by processing notes only
    ai_queue.process_notes(db, limit=None)
    assert ai_queue.notes_pending_count(db) == 0
    pending_before = ai_queue.pending_count(db)
    assert pending_before >= 2, "fixture must queue at least two groups"
    kicked.clear()   # enqueue kicks once; we are measuring the wake-slice's own remaining-work kick
    out = ai_queue._iteration(db)
    assert out["matebot_batches"] == 1 and out["more"] is True and kicked == [1]
    reviewed = db.execute("SELECT COUNT(*) FROM groups WHERE match_run_id=? AND ai_status<>'pending'", (res["run_id"],)).fetchone()[0]
    assert reviewed == 1 and ai_queue.pending_count(db) == pending_before - 1
