"""Controlled pilot gate (16 Nov 2026, MMAI_PILOT_GATE): the whole site sits behind an allowed staff sign-in.

Covers: refusal without sign-in and no side effects from refused requests; the manager's cookie and Bearer sign-in; other staff
refused; expired, disabled and signed-out sessions; the minimal health reply; the sign-in page files; TEST enforcement on every
way of creating an enquiry or saved form; headers; and unchanged behaviour while the gate is off.
"""
import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_PW
from tests.test_data_and_api import good_answers

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PILOT_DIR = os.path.join(ROOT, "server", "pilot")
COOKIE = "__Host-mmai_session"


def _client(tmp_path, monkeypatch, gate):
    monkeypatch.setenv("MMAI_DB", str(tmp_path / "pilot.db"))
    if gate is None:
        monkeypatch.delenv("MMAI_PILOT_GATE", raising=False)
    else:
        monkeypatch.setenv("MMAI_PILOT_GATE", gate)
    monkeypatch.delenv("MMAI_PILOT_USERS", raising=False)
    import server.db as dbmod
    dbmod._conn = None
    import server.app as appmod
    importlib.reload(appmod)
    from server import security as sec
    sec.limiter.reset()
    sec.login_guard.reset()
    return TestClient(appmod.app, base_url="https://mmai.test", follow_redirects=False), appmod, sec


@pytest.fixture
def gated(tmp_path, monkeypatch):
    c, appmod, sec = _client(tmp_path, monkeypatch, "1")
    yield c, appmod, sec
    sec.limiter.reset()
    sec.login_guard.reset()


@pytest.fixture
def ungated(tmp_path, monkeypatch):
    c, appmod, sec = _client(tmp_path, monkeypatch, None)
    yield c, appmod, sec
    sec.limiter.reset()
    sec.login_guard.reset()


def _login(c, user="bill"):
    return c.post("/api/auth/login", json={"username": user, "password": TEST_PW[user]})


def _counts(appmod):
    db = appmod.db
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("enquiries", "form_drafts", "sessions", "audit_log", "homebot_enquiries")}


# The requests an outsider could try. None may reach a handler.
OUTSIDER_REQUESTS = [
    ("GET", "/api/apartments", None), ("GET", "/api/public/meta", None), ("GET", "/api/auth/me", None),
    ("GET", "/api/enquiries", None), ("GET", "/api/public/homebot/meta", None), ("GET", "/api/auth/users", None),
    ("GET", "/api/homebot/enquiries", None), ("GET", "/api/public/countries/normalise?q=Sweden", None),
    ("POST", "/api/public/enquiries", "answers"), ("POST", "/api/public/validate", "answers"),
    ("POST", "/api/public/drafts", "draft"), ("POST", "/api/public/homebot/enquiries", "answers"),
    ("POST", "/api/public/homebot/handoff", "handoff"), ("PUT", "/api/public/drafts/abc", "draft"), ("POST", "/api/auth/logout", None),
    ("POST", "/api/enquiries", "answers"), ("DELETE", "/api/enquiries/1", None), ("PUT", "/api/auth/login", None),
    ("HEAD", "/api/health", None), ("POST", "/api/health", None), ("GET", "/api/health/", None), ("GET", "/api/auth/login", None),
    ("GET", "/openapi.json", None), ("GET", "/docs", None), ("GET", "/redoc", None),
    ("GET", "/app.js", None), ("GET", "/api.js", None), ("GET", "/styles.css", None), ("GET", "/homebot/app.js", None),
    ("GET", "/pilot/../app.js", None), ("GET", "/pilot/index.html", None), ("GET", "/pilot/pilot.js.map", None),
    ("GET", "/pilot//pilot.js", None), ("GET", "/PILOT/", None), ("POST", "/pilot/", None),
]


def _body(kind):
    if kind == "answers":
        return good_answers()
    if kind == "draft":
        return {"answers": {"first_name": "Outsider"}, "step": 1}
    if kind == "handoff":
        return {"headcount_now": 1, "headcount_target": 3}
    return None


