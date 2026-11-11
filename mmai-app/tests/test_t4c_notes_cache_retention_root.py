"""Independent cache/lifecycle probes; controlled replies, no provider."""
import json

from server import ai_queue, db
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _submit

MARKER = "PRIVATE_CACHE_CANARY_92817"


def prepare(client):
    e = _submit(client, else_text="Step-free access is essential for me.")
    c = appdb(client)
    gid = _plant_group(c, e["id"])
    c.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (gid,))
    c.execute("UPDATE enquiries SET notes_status='pending' WHERE id=?", (e["id"],))
    c.commit()
    return c, e["id"]


def cache_rows(c):
    if db.is_postgres():
        present = c.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='note_extraction_cache'").fetchone()[0]
    else:
        present = c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='note_extraction_cache'").fetchone()[0]
    return [dict(r) for r in c.execute("SELECT * FROM note_extraction_cache")] if present else []


def reply():
    # Untrusted extra model prose is not needed for any validated fact.
    return {"items": [], "residual": False, "private_debug": MARKER}


def test_unvalidated_model_prose_is_not_persisted_in_cache(client, monkeypatch):
    c, eid = prepare(client)
    monkeypatch.setattr(ai_queue, "_extract_notes", lambda *a, **kw: reply())
    ai_queue.process_notes(c, limit=1, attempts=1)
    assert MARKER not in json.dumps(cache_rows(c))


def test_person_deleted_during_call_does_not_leave_a_late_cache_row(client, monkeypatch):
    c, eid = prepare(client)

    def during_call(*args, **kwargs):
        response = client.delete(f"/api/enquiries/{eid}")
        assert response.status_code == 200, response.text
        return reply()

    monkeypatch.setattr(ai_queue, "_extract_notes", during_call)
    ai_queue.process_notes(c, limit=1, attempts=1)
    assert c.execute("SELECT COUNT(*) FROM enquiries WHERE id=?", (eid,)).fetchone()[0] == 0
    assert not cache_rows(c)


def test_person_deleted_after_caching_removes_orphaned_cache(client, monkeypatch):
    c, eid = prepare(client)
    monkeypatch.setattr(ai_queue, "_extract_notes", lambda *a, **kw: reply())
    ai_queue.process_notes(c, limit=1, attempts=1)
    response = client.delete(f"/api/enquiries/{eid}")
    assert response.status_code == 200, response.text
    assert not cache_rows(c)
