"""D2 (9 Nov 2026, root restart probe reproduced on C200 live-1): a valid current review whose staff brief was interrupted (brief_status pending,
no review pending) must be resumed by the worker AND by the explicit process entry WITHOUT a new review; a terminal failed brief is never
silently retried. Root-authored probe, copied verbatim (pytest_plugins line dropped: tests/conftest is auto-loaded here)."""
import pytest


from server import ai, ai_queue
from server.db import row
from tests.test_t6_fail_closed import _run, _groups, _reply_builder, _good


def _approved_then_interrupted(db, monkeypatch, state):
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    _, res = _run(db)
    ai_queue.process_pending(db)
    groups = _groups(db, res["run_id"])
    approved = next(g for g in groups if g["ai_status"] == "ok" and g["brief_status"] == "ok")
    gid = approved["id"]
    db.execute("DELETE FROM group_briefs WHERE group_id=?", (gid,))
    db.execute("UPDATE groups SET brief_status=?, ready_at=NULL WHERE id=?", (state, gid))
    db.commit()
    calls = []
    original = ai_queue._call_briefs

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    def unexpected_review(*args, **kwargs):
        raise AssertionError("A valid current review must not be repeated to recover its brief")

    monkeypatch.setattr(ai_queue, "_call_briefs", counted)
    monkeypatch.setattr(ai, "_ask", unexpected_review)
    ai_queue.requeue_interrupted(db)
    assert ai_queue.pending_count(db) == 0
    return gid, calls


@pytest.mark.parametrize("entry", ["worker", "process"])
def test_valid_review_with_interrupted_brief_resumes_without_new_review(db, monkeypatch, entry):
    gid, calls = _approved_then_interrupted(db, monkeypatch, "pending")
    if entry == "worker":
        ai_queue._iteration(db)
    else:
        ai_queue.process_pending(db)
    current = row(db.execute("SELECT ai_status,brief_status FROM groups WHERE id=?", (gid,)))
    assert calls, "Pending staff brief was stranded when no review work existed"
    assert current["ai_status"] == "ok"
    assert current["brief_status"] == "ok"


def test_terminal_failed_brief_is_not_silently_retried(db, monkeypatch):
    gid, calls = _approved_then_interrupted(db, monkeypatch, "failed")
    ai_queue._iteration(db)
    assert not calls
    assert row(db.execute("SELECT brief_status FROM groups WHERE id=?", (gid,)))["brief_status"] == "failed"


def _two_interrupted(db, monkeypatch):
    """Two approved groups with interrupted briefs and no review work."""
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    _, res = _run(db)
    ai_queue.process_pending(db)
    approved = [g for g in _groups(db, res["run_id"]) if g["ai_status"] == "ok" and g["brief_status"] == "ok"]
    if len(approved) < 2:
        pytest.skip("fixture produced fewer than two approved groups")
    gids = [g["id"] for g in approved[:2]]
    for gid in gids:
        db.execute("DELETE FROM group_briefs WHERE group_id=?", (gid,))
        db.execute("UPDATE groups SET brief_status='pending', ready_at=NULL WHERE id=?", (gid,))
    db.commit()
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no review may be repeated")))
    return gids


def test_brief_only_work_is_bounded_per_pass_and_signals_remaining(db, monkeypatch):
    """D2 fairness: standalone brief work is a bounded slice per pass (BRIEFS_PER_PASS) and the worker's remaining-work signal
    stays true until the last pending brief has landed; pending_count keeps its review-only meaning throughout."""
    gids = _two_interrupted(db, monkeypatch)
    monkeypatch.setattr(ai_queue, "BRIEFS_PER_PASS", 1)
    assert ai_queue.pending_count(db) == 0 and ai_queue.briefs_pending_count(db) == 2
    calls = []
    original = ai_queue._call_briefs
    monkeypatch.setattr(ai_queue, "_call_briefs", lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    monkeypatch.setattr(ai_queue, "MATEBOT_BATCHES_PER_WAKE", 1)
    kicked = []
    monkeypatch.setattr(ai_queue, "kick", lambda: kicked.append(1))
    out = ai_queue._iteration(db)
    assert len(calls) == 1 and ai_queue.briefs_pending_count(db) == 1 and ai_queue.pending_count(db) == 0
    assert out["more"] is True and kicked, "remaining brief work must re-wake the worker"
    out = ai_queue._iteration(db)
    assert len(calls) == 2 and ai_queue.briefs_pending_count(db) == 0 and out["more"] is False
    assert all(row(db.execute("SELECT brief_status FROM groups WHERE id=?", (g,)))["brief_status"] == "ok" for g in gids)


def test_review_and_brief_pending_together_one_pass(db, monkeypatch):
    """Both kinds pending: the review pass still runs first and the brief step follows in the same pass (unchanged behaviour), with
    the interrupted brief of the other group picked up too and no review repeated for it."""
    monkeypatch.setattr(ai, "_ask", _reply_builder(_good))
    _, res = _run(db)
    ai_queue.process_pending(db)
    groups = _groups(db, res["run_id"])
    approved = [g for g in groups if g["ai_status"] == "ok" and g["brief_status"] == "ok"]
    if len(approved) < 2:
        pytest.skip("fixture produced fewer than two approved groups")
    a, b = approved[0]["id"], approved[1]["id"]
    db.execute("DELETE FROM group_briefs WHERE group_id=?", (a,))
    db.execute("UPDATE groups SET brief_status='pending', ready_at=NULL WHERE id=?", (a,))
    db.execute("DELETE FROM review_rounds WHERE group_id=?", (b,))
    db.execute("UPDATE groups SET ai_status='pending', brief_status=NULL, review_verdict=NULL, ai_rating=NULL, ready_at=NULL WHERE id=?", (b,))
    db.commit()
    assert ai_queue.pending_count(db) == 1 and ai_queue.briefs_pending_count(db) == 1
    reviews = []
    good = _reply_builder(_good)
    monkeypatch.setattr(ai, "_ask", lambda *x, **k: (reviews.append(1), good(*x, **k))[1])
    ai_queue._iteration(db)
    assert row(db.execute("SELECT brief_status FROM groups WHERE id=?", (a,)))["brief_status"] == "ok"
    assert row(db.execute("SELECT ai_status, brief_status FROM groups WHERE id=?", (b,)))["ai_status"] == "ok"
    assert ai_queue.pending_count(db) == 0 and ai_queue.briefs_pending_count(db) == 0