# ---------------- refusal without sign-in ----------------
def test_outsider_is_refused_everywhere_and_nothing_runs(gated, monkeypatch):
    c, appmod, _ = gated
    woke = []
    monkeypatch.setattr(appmod.scheduler, "catch_up_if_overdue", lambda *a, **k: woke.append("scheduler"))
    monkeypatch.setattr(appmod.group_responses, "sweep_if_due", lambda *a, **k: woke.append("sweep"))
    before = _counts(appmod)
    for method, path, kind in OUTSIDER_REQUESTS:
        r = c.request(method, path, json=_body(kind), headers={"X-Visitor-Id": "outsider-device", "Accept": "application/json"})
        assert r.status_code == 401, (method, path, r.status_code)
        assert COOKIE not in r.headers.get("set-cookie", ""), (method, path)
        assert "no-store" in r.headers["cache-control"], (method, path)
    assert woke == [], "a refused request must not wake the scheduler or the sweep"
    assert _counts(appmod) == before, "a refused request must not write anything"
    # the addresses that do answer without sign-in wake nothing either, and only a failed sign-in leaves a trace (its audit line)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/session").status_code == 401
    assert c.get("/pilot/").status_code == 200
    assert woke == []
    assert _counts(appmod) == before
    assert c.post("/api/auth/login", json={"username": "bill", "password": "not-the-password"}).status_code == 401
    assert woke == []
    after = _counts(appmod)
    assert after["sessions"] == before["sessions"] and after["enquiries"] == before["enquiries"]
    # a signed-in pilot user's API call wakes background work as it always did
    assert _login(c).status_code == 200
    assert c.get("/api/apartments").status_code == 200
    assert "scheduler" in woke and "sweep" in woke


def test_outsider_page_visit_is_sent_to_the_sign_in_page(gated):
    c, _, _ = gated
    html = {"Accept": "text/html,application/xhtml+xml"}
    r = c.get("/", headers=html)
    assert r.status_code == 303 and r.headers["location"] == "/pilot/"
    r = c.get("/homebot/?test=1", headers=html)
    assert r.status_code == 303 and r.headers["location"] == "/pilot/?next=%2Fhomebot%2F%3Ftest%3D1"
    r = c.get("/index.html", headers=html)
    assert r.status_code == 303 and r.headers["location"].startswith("/pilot/?next=")
    # an API address never redirects, whatever the Accept header says, and a page POST is refused, not redirected
    assert c.get("/api/apartments", headers=html).status_code == 401
    assert c.post("/", headers=html).status_code == 401
    for r in (c.get("/", headers=html), c.get("/app.js")):
        assert "<html" not in r.text.lower() and "MMAI_API" not in r.text


