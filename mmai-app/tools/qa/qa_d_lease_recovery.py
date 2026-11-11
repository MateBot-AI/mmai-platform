"""Browser QA (headless Chromium) of the D lease follow-up recovery path on a DISPOSABLE synthetic server.

    python3 tools/qa/qa_d_lease_recovery.py     # own temp dir (DB + screenshots, path printed), starts 127.0.0.1:8078, drives the page, stops the server

What it checks, desktop (1280) and phone (390) viewports, screenshots under <tempdir>/shots:
  1. apartment A is leased to group G1 through the real API; the follow-up for a second group G2 is then RECONSTRUCTED from the
     data (G2's stored option list is put back to "still lists A at rank 2", as if its step never ran) - not an injected browser
     or request failure; pending_lease_reranks derives the pending work from exactly that data - no schema, no memory;
  2. the apartment page shows the plain warning ("still need to be moved ...") and the "Finish updating other groups" button;
  3. clicking it posts the same lease request with the apartment's own group binding: the message says the lease was NOT
     repeated and the other group was updated; the DB shows one claim (leased_at unchanged) and OPTION TRIMMING for G2: its
     stored option for A is removed and the group is flagged, while G2 stays on its own apartment B (nothing is re-assigned);
  4. after reload the warning is gone and the button remains (idempotent: a second click trims nothing and claims nothing).
Disposable only: port 8078 (never 8079; refused if busy), its own temporary SQLite file, the environment scrubbed of every DB URL,
MMAI_* setting and credential before any import, worker/scheduler/mail/alerts FORCED off, and with every provider key dropped no
model is reachable (there is no separate AI off switch; MMAI_AI_MODE is not one) - the deterministic AI doubles from the test suite
stand in; no SMTP, no live data. Scope: this harness exercises the served client as-is; byte-level identity of the served files
with a reviewed freeze, and the Node mapping check for staff.js text, are separate pieces of evidence recorded in the QA log.
Exit code 0 only when every check passed.
"""
import asyncio, json, os, re, signal, socket, subprocess, sys, tempfile, time, urllib.request

# ---- environment isolation, BEFORE any server import: this process and the disposable server see only what is set here ----
# Inherited DATABASE_URL / MMAI_TEST_DATABASE_URL would override MMAI_DB inside server.db; provider, SMTP and alert settings must
# not leak into a QA run. Everything MMAI_*, every DB URL and anything that looks like a credential is dropped, then the safe
# values are FORCED (not setdefault): no worker thread, no scheduler, mail and alerts off; with every key dropped no model is reachable.
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
PW = "Qa-D-8078!"
WORK = tempfile.mkdtemp(prefix="mmai-qa-d-")   # the harness owns its DB file and never removes a path it did not create
DB = os.path.join(WORK, "qa-d.db")
os.environ["MMAI_DB"] = DB
os.environ["MMAI_INITIAL_PASSWORD_BILL"] = PW
SHOTS = os.path.join(WORK, "shots")
os.makedirs(SHOTS, exist_ok=True)


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
    from server import ai, matching
    from server.db import row, rows
    from tests.test_t6_grounding import _party_group
    from tests.test_launch_neutral import _apt
    from tests.conftest import fake_staff_briefs
    mp = MonkeyPatch()
    mp.setattr(ai, "staff_briefs", fake_staff_briefs)
    dbmod._conn = None
    client = TestClient(appmod.app)
    db = appmod.db
    out = {}
    with client:
        groups = {}
        for tag in ("G1", "G2"):
            held = rows(db.execute("SELECT id, status FROM enquiries WHERE is_test=1"))
            db.execute("UPDATE enquiries SET status='QA hold' WHERE is_test=1"); db.commit()
            a, lead, solo, g = _party_group(db, mp)
            for h in held:
                db.execute("UPDATE enquiries SET status=? WHERE id=?", (h["status"], h["id"]))
            db.commit()
            groups[tag] = {"gid": g["id"], "apartment": a["id"], "lead": lead["id"]}
        r = client.post("/api/auth/login", json={"username": "bill", "password": PW}, headers={"X-Visitor-Id": "qa-seed"})
        assert r.status_code == 200, r.text
        client.headers.update({"Authorization": "Bearer " + r.json()["token"]})
        A, g1, g2 = groups["G1"]["apartment"], groups["G1"]["gid"], groups["G2"]["gid"]
        # the fixture re-uses one address, so both groups sit on A; give G2 its own apartment B (same type) as its #1 and A as #2
        B = _apt(db, type_code="3B", beds=("1 king or 2 singles", "1 king or 2 singles", "1 king or 2 singles"), rent12=1500.0, rent6=1600.0, address="Unit 2 / 2 Neutral Street")["id"]
        alloc = row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (g2,)))["allocation_json"]
        db.execute("UPDATE groups SET apartment_id=? WHERE id=?", (B, g2))
        db.execute("DELETE FROM group_apartments WHERE group_id=?", (g2,))
        db.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, allocation_json) VALUES (?,?,1,?)", (g2, B, alloc))
        db.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, allocation_json) VALUES (?,?,2,?)", (g2, A, alloc))
        db.commit()
        assert A != B
        r = client.post(f"/api/apartments/{A}/lease", json={"group_id": g1}); assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["leased_group_id"] == g1 and rep["rerank"]["status"] == "complete", rep
        # unfinished follow-up, from the data: G2 lists A again at rank 2 (as if its step never ran); the apartment is already Leased
        db.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g2, A))
        db.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, allocation_json) VALUES (?,?,2,?)", (g2, A, row(db.execute("SELECT allocation_json FROM groups WHERE id=?", (g2,)))["allocation_json"]))
        db.execute("UPDATE groups SET rerank_note=NULL, reranked_at=NULL, flag_kind=NULL WHERE id=?", (g2,))
        db.commit()
        pend = matching.pending_lease_reranks(db)
        assert [p["group_id"] for p in pend if p["apartment_id"] == A] == [g2], pend
        d = client.get(f"/api/apartments/{A}").json()
        assert d["rerank_pending_group_ids"] == [g2], d["rerank_pending_group_ids"]
        out.update({"A": A, "B": B, "g1": g1, "g2": g2, "leased_at": rep["leased_at"], "address": d["address"]})
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


