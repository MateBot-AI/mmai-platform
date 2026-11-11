"""HTTP-level coverage for the two app.py routes that no other test reached by URL (static route-to-test cross-check, 11 Nov
2026): GET /api/public/countries/normalise and POST /api/ai-queue/reassess. Test-only; no product change. Temp SQLite via the
shared `client` fixture; the AI queue is only re-queued here, never processed (no provider call)."""
from fastapi.testclient import TestClient

from server.db import rows
from tests.test_data_and_api import client  # noqa: F401  (fixture re-used)


def test_public_country_normalise_is_bounded_and_plain(client):
    r = client.get("/api/public/countries/normalise", params={"q": "  viet nam "})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"input", "country", "confident"} and isinstance(body["confident"], bool)
    assert body["country"] == "Vietnam" and body["confident"] is True
    # input is capped at 120 characters before anything is done with it; nothing about the guess is a staff decision
    long = client.get("/api/public/countries/normalise", params={"q": "x" * 500}).json()
    assert len(long["input"]) == 120 and long["confident"] is False
    # anonymous (public form helper): no token needed, no record touched
    assert TestClient(client.app).get("/api/public/countries/normalise", params={"q": "Australia"}).status_code == 200
    assert client.get("/api/public/countries/normalise").status_code == 422   # q is required


def test_ai_queue_reassess_is_staff_only_requeues_only_live_groups_and_is_audited(client):
    assert TestClient(client.app).post("/api/ai-queue/reassess", json={"dataset": "test"}).status_code == 401
    # nothing live yet: zero re-queued, status still returned; the deliberate staff action is audited even at zero
    r = client.post("/api/ai-queue/reassess", json={"dataset": "test"})
    assert r.status_code == 200 and r.json()["requeued"] == 0 and "status" in r.json()
    import server.app as appmod
    db = appmod.db
    audits = rows(db.execute("SELECT staff_member, entity, entity_id, detail FROM audit_log WHERE action='ai_reassess_all'"))
    assert len(audits) == 1 and audits[0]["entity"] == "dataset" and audits[0]["entity_id"] == "test" and "0 group(s)" in audits[0]["detail"]
    # an empty body means the real data set; still a valid request (no 422 for `{}`)
    assert client.post("/api/ai-queue/reassess", json={}).status_code == 200
    # a non-object body is refused by the shared JSON guard, never half-applied
    assert client.post("/api/ai-queue/reassess", content=b"[1,2]", headers={"Content-Type": "application/json"}).status_code in (400, 422)
