"""Browser QA (headless Chromium) of the r7 introduction-edit round trip on a DISPOSABLE synthetic server: two staff pages, one draft.

    python3 tools/qa/qa_r7_intro_edit.py     # own temp dir (DB + screenshots, path printed), starts 127.0.0.1:8078, drives two pages, stops the server

What it checks (desktop 1280 viewport, three independent logged-in pages on the same group):
  1. a ready, reviewed synthetic group gets its introduction through the PRODUCTION capture/record path (POST /api/groups/{id}/introduction
     with the deterministic AI double from the test suite standing in for the model - no provider); the draft is current and the DB
     row carries the stable draft id and context hash;
  2. both pages render the textarea with THAT draft id and context hash (the client wiring the edit is bound to);
  3. page 1 saves an edit: the plain confirmation appears and the DB holds page 1's text as a staff-edited history row;
  4. page 2 (still showing the old draft) tries to save its own edit: the request is refused with the plain stale message
     ("Someone changed this draft since you opened it ..."), nothing is written, page 1's text is retained, no page errors;
  5. after that refusal page 2's Copy / Open in email / Save edits / Mark Introduced are switched off, the unsaved text is kept
     (read-only), the local label reads "Earlier draft: reload required" and a reload is offered; nothing is replaced behind
     the staff member's back;
  6. page 3 (another old page) clicks "Mark Introduced": refused with the plain "newer introduction draft" message, the group
     stays Proposed (the action is bound to the draft row the page showed, not substituted with the latest draft), controls off;
  7. page 2 reloads the group: it shows page 1's text with the new draft id, controls restored; its "Mark Introduced" now succeeds.
Disposable only: port 8078 (never 8079; refused if busy), its own temporary SQLite file, the environment scrubbed of every DB URL,
MMAI_* setting and credential before any import, worker/scheduler/mail/alerts FORCED off, and with every provider key dropped no
model is reachable (there is no separate AI off switch); no SMTP, no live data. Exit code 0 only when every check passed.
"""
import asyncio, json, os, re, signal, socket, sqlite3, subprocess, sys, tempfile, time, urllib.error, urllib.request

# ---- environment isolation, BEFORE any server import (same discipline as qa_d_lease_recovery.py) ----
_DROP = re.compile(r"^(MMAI_|PG|DATABASE_URL$|ANTHROPIC_|OPENAI_|PPLX_|PERPLEXITY_)|(KEY|TOKEN|SECRET|PASSWORD|SMTP|ALERT|WEBHOOK|SLACK)", re.I)
for _k in [k for k in os.environ if _DROP.search(k)]:
    del os.environ[_k]
os.environ.update({"MMAI_AI_QUEUE": "0", "MMAI_SCHEDULER": "0", "MMAI_MAIL_MODE": "off", "MMAI_ALERT_MODE": "off",
                   "MMAI_RATE_LIMITING": "0", "MMAI_TRUST_VISITOR_ID": "1", "PYTHONUNBUFFERED": "1"})

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
PORT = 8078   # fixed: the disposable QA port; 8079 is the shared preview and is never used here
BASE = f"http://127.0.0.1:{PORT}/"
PW = "Qa-R7-8078!"
WORK = tempfile.mkdtemp(prefix="mmai-qa-r7-")   # the harness owns its DB file and never removes a path it did not create
DB = os.path.join(WORK, "qa-r7.db")
os.environ["MMAI_DB"] = DB
os.environ["MMAI_INITIAL_PASSWORD_BILL"] = PW
SHOTS = os.path.join(WORK, "shots")
os.makedirs(SHOTS, exist_ok=True)
STALE = "Someone changed this draft since you opened it"
SAVED = "Saved as a staff-edited draft."