def test_wrong_or_made_up_credentials_do_not_pass(gated):
    c, appmod, _ = gated
    assert c.get("/api/apartments", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401
    assert c.get("/api/apartments", cookies={COOKIE: "made-up"}).status_code == 401
    c.cookies.clear()
    # the preview-only visitor header is ignored while the gate is on, even with a remembered manager session for that device
    assert _login(c).status_code == 200
    c.cookies.clear()
    r = c.get("/api/auth/session", headers={"X-Visitor-Id": "testclient-device"})
    assert r.status_code == 401
    r = c.post("/api/auth/login", json={"username": "bill", "password": "wrong-password"})
    assert r.status_code == 401 and COOKIE not in r.headers.get("set-cookie", "")


# ---------------- the manager's sign-in ----------------
def test_manager_cookie_sign_in_opens_the_site(gated):
    c, _, _ = gated
    r = _login(c)
    assert r.status_code == 200 and r.json()["username"] == "bill"
    sc = r.headers["set-cookie"]
    assert sc.startswith(COOKIE + "=") and "HttpOnly" in sc and "Secure" in sc and "Path=/" in sc and "samesite=lax" in sc.lower()
    # cookie only (no Bearer header): pages, files and API all answer
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 200
    assert c.get("/app.js").status_code == 200
    assert c.get("/homebot/").status_code == 200
    assert c.get("/api/public/meta").status_code == 200
    assert c.get("/api/apartments").status_code == 200
    assert c.get("/api/auth/session").json()["username"] == "bill"
    full = c.get("/api/health").json()
    assert full["ok"] is True and "apartments" in full and "database" in full


def test_manager_bearer_token_also_passes(gated):
    c, _, _ = gated
    tok = _login(c).json()["token"]
    c.cookies.clear()
    assert c.get("/api/apartments").status_code == 401
    assert c.get("/api/apartments", headers={"Authorization": "Bearer " + tok}).status_code == 200


def test_other_staff_are_refused_and_get_no_session(gated):
    c, appmod, _ = gated
    before = appmod.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    r = _login(c, "anna")
    assert r.status_code == 403 and "token" not in r.json() and COOKIE not in r.headers.get("set-cookie", "")
    assert appmod.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == before
    assert c.get("/api/apartments").status_code == 401


def test_staff_session_made_before_the_gate_does_not_pass(gated, monkeypatch):
    c, appmod, _ = gated
    monkeypatch.delenv("MMAI_PILOT_GATE")
    tok = _login(c, "anna").json()["token"]           # signed in while the gate was off
    assert c.get("/api/enquiries").status_code == 200
    monkeypatch.setenv("MMAI_PILOT_GATE", "1")
    assert c.get("/api/enquiries").status_code == 401
    assert c.get("/api/auth/session").status_code == 401
    c.cookies.clear()
    assert c.get("/api/enquiries", headers={"Authorization": "Bearer " + tok}).status_code == 401
    # a non-pilot cookie is not rescued by a pilot Bearer token: the first valid sign-in decides, as in the handlers
    bill = _login(c).json()["token"]
    c.cookies.clear()
    r = c.get("/api/enquiries", headers={"Authorization": "Bearer " + bill}, cookies={COOKIE: tok})
    assert r.status_code == 401


def test_named_pilot_users_replace_the_default(gated, monkeypatch):
    c, _, _ = gated
    monkeypatch.setenv("MMAI_PILOT_USERS", "anna")
    assert _login(c, "bill").status_code == 403
    assert _login(c, "anna").status_code == 200
    assert c.get("/api/apartments").status_code == 200


def test_expired_session_is_refused(gated):
    c, appmod, _ = gated
    assert _login(c).status_code == 200
    assert c.get("/api/apartments").status_code == 200
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat()
    appmod.db.execute("UPDATE sessions SET expires_at=?", (past,))
    appmod.db.commit()
    assert c.get("/api/apartments").status_code == 401
    assert c.get("/api/auth/session").status_code == 401
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 303
    assert c.get("/api/health").json() == {"ok": True}


def test_disabled_account_is_refused(gated):
    c, appmod, _ = gated
    assert _login(c).status_code == 200
    appmod.db.execute("UPDATE staff_users SET disabled=1 WHERE username='bill'")
    appmod.db.commit()
    assert c.get("/api/apartments").status_code == 401
    assert c.get("/app.js").status_code == 401
    c.cookies.clear()
    assert _login(c).status_code == 401


def test_sign_out_closes_the_site_again(gated):
    c, appmod, _ = gated
    tok = _login(c).json()["token"]
    r = c.post("/api/auth/logout")
    assert r.status_code == 200
    assert COOKIE + '=""' in r.headers["set-cookie"] or "Max-Age=0" in r.headers["set-cookie"]
    assert appmod.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert c.get("/api/apartments").status_code == 401
    # the old token is dead too, by cookie or by Bearer
    assert c.get("/api/apartments", cookies={COOKIE: tok}).status_code == 401
    c.cookies.clear()
    assert c.get("/api/apartments", headers={"Authorization": "Bearer " + tok}).status_code == 401
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 303


# ---------------- health ----------------
def test_health_is_minimal_without_sign_in(gated):
    c, _, _ = gated
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "no-store" in r.headers["cache-control"]


def test_gate_fails_closed_when_the_database_is_unreachable(gated, monkeypatch):
    c, appmod, _ = gated
    assert _login(c).status_code == 200

    def boom(_tok):
        raise RuntimeError("database password=hunter2 unreachable")
    monkeypatch.setattr(appmod, "_session_row", boom)
    r = c.get("/api/apartments")
    assert r.status_code == 503 and "hunter2" not in r.text and "RuntimeError" not in r.text


# ---------------- the sign-in page ----------------
def test_sign_in_page_is_served_without_sign_in(gated):
    c, _, _ = gated
    r = c.get("/pilot")
    assert r.status_code == 307 and r.headers["location"] == "/pilot/"
    r = c.get("/pilot?next=%2Fhomebot%2F")
    assert r.headers["location"] == "/pilot/?next=%2Fhomebot%2F"
    page = c.get("/pilot/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert c.get("/pilot/pilot.js").headers["content-type"].startswith("text/javascript")
    assert c.get("/pilot/pilot.css").headers["content-type"].startswith("text/css")
    for r in (page, c.get("/pilot/pilot.js"), c.get("/pilot/pilot.css")):
        assert r.status_code == 200
        assert "no-store" in r.headers["cache-control"] and "private" in r.headers["cache-control"]
        assert r.headers["strict-transport-security"].startswith("max-age=")
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self'" in r.headers["content-security-policy"]


def test_sign_in_page_files_hold_no_secrets_and_no_inline_script():
    names = sorted(os.listdir(PILOT_DIR))
    assert names == ["index.html", "pilot.css", "pilot.js"]
    text = {n: open(os.path.join(PILOT_DIR, n), encoding="utf-8").read() for n in names}
    joined = "\n".join(text.values())
    for pw in TEST_PW.values():
        assert pw not in joined
    low = joined.lower()
    for word in ("api_key", "apikey", "secret", "sk-ant", "client_secret", "database_url", "postgres", "localstorage",
                 "sessionstorage", "document.cookie", "innerhtml", "eval(", "http://", "https://"):
        assert word not in low, word
    html = text["index.html"]
    assert html.count("<script") == 1 and '<script src="/pilot/pilot.js" defer></script>' in html
    assert "onclick" not in html.lower() and "onsubmit" not in html.lower() and "style=" not in html.lower()
    assert "<style" not in html.lower()
    assert 'type="password"' in html and 'autocomplete="current-password"' in html
    assert "noindex" in html
    # plain staff language: no rule-register codes on the page (T10)
    import re
    assert not re.search(r"\b(?:T|H|GP|I)\d+(?:\([a-z]\))?", html)
    # the script only calls this site's own sign-in addresses
    assert set(re.findall(r"'(/api/[^']*)'", text["pilot.js"])) == {"/api/auth/session", "/api/auth/login", "/api/auth/logout"}


def test_next_target_only_accepts_plain_local_paths():
    from server import security as sec
    assert sec.pilot_next_target("/homebot/", "test=1") == "/homebot/?test=1"
    assert sec.pilot_next_target("/", "") == "/"
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil.example", "evil", "", "/a\nb"):
        assert sec.pilot_next_target(bad, "") == "", bad
    for own in ("/pilot", "/pilot/", "/pilot/pilot.js"):
        assert sec.pilot_next_target(own, "") == ""


def test_open_addresses_are_an_exact_list():
    from server import security as sec
    assert sec.pilot_open_path("GET", "/pilot/") and sec.pilot_open_path("HEAD", "/pilot/pilot.css")
    assert sec.pilot_open_path("POST", "/api/auth/login") and sec.pilot_open_path("GET", "/api/health")
    assert sec.pilot_open_path("GET", "/api/auth/session")
    for method, path in (("POST", "/pilot/"), ("GET", "/api/auth/login"), ("POST", "/api/auth/logout"), ("GET", "/api/health/x"),
                         ("GET", "/api/auth/session/"), ("GET", "/pilot/x"), ("GET", "/api/auth/me"), ("DELETE", "/api/health")):
        assert not sec.pilot_open_path(method, path), (method, path)


def test_gate_setting_fails_closed():
    from server import security as sec
    for off in (None, "", "0", "off", "false", "no", " OFF "):
        os.environ.pop("MMAI_PILOT_GATE", None)
        if off is not None:
            os.environ["MMAI_PILOT_GATE"] = off
        assert sec.pilot_gate_enabled() is False, off
    try:
        for on in ("1", "on", "true", "yes", "enabled", "anything-else"):
            os.environ["MMAI_PILOT_GATE"] = on
            assert sec.pilot_gate_enabled() is True, on
    finally:
        os.environ.pop("MMAI_PILOT_GATE", None)


# ---------------- everything new is TEST ----------------
def test_every_new_enquiry_and_saved_form_is_test(gated):
    c, appmod, _ = gated
    db = appmod.db
    assert _login(c).status_code == 200

    r = c.post("/api/public/enquiries", json=good_answers(is_test=False))
    assert r.status_code == 201, r.text
    r = c.post("/api/public/enquiries", json=good_answers(email="second.pilot@example.com"))   # is_test not sent at all
    assert r.status_code == 201, r.text
    r = c.post("/api/enquiries", json=good_answers(email="phone.pilot@example.com", is_test=False))
    assert r.status_code in (200, 201), r.text
    assert db.execute("SELECT COUNT(*) FROM enquiries").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM enquiries WHERE COALESCE(is_test,0)=0").fetchone()[0] == 0

    r = c.post("/api/public/drafts", json={"answers": {"first_name": "Pilot"}, "step": 1, "is_test": False})
    assert r.status_code in (200, 201), r.text
    r = c.post("/api/public/homebot/handoff", json={"headcount_now": 1, "headcount_target": 3, "is_test": False})
    assert r.status_code in (200, 201), r.text
    assert "test=1" in r.json()["resume"]
    assert db.execute("SELECT COUNT(*) FROM form_drafts").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM form_drafts WHERE COALESCE(is_test,0)=0").fetchone()[0] == 0

    from tests.test_homebot import body as homebot_body
    monkey_free = os.environ.pop("MMAI_HOMEBOT_REVIEW_BUILD", None)   # the review build already forces TEST; prove the gate does it alone
    try:
        r = c.post("/api/public/homebot/enquiries", json=homebot_body(is_test=False), headers={"X-Submission-Key": "pilot-homebot-key-0001"})
    finally:
        if monkey_free is not None:
            os.environ["MMAI_HOMEBOT_REVIEW_BUILD"] = monkey_free
    assert r.status_code == 201, r.text
    assert db.execute("SELECT COUNT(*) FROM homebot_enquiries").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM homebot_enquiries WHERE COALESCE(is_test,0)=0").fetchone()[0] == 0


def test_server_functions_force_test_under_the_gate(gated):
    """Defence in depth below the web handlers: the storage functions themselves refuse to store real data during the pilot."""
    c, appmod, _ = gated
    from server import enquiries, homebot_handoff
    from server.db import now_iso
    e = enquiries.create_enquiry(appmod.db, good_answers(email="direct.pilot@example.com"), "Landing page", is_test=False, use_ai_countries=False)
    assert appmod.db.execute("SELECT is_test FROM enquiries WHERE id=?", (e["id"],)).fetchone()[0] == 1
    d = homebot_handoff.create(appmod.db, 1, 3, is_test=False, now_iso=now_iso(), expires_iso=now_iso())
    appmod.db.commit()
    assert d["is_test"] is True and "test=1" in d["resume"]


# ---------------- gate off: nothing changes ----------------
def test_gate_off_keeps_the_existing_behaviour(ungated):
    c, appmod, _ = ungated
    assert c.get("/pilot").status_code == 404
    assert c.get("/pilot/").status_code == 404
    assert c.get("/pilot/pilot.js").status_code == 404
    assert c.get("/", headers={"Accept": "text/html"}).status_code == 200
    assert c.get("/api/public/meta").status_code == 200
    h = c.get("/api/health")
    assert "apartments" in h.json() and "strict-transport-security" not in h.headers
    assert c.get("/api/apartments").status_code == 401            # staff-only, as before
    assert _login(c, "anna").status_code == 200                   # every staff member signs in as before
    assert c.get("/api/enquiries").status_code == 200
    r = c.post("/api/public/enquiries", json=good_answers())
    assert r.status_code == 201
    assert appmod.db.execute("SELECT COALESCE(is_test,0) FROM enquiries").fetchone()[0] == 0   # a real enquiry stays real