async def login(pg):
    await pg.goto(BASE + "#/staff"); await pg.wait_for_selector("input[name=username]")
    await pg.fill("input[name=username]", "bill"); await pg.fill("input[name=password]", PW); await pg.keyboard.press("Enter")
    await pg.wait_for_selector("#view-staff .side, #view-staff nav", timeout=10000)
    await pg.wait_for_selector('[data-role="dataset"] input[value=test]', timeout=10000)
    await pg.check('[data-role="dataset"] input[value=test]'); await pg.wait_for_timeout(500)


async def wait_leasemsg(pg, timeout=10.0):
    # the page's Content Security Policy forbids evaluated scripts, so poll the element's text from here (no page-side eval)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (await pg.locator('[data-role="leasemsg"]').text_content() or "").strip():
            return
        await pg.wait_for_timeout(100)
    raise AssertionError("no lease message appeared within %.0fs" % timeout)


async def open_apartment(pg, aid):
    await pg.goto(BASE + f"#/staff/apartments/{aid}")
    await pg.wait_for_selector('[data-role="leasemsg"]', state="attached", timeout=10000)
    await pg.wait_for_timeout(400)


async def checks(seeded):
    from playwright.async_api import async_playwright
    A, g1, g2 = seeded["A"], seeded["g1"], seeded["g2"]
    tok = api_login()
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for name, vp, tag in (("desktop", {"width": 1280, "height": 900}, "1280"), ("phone", {"width": 390, "height": 844}, "390")):
            ctx = await b.new_context(viewport=vp, device_scale_factor=1)
            pg = await ctx.new_page()
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            await login(pg)
            # restore the pending state before the second viewport's run (the first run finished it)
            if name == "phone":
                import sqlite3
                c = sqlite3.connect(DB)
                c.execute("DELETE FROM group_apartments WHERE group_id=? AND apartment_id=?", (g2, A))
                c.execute("INSERT INTO group_apartments(group_id, apartment_id, rank, allocation_json) VALUES (?,?,2,(SELECT allocation_json FROM groups WHERE id=?))", (g2, A, g2))
                c.execute("UPDATE groups SET rerank_note=NULL, reranked_at=NULL, flag_kind=NULL, apartment_id=? WHERE id=?", (seeded["B"], g2))
                c.commit(); c.close()
                assert api("GET", f"api/apartments/{A}", tok)["rerank_pending_group_ids"] == [g2]
            await open_apartment(pg, A)
            text = await pg.inner_text('#view-staff')
            assert "Finish updating other groups" in text, text[:800]
            assert "still need to be moved to their next best apartment" in text and "1 other group(s)" in text, text[:800]
            assert "Mark Leased" not in text, "a leased apartment offers no second lease"
            btn = pg.locator('[data-action="finish-lease"]')
            assert await btn.is_visible()
            box = await btn.bounding_box()
            assert box and box["x"] >= 0 and box["x"] + box["width"] <= vp["width"] + 1, f"button clipped at {vp['width']}: {box}"
            warn = pg.locator("p.warn-text")
            wbox = await warn.bounding_box()
            assert wbox and wbox["x"] + wbox["width"] <= vp["width"] + 1, f"warning clipped: {wbox}"
            await pg.screenshot(path=f"{SHOTS}/pending-{tag}.png", full_page=True)
            ok(f"{name}: pending warning + button rendered within viewport")
            before = api("GET", f"api/apartments/{A}", tok)
            await btn.click()
            await wait_leasemsg(pg)
            m = await pg.inner_text('[data-role="leasemsg"]')
            assert "not repeated" in m and "1 group(s) moved or trimmed" in m, m
            cls = await pg.get_attribute('[data-role="leasemsg"]', "class")
            assert "ok" in (cls or ""), cls
            await pg.screenshot(path=f"{SHOTS}/result-{tag}.png", full_page=True)
            ok(f"{name}: result message: {m}")
            after = api("GET", f"api/apartments/{A}", tok)
            assert after["leased_at"] == before["leased_at"] == seeded["leased_at"] and after["leased_group_id"] == g1, "the claim was not repeated or moved"
            assert after["rerank_pending_group_ids"] == [], after["rerank_pending_group_ids"]
            gd = api("GET", f"api/groups/{g2}", tok)
            assert gd["apartment_id"] == seeded["B"] and gd.get("flag_kind") == "lease" and gd["status"] not in ("Leased", "Dissolved"), {k: gd.get(k) for k in ("apartment_id", "flag_kind", "status", "rerank_note")}
            ok(f"{name}: group {g2} stays on its own apartment {seeded['B']}, its option for {A} trimmed and the group flagged; apartment {A} still leased once to group {g1}")
            await pg.wait_for_timeout(1500)   # the page re-renders itself after the action
            await pg.reload(); await pg.wait_for_selector('[data-role="leasemsg"]', state="attached", timeout=10000); await pg.wait_for_timeout(400)
            text2 = await pg.inner_text('#view-staff')
            assert "still need to be moved" not in text2 and "Finish updating other groups" in text2, text2[:600]
            await pg.screenshot(path=f"{SHOTS}/after-{tag}.png", full_page=True)
            # idempotent second click: nothing to move, nothing claimed
            await pg.locator('[data-action="finish-lease"]').click()
            await wait_leasemsg(pg)
            m2 = await pg.inner_text('[data-role="leasemsg"]')
            assert "not repeated" in m2 and "0 group(s) moved or trimmed" in m2, m2
            again = api("GET", f"api/apartments/{A}", tok)
            assert again["leased_at"] == seeded["leased_at"] and again["leased_group_id"] == g1
            assert not errors, errors
            ok(f"{name}: after reload the warning is gone, the action stays and is idempotent; no page errors")
            await ctx.close()
        await b.close()
    import sqlite3
    c = sqlite3.connect(DB)
    n_claims = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='group_status' AND detail LIKE '%claimed apartment%'").fetchone()[0]
    n_leased = c.execute("SELECT COUNT(*) FROM audit_log WHERE action='apartment_leased' AND entity_id=?", (str(A),)).fetchone()[0]
    c.close()
    assert n_claims == 1, n_claims
    assert n_leased >= 3, n_leased   # seed lease + two viewports' finish (+ idempotent clicks): follow-up audits, never a second claim
    ok(f"audit: 1 claim, {n_leased} follow-up records")


if __name__ == "__main__":
    seeded = seed()
    print("seeded", seeded)
    srv = start_server()
    try:
        asyncio.run(checks(seeded))
        print("ALL PASSED; screenshots in", SHOTS)
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(10)
        except Exception:
            srv.kill()
