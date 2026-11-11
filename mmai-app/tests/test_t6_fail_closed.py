"""T6 fail-closed behaviour at the existing queue paths: preparatory fixtures and tests (8 November 2026).

Status of each test is explicit:
  * plain tests pin behaviour that already holds today (an outage never produces a 'ready' group; a group the review left out
    is failed, not ready; 'ready' never counts a failed group);
  * the second block pins the T6 fail-closed items implemented on 8 Nov 2026 (root-approved contract A): no clamping or
    defaulting of a rating, a review that omits offered apartments is unusable, requeue never promotes a legacy rating, and
    every assessment records the rule version and evidence it rests on.

All AI traffic is a fabricated `_ask` capture; no live call, no Google content, no real people.
"""
import json

import pytest

from server import ai, ai_queue, matching
from server.db import rows
from tests.test_launch_neutral import _apt, _pool  # noqa: F401  (fixtures: one 2B2B apartment, four compatible test enquirers)

def _run(db):
    a = _apt(db)
    _pool(db)
    res = matching.run_match(db, a["id"], "tester", dataset="test")
    assert res["ai_queued"] > 0
    return a, res


def _groups(db, run_id):
    return rows(db.execute("SELECT * FROM groups WHERE match_run_id=? ORDER BY id", (run_id,)))


def _reply_builder(make_item):
    """An `_ask` replacement: parses the packet the prompt carries and answers each group through `make_item(group_desc)`;
    returning None for a group leaves it out of the reply."""
    def _ask(prompt, model=None, max_tokens=1500, system=""):
        groups = json.loads(prompt[prompt.rindex("\n\n") + 2:])
        items = [make_item(g) for g in groups]
        return json.dumps([x for x in items if x is not None])
    return _ask


def _good(g):
    return {"index": g["index"], "rating": 7, "rationale": "fine", "verdict": "approve", "apartments": [{"apartment_id": x["apartment_id"], "reason": "r"} for x in g["apartments_in_rule_order"]],
            "findings": [{"fact_ids": g["fact_ids"][:2], "note": "fine"}]}


# ---------------------------------------------------------------- holds today
def test_outage_never_marks_ready(db, monkeypatch):
    a, res = _run(db)

    def down(prompt, model=None, max_tokens=1500, system=""):
        raise ai.AIUnavailable("simulated outage")

    monkeypatch.setattr(ai, "_ask", down)
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    out = ai_queue.process_pending(db)
    assert out["ok"] == 0 and out["failed"] == res["ai_queued"]
    g = _groups(db, res["run_id"])
    assert all(x["ai_status"] == "failed" and x["ai_rating"] is None and x["ranking_basis"] == "Rules only (AI failed)" and x["ai_error"] == ai.FAILURE_TEXT["provider"] for x in g)   # Modified A: closed sentence, never the exception's words
    st = ai_queue.status(db)
    assert st["ready"] == 0 and st["failed"] == len(g)
    # rule ranks are untouched: no apartment got an AI rank or reason
    assert all(r["ai_reason"] is None for r in rows(db.execute("SELECT ai_reason FROM group_apartments WHERE group_id IN (%s)" % ",".join(str(x["id"]) for x in g))))


def test_group_omitted_from_review_is_failed_not_ready(db, monkeypatch):
    a, res = _run(db)
    g0 = _groups(db, res["run_id"])
    monkeypatch.setattr(ai, "_ask", _reply_builder(lambda g: _good(g) if g["index"] != 0 else None))
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    out = ai_queue.process_pending(db, batch_size=len(g0))
    g = _groups(db, res["run_id"])
    assert out["ok"] == len(g) - 1 and out["failed"] == 1
    failed = [x for x in g if x["ai_status"] == "failed"]
    assert len(failed) == 1 and failed[0]["ai_rating"] is None and failed[0]["ai_error"] == ai.FAILURE_TEXT["review_missing"]


