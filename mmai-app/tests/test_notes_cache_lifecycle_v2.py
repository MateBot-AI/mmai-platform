"""Positive-backed lifecycle checks; original three before-tests remain untouched."""
import json

from server import ai_queue
from tests.test_data_and_api import appdb, client  # noqa: F401
from tests.test_intro_profile import _plant_group
from tests.test_retention_cleanup import _submit

TEXT = "Step-free access is essential for me."
CANARY = "UNVALIDATED_PRIVATE_CACHE_PROSE_84206"


def prepare(client):
    e = _submit(client, else_text=TEXT)
    c = appdb(client)
    gid = _plant_group(c, e["id"])
    c.execute("UPDATE groups SET ai_status='pending' WHERE id=?", (gid,))
    c.execute("UPDATE enquiries SET notes_status='pending' WHERE id=?", (e["id"],))
    c.commit()
    return c, e["id"]


def controlled_reply():
    return {"items": [{
        "category": "step_free_access", "polarity": "affirmed", "strength": "essential",
        "subject": "self", "field": "else_text", "span": TEXT,
        "start": 0, "end": len(TEXT), "value": None,
    }], "residual": False, "private_debug": CANARY}


def cached(c):
    return [dict(r) for r in c.execute("SELECT * FROM note_extraction_cache")]


def test_valid_fact_cached_without_untrusted_extra_prose_then_deleted(client, monkeypatch):
    c, eid = prepare(client)
    monkeypatch.setattr(ai_queue, "_extract_notes", lambda *a, **kw: controlled_reply())
    result = ai_queue.process_notes(c, limit=1, attempts=1)
    assert result["ok"] == 1
    before = cached(c)
    assert len(before) == 1 and before[0]["enquiry_id"] == eid
    assert CANARY not in json.dumps(before)
    packet = json.loads(c.execute("SELECT notes_packet_json FROM enquiries WHERE id=?", (eid,)).fetchone()[0])
    assert any(i["category"] == "step_free_access" and i["strength"] == "essential" for i in packet)
    response = client.delete(f"/api/enquiries/{eid}")
    assert response.status_code == 200, response.text
    assert not cached(c)
    assert c.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (eid,)).fetchone()[0] == 0


def test_deletion_during_valid_extraction_does_not_resurrect_any_result(client, monkeypatch):
    c, eid = prepare(client)

    def extract(*args, **kwargs):
        response = client.delete(f"/api/enquiries/{eid}")
        assert response.status_code == 200, response.text
        return controlled_reply()

    monkeypatch.setattr(ai_queue, "_extract_notes", extract)
    ai_queue.process_notes(c, limit=1, attempts=1)
    assert c.execute("SELECT COUNT(*) FROM enquiries WHERE id=?", (eid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (eid,)).fetchone()[0] == 0
    assert not cached(c)


def test_edit_during_valid_extraction_discards_old_result_and_keeps_new_note_pending(client, monkeypatch):
    c, eid = prepare(client)
    replacement = "I prefer living near a train line."

    def extract(*args, **kwargs):
        response = client.patch(f"/api/enquiries/{eid}", json={"else_text": replacement})
        assert response.status_code == 200, response.text
        return controlled_reply()

    monkeypatch.setattr(ai_queue, "_extract_notes", extract)
    ai_queue.process_notes(c, limit=1, attempts=1)
    current = dict(c.execute("SELECT * FROM enquiries WHERE id=?", (eid,)).fetchone())
    assert current["else_text"] == replacement
    assert current["notes_status"] == "pending"
    assert not json.loads(current["notes_packet_json"] or "[]")
    assert c.execute("SELECT COUNT(*) FROM note_facts WHERE enquiry_id=?", (eid,)).fetchone()[0] == 0
    assert not cached(c)
