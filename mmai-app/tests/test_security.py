"""Pre-publish security review (27 Oct 2026): regression tests for the fixes.

H1 stored XSS, H2 blocking logins / lockout, M1 CORS, M2 abuse limits, M4 bad input shapes, M5 public meta, L1 headers,
L4 timing, L5 hashed session tokens, L7 phone-enquiry attribution.
"""
import asyncio
import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PW
from tests.test_data_and_api import good_answers

XSS = "<img src=x onerror=alert(document.domain)>"


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MMAI_DB", str(tmp_path / "sec.db"))
    import server.db as dbmod
    dbmod._conn = None
    import server.app as appmod
    importlib.reload(appmod)
    from server import security as sec
    sec.limiter.reset()
    sec.login_guard.reset()
    c = TestClient(appmod.app, base_url="https://mmai.test")
    yield c, appmod, sec
    sec.limiter.reset()
    sec.login_guard.reset()


def _items(payload):
    return payload if isinstance(payload, list) else payload.get("items") or payload.get("enquiries") or []


def _staff(c, user="anna"):
    r = c.post("/api/auth/login", json={"username": user, "password": TEST_PW[user]})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["token"]}


# ---------------- H1: names cannot carry markup; the client escapes everything it renders ----------------
def test_h1_markup_in_names_is_rejected_server_side(app_client):
    c, _, _ = app_client
    r = c.post("/api/public/enquiries", json=good_answers(first_name=XSS))
    assert r.status_code == 422 and "first_name" in r.json()["errors"]
    r = c.post("/api/public/enquiries", json=good_answers(party="With friends", party_members=[{"name": XSS, "birth_country_raw": "Sweden"}]))
    assert r.status_code == 422 and any(k.startswith("party_members.0.name") for k in r.json()["errors"])
    # ordinary punctuation in names is still fine
    assert c.post("/api/public/enquiries", json=good_answers(first_name="Anne-Marie O'Neil")).status_code == 201