def test_non_json_and_empty_reviews_fail_every_group(db, monkeypatch):
    a, res = _run(db)
    for reply in ("I cannot assess these people.", "[]", "{}"):
        db.execute("UPDATE groups SET ai_status='pending', ai_error=NULL WHERE match_run_id=?", (res["run_id"],)); db.commit()
        monkeypatch.setattr(ai, "_ask", lambda prompt, model=None, max_tokens=1500, system="", _r=reply: _r)
        monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
        out = ai_queue.process_pending(db)
        assert out["ok"] == 0, reply
        assert all(x["ai_status"] == "failed" and x["ai_rating"] is None for x in _groups(db, res["run_id"])), reply


# ---------------------------------------------------------------- T6 fail-closed (implemented 8 Nov 2026)
def test_out_of_range_rating_is_rejected_not_clamped(db, monkeypatch):
    a, res = _run(db)
    monkeypatch.setattr(ai, "_ask", _reply_builder(lambda g: {**_good(g), "rating": 42}))
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    ai_queue.process_pending(db)
    g = _groups(db, res["run_id"])
    assert all(x["ai_status"] == "failed" and x["ai_error"] == ai.FAILURE_TEXT["review_unusable"] + " (1 problem found)" for x in g)   # the validator's sentence stays in process
    assert ai_queue.status(db)["ready"] == 0


def test_review_that_omits_offered_apartments_is_not_ready(db, monkeypatch):
    a, res = _run(db)
    monkeypatch.setattr(ai, "_ask", _reply_builder(lambda g: {**_good(g), "apartments": []}))
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    ai_queue.process_pending(db)
    g = _groups(db, res["run_id"])
    assert all(x["ai_status"] == "failed" and x["ai_error"] == ai.FAILURE_TEXT["review_unusable"] + " (1 problem found)" for x in g)
    # the raw review is kept as history, marked invalid; the stored reason is the closed sentence + count (Modified A)
    rr = rows(db.execute("SELECT valid, invalid_reason FROM review_rounds WHERE group_id=?", (g[0]["id"],)))
    assert rr and rr[0]["valid"] == 0 and rr[0]["invalid_reason"] == ai.FAILURE_TEXT["review_unusable"] + " (1 problem found)"


def test_requeue_does_not_promote_unassessed_legacy_rating_to_ok(db):
    a, res = _run(db)
    gid = _groups(db, res["run_id"])[0]["id"]
    # a pre-queue group: a rating from the old (pre-launch-neutral) assessment but no queue state
    db.execute("UPDATE groups SET ai_status=NULL, ai_rating=8.0, ai_rationale='old assessment mentioning nationality' WHERE id=?", (gid,)); db.commit()
    ai_queue.requeue_interrupted(db)
    g = rows(db.execute("SELECT ai_status, ai_rating, held_reason FROM groups WHERE id=?", (gid,)))[0]
    assert g["ai_status"] == "pending" and g["ai_rating"] is None and "superseded" in g["held_reason"].lower()
    hist = rows(db.execute("SELECT round, rating, rationale, valid FROM review_rounds WHERE group_id=?", (gid,)))
    assert hist == [{"round": 0, "rating": 8.0, "rationale": "old assessment mentioning nationality", "valid": 0}]   # history kept, not current


def test_rule_version_recorded_with_assessment(db, monkeypatch):
    a, res = _run(db)
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    monkeypatch.setattr(ai_queue.time, "sleep", lambda s: None)
    ai_queue.process_pending(db)
    g = _groups(db, res["run_id"])[0]
    from server import rules
    assert g["rule_version"] == rules.RULE_VERSION and g["evidence_hash"] and g["review_verdict"] == "approve" and g["ai_model"] == ai.MODEL
    ev = json.loads(g["evidence_json"])
    assert ev["rule_version"] == rules.RULE_VERSION and {m["enquiry_id"] for m in ev["members"]} == {int(x) for x in g["member_key"].split(",")}
    blob = json.dumps(ev)
    for w in ("Astrid", "Sweden", "@example.com", "cooking", "intro_sex", "intro_age"):
        assert w not in blob, "the evidence snapshot carries practical facts only, no identity or intro-only fields"
