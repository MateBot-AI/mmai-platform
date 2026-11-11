"""Independent bounded-work and wake-up checks, with no provider calls."""
from server import ai_queue
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _submit


def test_default_notes_pass_honours_the_configured_person_bound(client, monkeypatch):
    c = appdb(client)
    count = ai_queue.NOTES_PER_PASS + 1
    for n in range(count):
        e = _submit(client, email=f"notes-limit-{n}@example.test",
                    else_text="Step-free access is essential for me.", intro=f"Test note tuple {n}.")
        gid = _plant_group(c, e["id"])
        c.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (gid,))
        c.execute("UPDATE enquiries SET notes_status='pending' WHERE id=?", (e["id"],))
        c.commit()
    invocations = []

    def controlled(*args, **kwargs):
        invocations.append(1)
        return {"items": [], "residual": True}

    monkeypatch.setattr(ai_queue, "_extract_notes", controlled)
    result = ai_queue.process_notes(c)
    assert 0 < result["people"] <= ai_queue.NOTES_PER_PASS
    assert len(invocations) <= ai_queue.NOTES_PER_PASS
    assert ai_queue.notes_pending_count(c) > 0


def test_notes_only_progress_signals_remaining_work_without_starving_homebot(monkeypatch):
    order = []
    monkeypatch.setattr(ai_queue, "MATEBOT_BATCHES_PER_WAKE", 3)
    monkeypatch.setattr(ai_queue, "pending_count", lambda c: 1)
    monkeypatch.setattr(ai_queue, "briefs_pending_count", lambda c: 0)
    monkeypatch.setattr(ai_queue, "notes_pending_count", lambda c: 9)
    monkeypatch.setattr(ai_queue, "_consume_homebot_reviews",
                        lambda c: order.append("homebot") or {"batches": 0})
    monkeypatch.setattr(ai_queue, "process_pending",
                        lambda c, **kw: order.append("notes") or {"groups": 0, "notes": {"people": 2}})
    monkeypatch.setattr(ai_queue, "kick", lambda: order.append("wake"))
    result = ai_queue._iteration(object())
    assert result["more"] is True
    assert 1 <= order.count("notes") <= 3
    assert order[0] == "homebot" and order[-2:] == ["homebot", "wake"]
    assert order.count("wake") == 1