def test_h1_client_escapes_user_text():
    """form.js and app.js must not interpolate person-supplied text into innerHTML unescaped."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    form = open(os.path.join(root, "client", "form.js"), encoding="utf-8").read()
    appjs = open(os.path.join(root, "client", "app.js"), encoding="utf-8").read()
    api = open(os.path.join(root, "client", "api.js"), encoding="utf-8").read()
    assert "esc:" in api and "&lt;" in api
    assert "${mate.enquirer_first_name}" not in form and "${esc(mate.enquirer_first_name)}" in form
    html_lines = [l for l in form.splitlines() if "innerHTML" in l or l.strip().startswith(("?", ":"))]
    assert not any("${friendOf}" in l or "${answers.first_name}" in l or "${mate.reason}" in l for l in html_lines)
    assert "${esc(friendOf)}" in form and "${esc(answers.first_name)}" in form
    assert "${e.message}" not in appjs.split("innerHTML")[1].split("\n")[0]
    index = open(os.path.join(root, "client", "index.html"), encoding="utf-8").read()
    assert "Content-Security-Policy" in index and "<script>" not in index and "fontshare" not in index


# ---------------- H2: failed logins do not stall the server; lockout after repeated failures ----------------
def test_h2_failed_login_does_not_block_other_requests(app_client):
    c, appmod, _ = app_client
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=appmod.app)
        async with httpx.AsyncClient(transport=transport, base_url="https://mmai.test") as ac:
            bad = [ac.post("/api/auth/login", json={"username": "anna", "password": "wrong"}) for _ in range(5)]
            await asyncio.sleep(0.05)  # let the bad logins start first
            t0 = time.perf_counter()
            h = await ac.get("/api/health")
            dt = time.perf_counter() - t0
            rs = await asyncio.gather(*bad)
            return h.status_code, dt, [r.status_code for r in rs]

    status, dt, codes = asyncio.run(run())
    assert status == 200 and codes == [401] * 5
    assert dt < 0.3, f"/api/health took {dt:.2f}s while bad logins were in flight"


def test_h2_lockout_after_repeated_failures(app_client, monkeypatch):
    c, appmod, sec = app_client
    monkeypatch.setattr(sec, "LOGIN_LOCK_FAILURES", 3)
    for _ in range(3):
        assert c.post("/api/auth/login", json={"username": "anna", "password": "wrong"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "anna", "password": TEST_PW["anna"]})
    assert r.status_code == 423 and "wait" in r.json()["detail"].lower()
    # other usernames are not affected and the lock is recorded in the audit log
    assert c.post("/api/auth/login", json={"username": "bill", "password": TEST_PW["bill"]}).status_code == 200
    log = c.get("/api/audit", headers=_staff(c, "bill")).json()
    assert any(x["action"] == "login_locked" and x["entity_id"] == "anna" for x in log)
    sec.login_guard.reset()
    assert c.post("/api/auth/login", json={"username": "anna", "password": TEST_PW["anna"]}).status_code == 200


def test_l4_unknown_user_takes_as_long_as_wrong_password(app_client):
    c, _, _ = app_client
    def t(user):
        t0 = time.perf_counter(); c.post("/api/auth/login", json={"username": user, "password": "wrong"}); return time.perf_counter() - t0
    a = min(t("anna") for _ in range(3)); b = min(t("nobody-here") for _ in range(3))
    assert abs(a - b) < 0.1


# ---------------- M1 / L1: no reflected CORS, security headers present ----------------
def test_m1_cors_not_reflected_and_l1_headers(app_client):
    c, _, _ = app_client
    r = c.get("/api/health", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors" in r.headers["content-security-policy"]
    pre = c.options("/api/public/enquiries", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in pre.headers}
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404


# ---------------- M2: size caps, length caps, honeypot, per-IP limits ----------------
def test_m2_body_and_field_limits(app_client):
    c, _, sec = app_client
    big = "x" * (sec.MAX_BODY_BYTES + 10)
    r = c.post("/api/public/drafts", content=json.dumps({"answers": {"intro": big}}), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    r = c.post("/api/public/drafts", json={"answers": {"intro": "y" * (sec.MAX_DRAFT_BYTES + 10)}})
    assert r.status_code == 422
    r = c.post("/api/public/enquiries", json=good_answers(intro="z" * 2001))
    assert r.status_code == 422 and "intro" in r.json()["errors"]
    r = c.post("/api/public/enquiries", json=good_answers(first_name="A" * 81))
    assert r.status_code == 422 and "first_name" in r.json()["errors"]
    assert c.post("/api/public/enquiries", json=good_answers(intro="z" * 2000)).status_code == 201
    # staff edits get the same caps
    h = _staff(c)
    eid = _items(c.get("/api/enquiries", headers=h).json())[0]["id"]
    assert c.patch(f"/api/enquiries/{eid}", json={"intro": "q" * 2001}, headers=h).status_code == 422
    assert c.patch(f"/api/enquiries/{eid}", json={"staff_notes": "n" * 100}, headers=h).status_code == 200


def test_m2_honeypot_and_rate_limit(app_client, monkeypatch):
    c, _, sec = app_client
    r = c.post("/api/public/enquiries", json=good_answers(company_website="http://spam.example"))
    assert r.status_code == 422 and "form" in r.json()["errors"]
    monkeypatch.setattr(sec, "RATE_LIMITING", True)
    monkeypatch.setattr(sec, "RATE_ENQUIRIES_PER_HOUR", 2)
    sec.limiter.reset()
    assert c.post("/api/public/enquiries", json=good_answers()).status_code == 201
    assert c.post("/api/public/enquiries", json=good_answers()).status_code == 201
    r = c.post("/api/public/enquiries", json=good_answers())
    assert r.status_code == 429
    # another address is not affected (the hosting proxy passes the visitor address in X-Forwarded-For)
    assert c.post("/api/public/enquiries", json=good_answers(), headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 201
    # honeypot submissions never consume a real enquiry id
    h = _staff(c)
    assert all(e["first_name"] == "Test" for e in _items(c.get("/api/enquiries", headers=h).json()))


# ---------------- M4: bad input shapes answer 422, never 500 ----------------
def test_m4_non_object_bodies_return_422(app_client):
    c, _, _ = app_client
    h = _staff(c)
    for path, hdrs in (("/api/auth/login", {}), ("/api/public/drafts", {}), ("/api/public/validate", {}), ("/api/enquiries", h)):
        for body in ("[]", "\"x\"", "not json", "123"):
            r = c.post(path, content=body, headers={"Content-Type": "application/json", **hdrs})
            assert r.status_code == 422, (path, body, r.status_code)
    assert c.post("/api/public/drafts", json={"answers": {"a": 1}, "step": "abc"}).status_code == 201
    assert c.post("/api/public/drafts", json={"answers": [1, 2]}).status_code == 422
    r = c.post("/api/public/enquiries", json=good_answers(languages={"a": 1}))
    assert r.status_code == 422


# ---------------- M5: the public meta route does not expose internal ids ----------------
def test_m5_public_meta_hides_enquiry_id(app_client):
    c, _, _ = app_client
    r = c.post("/api/public/enquiries", json=good_answers())
    tok = r.json()["mate_link"]
    info = c.get("/api/public/meta", params={"mate_token": tok}).json()["mate_link"]
    assert info["valid"] is True and "enquiry_id" not in info and info["enquirer_first_name"] == "Test"


# ---------------- L5: session tokens are stored hashed; legacy clear tokens are migrated ----------------
def test_l5_session_tokens_hashed(app_client):
    c, appmod, sec = app_client
    r = c.post("/api/auth/login", json={"username": "anna", "password": TEST_PW["anna"]})
    tok = r.json()["token"]
    rows = [x[0] for x in appmod.db.execute("SELECT token FROM sessions").fetchall()]
    assert tok not in rows and sec.token_hash(tok) in rows and all(len(t) == 64 for t in rows)
    h = {"Authorization": "Bearer " + tok}
    assert c.get("/api/auth/session", headers=h).status_code == 200
    anon = TestClient(appmod.app, base_url="https://mmai.test")  # no cookie: a stolen hash is not a usable token
    assert anon.get("/api/auth/session", headers={"Authorization": "Bearer " + sec.token_hash(tok)}).status_code == 401
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    assert c.get("/api/auth/session", headers=h).status_code == 401
    # legacy clear-text row -> hashed in place, still usable
    appmod.db.execute("INSERT INTO sessions(token, user_id, created_at, expires_at, kind) VALUES (?,?,?,?,?)",
                      ("legacy-clear-token", 1, "2026-10-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", "remembered"))
    appmod.db.commit()
    assert sec.hash_legacy_sessions(appmod.db) == 1
    assert anon.get("/api/auth/session", headers={"Authorization": "Bearer legacy-clear-token"}).status_code == 200


# ---------------- L7: phone enquiries are attributed to the signed-in person ----------------
def test_l7_staff_enquiry_attribution(app_client):
    c, _, _ = app_client
    reset = c.post("/api/auth/users/elaisa/reset-password", json={}, headers=_staff(c, "bill"))   # an ordinary staff sign-in
    token = c.post("/api/auth/login", json={"username": "elaisa", "password": reset.json()["temporary_password"]}).json()["token"]
    c.cookies.clear()
    elaisa = {"Authorization": "Bearer " + token}
    r = c.post("/api/enquiries", json={**good_answers(), "channel": "Phone", "staff_member": "Someone Else"}, headers=elaisa)
    assert r.status_code == 201 and r.json()["staff_member"] == "Elaisa Doctolero"
    c.cookies.clear()
    anna = _staff(c, "anna")   # Anna is a manager like Bill (ruling of 17 November 2026)
    r = c.post("/api/enquiries", json={**good_answers(email="a@example.com"), "channel": "Phone", "staff_member": "Someone Else"}, headers=anna)
    assert r.status_code == 201 and r.json()["staff_member"] == "Someone Else"
    c.cookies.clear()
    bill = _staff(c, "bill")
    r = c.post("/api/enquiries", json={**good_answers(email="b@example.com"), "channel": "Phone", "staff_member": "Someone Else"}, headers=bill)
    assert r.status_code == 201 and r.json()["staff_member"] == "Someone Else"