def refuse_busy_port():
    """Fail before seeding if anything already listens on the QA port: the health poll must only ever reach OUR child."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sk.bind(("0.0.0.0", PORT))
        except OSError as ex:
            raise SystemExit(f"port {PORT} is busy ({ex}); refusing to run against a server this harness did not start")


refuse_busy_port()
ok = lambda tag, *rest: print("OK", tag, *rest)


def seed():
    from pytest import MonkeyPatch
    from fastapi.testclient import TestClient
    import server.db as dbmod
    import server.app as appmod
    from server import ai, ai_queue
    from server.db import row
    from tests.test_t6_grounding import _party_group
    from tests.test_t6_staff_api import _fake_draft
    from tests.conftest import fake_staff_briefs
    mp = MonkeyPatch()
    mp.setattr(ai, "staff_briefs", fake_staff_briefs)
    mp.setattr(ai, "draft_introduction", _fake_draft)   # the model double; capture, guard, CAS and record are the production code
    dbmod._conn = None
    client = TestClient(appmod.app)
    db = appmod.db
    out = {}
    with client:
        a, lead, solo, g = _party_group(db, mp)
        gid = g["id"]
        assert ai_queue.can_circulate(db, g)["ok"]
        r = client.post("/api/auth/login", json={"username": "bill", "password": PW}, headers={"X-Visitor-Id": "qa-seed"}); assert r.status_code == 200, r.text
        client.headers.update({"Authorization": "Bearer " + r.json()["token"]})
        r = client.post(f"/api/groups/{gid}/introduction"); assert r.status_code == 200, r.text
        assert r.json()["current"] is True and r.json()["basis"] == "AI", r.json()
        d = client.get(f"/api/groups/{gid}").json()
        i = d["intro"]
        assert i["current"] and i["draft_id"] and i["context_hash"], i
        last = row(db.execute("SELECT id, body_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT 1", (gid,)))
        assert last["id"] == i["draft_id"] and json.loads(last["body_json"])["context_hash"] == i["context_hash"]
        out.update({"gid": gid, "draft_id": i["draft_id"], "context_hash": i["context_hash"], "text": i["text"],
                    "history": db.execute("SELECT COUNT(*) FROM group_briefs WHERE group_id=? AND kind='intro'", (gid,)).fetchone()[0],
                    "edits": db.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_intro_edit'").fetchone()[0]})
    db.commit()
    try:
        db.close()   # the disposable server must be the only connection (journal mode / schema steps at start-up)
    except Exception:
        pass
    dbmod._conn = None
    mp.undo()   # the test fixtures patch time.sleep to a no-op; the server wait below needs the real clock
    return out


def start_server():
    env = dict(os.environ, PORT=str(PORT))   # the sanitised environment from the top of this file, nothing inherited beyond it
    p = subprocess.Popen([sys.executable, "-m", "server.app"], env=env, stdout=open(f"{SHOTS}/server.log", "w"), stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 60   # wall clock, not an iteration count
    while p.poll() is None and time.monotonic() < deadline:
        try:
            urllib.request.urlopen(BASE + "api/health", timeout=1); return p
        except Exception:
            time.sleep(0.25)
    p.kill(); raise RuntimeError(f"server did not start (exit {p.poll()}); see " + f"{SHOTS}/server.log")


def api_login():
    req = urllib.request.Request(BASE + "api/auth/login", data=json.dumps({"username": "bill", "password": PW}).encode(), headers={"Content-Type": "application/json", "X-Visitor-Id": "qa-api"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["token"]


def api(method, path, tok, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method=method)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def db_state(gid):
    c = sqlite3.connect(DB)
    try:
        text = c.execute("SELECT intro_text FROM groups WHERE id=?", (gid,)).fetchone()[0]
        hist = c.execute("SELECT id, body_json FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id", (gid,)).fetchall()
        edits = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_intro_edit'").fetchone()[0]
        return text, [(h[0], json.loads(h[1])) for h in hist], edits
    finally:
        c.close()


async def login(pg, visitor):
    await pg.goto(BASE + "#/staff"); await pg.wait_for_selector("input[name=username]")
    await pg.fill("input[name=username]", "bill"); await pg.fill("input[name=password]", PW); await pg.keyboard.press("Enter")
    await pg.wait_for_selector("#view-staff .side, #view-staff nav", timeout=10000)
    await pg.wait_for_selector('[data-role="dataset"] input[value=test]', timeout=10000)
    await pg.check('[data-role="dataset"] input[value=test]'); await pg.wait_for_timeout(500)


async def open_group(pg, gid):
    await pg.goto(BASE + f"#/staff/groups/{gid}")
    await pg.wait_for_selector('[data-role="intro"]', state="attached", timeout=10000)
    await pg.wait_for_timeout(400)


async def wait_msg(pg, timeout=10.0, role="imsg"):
    # the page's Content Security Policy forbids evaluated scripts, so poll the element's text from here (no page-side eval)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = (await pg.locator(f'[data-role="{role}"]').first.text_content() or "").strip()
        if t:
            return t, (await pg.locator(f'[data-role="{role}"]').first.get_attribute("class") or "")
        await pg.wait_for_timeout(100)
    raise AssertionError("no message appeared within %.0fs" % timeout)


async def controls(pg):
    """Enabled/visible state of the controls that act on the draft shown by this page."""
    loc = lambda sel: pg.locator(sel).first
    return {"copy": await loc('[data-action="copy"]').is_enabled(), "save": await loc('[data-action="save-intro"]').is_enabled(),
            "introduce": await loc('[data-status="Introduced"]').is_enabled(), "mailto": await loc('[data-role="mailto"]').is_visible(),
            "reload": await loc('[data-action="reload-group"]').is_visible(), "readonly": await loc('[data-role="intro"]').get_attribute("readonly")}


LIVE = {"copy": True, "save": True, "introduce": True, "mailto": True, "reload": False, "readonly": None}
DEAD = {"copy": False, "save": False, "introduce": False, "mailto": False, "reload": True, "readonly": ""}


async def binding(pg):
    ta = pg.locator('[data-role="intro"]')
    return {"draft_id": await ta.get_attribute("data-draft-id"), "context_hash": await ta.get_attribute("data-context-hash"),
            "readonly": await ta.get_attribute("readonly"), "value": await ta.input_value()}


async def checks(seeded):
    from playwright.async_api import async_playwright
    gid = seeded["gid"]
    tok = api_login()
    async with async_playwright() as p:
        b = await p.chromium.launch()
        vp = {"width": 1280, "height": 900}
        errors = []
        pages = []
        for n in (1, 2, 3):   # three independent sessions (the token lives only in page memory), all on the same group
            ctx = await b.new_context(viewport=vp, device_scale_factor=1)
            pg = await ctx.new_page()
            pg.on("pageerror", lambda e, n=n: errors.append(f"page{n}: {e}"))
            await login(pg, f"qa-page-{n}")
            await open_group(pg, gid)
            pages.append(pg)
        p1, p2, p3 = pages
        for n, pg in enumerate(pages, 1):
            bd = await binding(pg)
            assert bd["draft_id"] == str(seeded["draft_id"]) and bd["context_hash"] == seeded["context_hash"], (n, bd, seeded)
            assert bd["readonly"] is None and bd["value"].strip() == seeded["text"].strip(), (n, bd)
            assert not await pg.locator('[data-action="save-intro"]').is_disabled()
        ok(f"all pages bound to draft {seeded['draft_id']} / context {seeded['context_hash'][:12]}… (textarea editable, Save enabled)")
        await p1.screenshot(path=f"{SHOTS}/1-page1-before-edit.png", full_page=True)
        # page 1 saves its edit
        text1 = seeded["text"].rstrip() + "\n\nEdited on page one: the move-in date is confirmed."
        await p1.fill('[data-role="intro"]', text1)
        await p1.click('[data-action="save-intro"]')
        m, cls = await wait_msg(p1)
        assert m == SAVED and "ok" in cls.split(), (m, cls)
        await p1.screenshot(path=f"{SHOTS}/2-page1-saved.png", full_page=True)
        text, hist, edits = db_state(gid)
        assert text == text1, text
        assert len(hist) == seeded["history"] + 1 and hist[-1][1]["basis"] == "staff" and hist[-1][1]["by"] and hist[-1][1]["text"] == text1 and hist[-1][0] != seeded["draft_id"], hist[-1]
        assert hist[-1][1]["context_hash"] == seeded["context_hash"], "an unchanged context is carried, never re-stamped"
        assert edits == seeded["edits"] + 1
        new_draft_id = hist[-1][0]
        ok(f"page 1 edit saved: groups.intro_text updated, staff history row {new_draft_id} added, one edit audit")
        # page 2 still shows the old draft and tries to save its own edit: refused, nothing written
        bd2 = await binding(p2)
        assert bd2["draft_id"] == str(seeded["draft_id"]), bd2
        text2 = seeded["text"].rstrip() + "\n\nEdited on page two: a different sentence."
        await p2.fill('[data-role="intro"]', text2)
        await p2.click('[data-action="save-intro"]')
        m, cls = await wait_msg(p2)
        assert m.startswith(STALE) and "err" in cls.split(), (m, cls)
        assert "409" not in m and "draft_id" not in m and "context_hash" not in m and "hash" not in m.lower(), m
        await p2.wait_for_timeout(800)
        await p2.screenshot(path=f"{SHOTS}/3-page2-stale-refused.png", full_page=True)
        text, hist, edits = db_state(gid)
        assert text == text1 and len(hist) == seeded["history"] + 1 and edits == seeded["edits"] + 1, "page 2's refused edit wrote nothing"
        ok(f"page 2 refused with the plain stale message: {m!r}; page 1's text retained, no history/audit row")
        # after the refusal this page's copy of the draft is not current: copy / email / save / Mark Introduced are off, the unsaved
        # text is still there (read-only) and the page offers to reload the group
        st = await controls(p2)
        assert st == DEAD, st
        assert (await p2.locator('[data-role="intro"]').input_value()) == text2, "unsaved text is preserved, not replaced"
        assert "switched off until you reload" in m and "remains in the text box" in m
        assert (await p2.locator('[data-role="intro-label"]').text_content()) == "Earlier draft: reload required"
        ok("page 2 after the refusal: Copy, Open in email, Save edits and Mark Introduced are off, unsaved text kept, reload offered")
        # the API agrees with what both pages did
        d = api("GET", f"api/groups/{gid}", tok)
        assert d["intro"]["text"] == text1 and d["intro"]["draft_id"] == new_draft_id and d["intro"]["current"] is True, d["intro"]
        # page 3 (another old page, untouched so far) marks the group introduced: bound to the OLD row -> refused, group stays Proposed
        assert await controls(p3) == LIVE
        await p3.click('[data-status="Introduced"]')
        m, cls = await wait_msg(p3)
        assert m.startswith("A newer introduction draft exists for this group since you opened the page.") and "err" in cls.split(), (m, cls)
        assert "hash" not in m.lower() and "409" not in m, m
        await p3.wait_for_timeout(500)
        await p3.screenshot(path=f"{SHOTS}/3b-page3-introduced-refused.png", full_page=True)
        d = api("GET", f"api/groups/{gid}", tok)
        assert d["status"] == "Proposed" and d["intro"]["draft_id"] == new_draft_id, (d["status"], d["intro"]["draft_id"])
        assert db_state(gid)[2] == seeded["edits"] + 1 and len(db_state(gid)[1]) == seeded["history"] + 1
        st = await controls(p3)
        assert st == DEAD, st
        assert (await p3.locator('[data-role="intro-label"]').text_content()) == "Earlier draft: reload required"
        ok(f"page 3's Mark Introduced refused with the plain message: {m!r}; group still Proposed, nothing written, controls off")
        # page 2 uses the offered reload: now bound to the new row, page 1's text shown, controls restored
        await p2.click('[data-action="reload-group"]')
        await p2.wait_for_timeout(300)
        await p2.wait_for_selector('[data-role="intro"]', state="attached", timeout=10000)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (await binding(p2))["draft_id"] != str(new_draft_id):
            await p2.wait_for_timeout(200)
        bd2 = await binding(p2)
        assert await controls(p2) == LIVE, await controls(p2)
        assert (await p2.locator('[data-role="intro-label"]').text_content()) == "Current introduction draft"
        assert bd2["draft_id"] == str(new_draft_id) and bd2["value"].strip() == text1.strip() and bd2["readonly"] is None, bd2
        await p2.screenshot(path=f"{SHOTS}/4-page2-after-reload.png", full_page=True)
        ok(f"page 2 after reload: bound to draft {new_draft_id}, shows page 1's text, controls restored")
        await p2.click('[data-status="Introduced"]')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and api("GET", f"api/groups/{gid}", tok)["status"] != "Introduced":
            await p2.wait_for_timeout(200)
        d = api("GET", f"api/groups/{gid}", tok)
        assert d["status"] == "Introduced", d["status"]
        await p2.wait_for_timeout(800)
        await p2.screenshot(path=f"{SHOTS}/5-page2-introduced.png", full_page=True)
        assert "Introduced" in (await p2.inner_text("#view-staff"))
        ok("page 2 after reload: Mark Introduced accepted with the current draft's binding")
        assert not errors, errors
        ok("no page errors")
        await b.close()


def main():
    seeded = seed()
    ok("seeded via production capture/record (model double): group", seeded["gid"], "draft", seeded["draft_id"])
    srv = start_server()
    try:
        asyncio.run(checks(seeded))
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(timeout=10)
        except subprocess.TimeoutExpired:
            srv.kill()
    print("ALL PASSED; screenshots in", SHOTS)


if __name__ == "__main__":
    main()
