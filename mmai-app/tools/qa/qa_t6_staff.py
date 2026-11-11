"""Browser QA (8 November 2026): T6 staff readiness / introduction guards and the history labels on the staff Groups views.

Runs ONLY against a disposable local server on a throw-away SQLite file (never the shared preview on 8079, never a live
database). No model is called: the AI is replaced by the deterministic test doubles from tests/ while seeding.

    python3 tools/qa/qa_t6_staff.py            # seeds /tmp/qa-t6-8078.db, starts 127.0.0.1:8078, runs the checks, stops the server

Seeded groups (dataset "test"):
  G1  ready: AI review approved, briefs complete, AI introduction drafted (current)
  G2  ready, but the introduction went stale: a member's introduction details changed after the draft
  G3  members replaced after the review (Devi out, Zelda in): the brief / review are history (not current); the rows kept the
      writer's immutable ref map, so the earlier people are named from it and the newcomer never is; then Devi's record is
      deleted through the staff route and the T5 cleanup removes the earlier round that was about her
  G4  like G3 but legacy rows without a map -> names withheld with the "earlier set of members" label
  G5  reviewed and ready, but no introduction drafted yet

Checks (each prints OK or raises):
  L1 groups list (1280): the "Ready" filter shows exactly the groups the server's readiness predicate accepts; the overview
     counter "Ready for reservations" equals the number of ready primary groups; badges use plain words, no rule codes
  D1 G1 detail: "Ready to circulate", Copy text enabled, "Open in email" shown with every real person's own address (4
     recipients, party members included), textarea editable, brief names the people (no U1/U2/I1 refs)
  D2 G2 detail: readiness line reads "Group reviewed; create a current introduction before sharing." (never the green "Ready to
     circulate" without a current draft), stale banner "cannot be copied or sent until a fresh draft passes the checks" with
     the reason as a proper sentence, Copy disabled, Open-in-email hidden, textarea read-only, the old text still visible as
     history, "Draft again with AI" enabled (group itself is reviewed and ready for a new draft)
  D3 G3 detail: brief/review marked as earlier (not current); earlier people named from the map; the newcomer never named in
     the old brief; no raw ids. D3b: deleting Devi via DELETE /api/enquiries removes that earlier round from the group page
     (no brief, review or intro text about her remains) while the group and its current members stay
  D4 G4 detail: "Written about an earlier set of members or facts, so the people in it are not named here."; nobody named,
     refs shown as neutral words ("one member", "another member")
  D5 G5 detail: "Group reviewed; create a current introduction before sharing." with "no introduction has been drafted yet";
     never the green "Ready to circulate" without a current draft; drafting offered
  D1 also checks the T10 presentation: the seeded review rationale / member brief carry U1/U2, "under A16" and an r5
     compared-with bracket; the page shows names, "under the total-rent allowance" and "Compared with <name>'s daily routine"
     with no bare U1/U2 or codes in the review-and-brief card
  M1 phone (375): G1 and G2 detail have no horizontal overflow
  W1 wording: no rule codes (T6, GP2, R.A…) anywhere in the rendered group pages

Qualification: the seeded introduction and brief texts are FIXTURE output (tests' deterministic doubles) and contain refs such
as I1 / I1-m1 by construction. The "no raw ids" checks here cover the staff UI's own rendering (labels, member names, evidence
lines); they are NOT evidence about real model output - that is checked separately against actual AI runs.
"""
import asyncio, json, os, re, signal, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
PORT = int(os.environ.get("MMAI_QA_PORT", "8078"))
assert PORT != 8079, "8079 is the shared preview; QA uses a disposable server only"
DB = os.environ.get("MMAI_QA_DB", f"/tmp/qa-t6-{PORT}.db")
BASE = f"http://127.0.0.1:{PORT}/"
PW = "Qa-T6-8078!"
SHOTS = os.environ.get("MMAI_QA_SHOTS", "/tmp/mmai-qa-t6")
os.makedirs(SHOTS, exist_ok=True)
ok = lambda tag, *rest: print("OK", tag, *rest)
RULE_CODES = re.compile(r"\b(T\d{1,2}|GP\d{1,2}[a-z]?|I\d{1,2}\([a-z]\)|R\.A\d+|H\d{2})\b")
RAW_ID = re.compile(r"\b[UIR]\d+(-m\d+)?\.|\bR\.A\d+\.")
RAW_REF = re.compile(r"(?<![A-Za-z0-9_-])(?:[UR]\d+(?:-m\d+)?|A\d+\.[a-z_]+|R\.A\d+)(?![A-Za-z0-9_-])")   # strict: bare U1 / U1-m1 too (review + brief card only; the intro textarea is fixture text)


def seed():
    """Four groups via the real API + the deterministic AI doubles; returns the group ids and names."""
    for f in (DB, DB + "-wal", DB + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    os.environ["MMAI_DB"] = DB
    os.environ["MMAI_INITIAL_PASSWORD_BILL"] = PW
    os.environ["MMAI_MAIL_MODE"] = "off"
    from pytest import MonkeyPatch
    from fastapi.testclient import TestClient
    import server.db as dbmod
    import server.app as appmod
    from server import ai, ai_queue
    from server.db import row, rows
    from tests.test_t6_grounding import _party_group, _enq
    from tests.test_t6_staff_api import _fake_draft
    from tests.conftest import fake_staff_briefs
    mp = MonkeyPatch()
    mp.setattr(ai, "staff_briefs", fake_staff_briefs)      # the same deterministic brief double the test suite uses (no model)
    dbmod._conn = None
    client = TestClient(appmod.app)
    db = appmod.db
    out = {}
    with client:
        mp.setattr(ai, "draft_introduction", _fake_draft)
        for tag in ("G1", "G2", "G3", "G4", "G5"):
            # keep each seeded foursome to its own apartment: people already grouped sit out of the next run (status restored after)
            held = rows(db.execute("SELECT id, status FROM enquiries WHERE is_test=1"))
            db.execute("UPDATE enquiries SET status='QA hold' WHERE is_test=1"); db.commit()
            a, lead, solo, g = _party_group(db, mp)
            for h in held:
                db.execute("UPDATE enquiries SET status=? WHERE id=?", (h["status"], h["id"]))
            db.commit()
            out[tag] = {"gid": g["id"], "lead": lead["id"], "solo": solo["id"], "apartment": a["id"]}
        # staff session for the API calls
        r = client.post("/api/auth/login", json={"username": "bill", "password": PW}, headers={"X-Visitor-Id": "qa-seed"})
        assert r.status_code == 200, r.text
        client.headers.update({"Authorization": "Bearer " + r.json()["token"]})
        # G1: current AI draft; its review rationale and one member brief carry the AI's internal refs / a register code, as real
        # model output does (U1/U2, "under A16", an r5 compared_with bracket) - the staff page must show names and plain words
        r = client.post(f"/api/groups/{out['G1']['gid']}/introduction"); assert r.status_code == 200, r.text
        g1 = out["G1"]["gid"]
        RATIONALE = "U1 and U2 keep similar hours; the rent is negotiable under A16 if U2.guests stays occasional."
        db.execute("UPDATE review_rounds SET rationale=? WHERE group_id=? AND valid=1", (RATIONALE, g1))
        db.execute("UPDATE groups SET ai_rationale=? WHERE id=?", (RATIONALE, g1))
        mb = row(db.execute("SELECT id, body_json FROM group_briefs WHERE group_id=? AND kind='member' AND person_ref='U1' ORDER BY id DESC LIMIT 1", (g1,)))
        body = json.loads(mb["body_json"]); body.setdefault("rendered", {})["strengths"] = ["U1 rises as early as U2  [U1.routine=Early riser]  [compared with U2: U2.routine=Early riser]"]
        db.execute("UPDATE group_briefs SET body_json=? WHERE id=?", (json.dumps(body), mb["id"]))
        db.commit()
        # G5: reviewed and ready, but no introduction drafted yet
        # G2: draft, then a member's introduction details change -> stale marker
        r = client.post(f"/api/groups/{out['G2']['gid']}/introduction"); assert r.status_code == 200, r.text
        r = client.patch(f"/api/enquiries/{out['G2']['solo']}", json={"intro_age": 29, "intro_sex": "Female"}); assert r.status_code == 200, r.text
        # G3: Devi replaced by Zelda (same group row); map kept; then Devi's record deleted
        g3 = out["G3"]
        z = _enq(db, "Zelda", f"zelda{g3['gid']}@example.com", budget_option="3B-single", apt_sizes=["3 bedrooms"], mate_max="5", contact_method="Email")
        db.execute("DELETE FROM group_members WHERE group_id=? AND enquiry_id=?", (g3["gid"], g3["solo"]))
        db.execute("INSERT INTO group_members(group_id, enquiry_id) VALUES (?,?)", (g3["gid"], z["id"]))
        db.execute("UPDATE groups SET evidence_hash='members-changed', member_key=? WHERE id=?", (",".join(sorted([str(g3["lead"]), str(z["id"])], key=int)), g3["gid"]))
        db.commit()
        # G4: legacy rows (no map) + hash change
        g4 = out["G4"]
        db.execute("UPDATE group_briefs SET refs_json=NULL WHERE group_id=?", (g4["gid"],))
        db.execute("UPDATE review_rounds SET refs_json=NULL WHERE group_id=?", (g4["gid"],))
        db.execute("UPDATE groups SET evidence_hash='members-changed' WHERE id=?", (g4["gid"],))
        db.commit()
        d1 = client.get(f"/api/groups/{out['G1']['gid']}").json()
        assert d1["ready"] is True and d1["intro"]["current"] is True
        assert "under the total-rent allowance" in d1["review"]["rationale"] and not RAW_REF.search(d1["review"]["rationale"]) and d1["ai_rationale"] == d1["review"]["rationale"], d1["review"]
        out["g1_rationale"] = d1["review"]["rationale"]
        d5 = client.get(f"/api/groups/{out['G5']['gid']}").json()
        assert d5["ready"] is True and not d5["intro"]["text"] and d5["intro"]["current"] is False, d5["intro"]
        d2 = client.get(f"/api/groups/{out['G2']['gid']}").json()
        assert d2["ready"] is True and d2["intro"]["current"] is False and d2["intro"]["text"]
        ready_ids = sorted(x["id"] for x in client.get("/api/groups", params={"dataset": "test", "ai_state": "ready"}).json()["items"])
        stats = client.get("/api/stats", params={"dataset": "test"}).json()
        out["ready_ids"] = ready_ids
        out["groups_ready"] = stats["groups_ready"]
        out["g3_detail"] = client.get(f"/api/groups/{g3['gid']}").json()
    db.commit()
    mp.undo()
    appmod.db.close() if hasattr(appmod.db, "close") else None
    dbmod._conn = None
    return out


def start_server():
    env = dict(os.environ, MMAI_DB=DB, MMAI_INITIAL_PASSWORD_BILL=PW, MMAI_MAIL_MODE="off", PORT=str(PORT))
    p = subprocess.Popen([sys.executable, "-m", "server.app"], env=env, stdout=open(f"{SHOTS}/server.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            urllib.request.urlopen(BASE + "api/health", timeout=1); return p
        except Exception:
            time.sleep(0.25)
    p.kill(); raise RuntimeError("server did not start; see " + f"{SHOTS}/server.log")


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
    await pg.check('[data-role="dataset"] input[value=test]'); await pg.wait_for_timeout(500)      # seeded records are test data


async def open_group(pg, gid):
    if pg.url.endswith(f"#/staff/groups/{gid}"):      # same hash would not re-render: leave and come back so the page is fresh
        await pg.goto(BASE + "#/staff/overview"); await pg.wait_for_timeout(300)
    await pg.goto(BASE + f"#/staff/groups/{gid}")
    await pg.wait_for_selector('[data-role="intro"], .group-detail, textarea.intro-box', timeout=10000)
    await pg.wait_for_timeout(400)


async def page_text(pg):
    return await pg.evaluate("document.querySelector('#view-staff').innerText")


async def checks(seeded):
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await (await b.new_context(viewport={"width": 1280, "height": 900})).new_page()
        errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
        await login(pg)
        # L1 overview counter + groups list ready filter
        await pg.goto(BASE + "#/staff/overview"); await pg.wait_for_timeout(600)
        ov = await page_text(pg)
        m = re.search(r"(\d+)\s*\n?\s*Ready for reservations", ov)
        assert m and int(m.group(1)) == seeded["groups_ready"] == len(seeded["ready_ids"]), (m and m.group(0), seeded["groups_ready"], seeded["ready_ids"])
        await pg.screenshot(path=f"{SHOTS}/overview-1280.png", full_page=True)
        await pg.goto(BASE + "#/staff/groups"); await pg.wait_for_selector('[data-role="ai"]', timeout=10000); await pg.wait_for_timeout(500)
        await pg.select_option('[data-role="ai"]', "ready"); await pg.wait_for_selector('[data-role="ai"]', timeout=10000); await pg.wait_for_timeout(800)
        shown = await pg.evaluate("[...document.querySelectorAll('[data-role=\"groups\"] [data-action=\"open-group\"]')].map(b => Number(b.dataset.id))")
        assert sorted(set(shown)) == seeded["ready_ids"], (sorted(set(shown)), seeded["ready_ids"])
        lst = await page_text(pg)
        assert re.search(r"\b%d ready for reservations" % len(seeded["ready_ids"]), lst), lst[:1200]
        ok("L1", "ready filter lists exactly", seeded["ready_ids"], "and the counter agrees")
        await pg.screenshot(path=f"{SHOTS}/groups-list-1280.png", full_page=True)
        assert not RULE_CODES.search(await page_text(pg)), RULE_CODES.search(await page_text(pg)).group(0)

        # D1 ready group with a current AI draft
        g1 = seeded["G1"]["gid"]; await open_group(pg, g1)
        t = await page_text(pg)
        assert "Ready to circulate" in t and "create a current introduction" not in t, t[:600]
        assert not await pg.is_disabled('[data-action="copy"]') and await pg.is_visible('[data-role="mailto"]')
        href = await pg.get_attribute('[data-role="mailto"]', "href")
        for who in ("astrid@example.com", "bo@example.com", "cy@example.com", "devi@example.com"):
            assert who in href, href[:200]
        assert await pg.evaluate("!document.querySelector('[data-role=\"intro\"]').readOnly")
        assert not RAW_ID.search(t) and not RULE_CODES.search(t), (RAW_ID.search(t) or RULE_CODES.search(t)).group(0)
        assert "Astrid" in t and "Devi" in t
        await pg.evaluate("document.querySelectorAll('[data-role=\"review-brief\"] details').forEach(d => d.open = true)")
        card = await pg.evaluate("document.querySelector('[data-role=\"review-brief\"]').innerText")
        assert seeded["g1_rationale"] in card and "Compared with " in card and "'s daily routine: Early riser" in card, card[:900]
        assert not RAW_REF.search(card) and not RULE_CODES.search(card) and "A16" not in card and "unusable" not in card, (RAW_REF.search(card) or RULE_CODES.search(card))
        assert seeded["g1_rationale"] in await pg.evaluate("document.querySelector('.rationale').innerText"), "list/detail card shows the plain assessment line"
        await pg.screenshot(path=f"{SHOTS}/g1-ready-1280.png", full_page=True)
        ok("D1", "G1 ready: copy enabled, email to 4 own addresses, editable, people named; review/brief in plain words (names, 'under the total-rent allowance', compared-with evidence)")

        # D5 ready group with no draft yet: never the green "Ready to circulate"
        g5 = seeded["G5"]["gid"]; await open_group(pg, g5)
        t = await page_text(pg)
        assert "Group reviewed; create a current introduction before sharing." in t and "no introduction has been drafted yet" in t and "Ready to circulate" not in t, t[:900]
        assert not await pg.is_disabled('[data-action="draft"]') and await pg.is_hidden('[data-role="mailto"]')
        await pg.screenshot(path=f"{SHOTS}/g5-no-draft-1280.png", full_page=True)
        ok("D5", "G5 reviewed, no draft: 'create a current introduction' line with 'no introduction has been drafted yet'; drafting offered; no green ready line")

        # D2 stale introduction
        g2 = seeded["G2"]["gid"]; await open_group(pg, g2)
        t = await page_text(pg)
        assert "cannot be copied or sent until a fresh draft passes the checks" in t, t[:900]
        assert "Group reviewed; create a current introduction before sharing." in t and "Ready to circulate" not in t, t[:900]
        banner = await pg.evaluate("[...document.querySelectorAll('p.msg.err.small')].map(e => e.innerText).find(x => x.includes('Earlier text is kept')) || ''")
        assert re.search(r"\. [A-Z][^.]*\. Earlier text is kept", banner), banner      # label. Reason as a sentence. Earlier text…
        assert await pg.is_disabled('[data-action="copy"]') and await pg.is_hidden('[data-role="mailto"]') and await pg.is_disabled('[data-action="save-intro"]')
        assert await pg.evaluate("document.querySelector('[data-role=\"intro\"]').readOnly")
        assert (await pg.evaluate("document.querySelector('[data-role=\"intro\"]').value")).strip(), "old text kept for reference"
        assert not await pg.is_disabled('[data-action="draft"]'), "group is ready; drafting again must be offered"
        assert "Draft again with AI" in t and "Earlier introduction drafts and changes" in t
        assert not RULE_CODES.search(t)
        await pg.screenshot(path=f"{SHOTS}/g2-stale-1280.png", full_page=True)
        ok("D2", "G2 stale: 'create a current introduction' line, banner sentence, copy/email/save disabled, read-only, history kept, redraft offered")

        # D3 replaced-out member: earlier people named from the immutable map, newcomer not named in the old brief
        g3 = seeded["G3"]["gid"]; await open_group(pg, g3)
        t = await page_text(pg)
        d3 = seeded["g3_detail"]
        assert d3["brief"]["current"] is False and d3["brief"]["names_available"] is True and d3["review"]["current"] is False
        names = [m["name"] for m in d3["brief"]["members"]]
        assert "Astrid" in names and "Devi" in names and "Zelda" not in json.dumps(d3["brief"]), names
        await pg.evaluate("document.querySelectorAll('[data-role=\"review-brief\"] details').forEach(d => d.open = true)")
        brief = await pg.evaluate("document.querySelector('[data-role=\"review-brief\"]').innerText")
        assert "Not ready to circulate" in t and "Zelda" not in brief and "Devi" in brief and not RAW_ID.search(t) and not RULE_CODES.search(t), brief[:400]
        await pg.screenshot(path=f"{SHOTS}/g3-earlier-map-1280.png", full_page=True)
        ok("D3", "G3 history named from its own map; newcomer absent from the old brief")
        # D3b: authorised deletion of the earlier member removes her round from the page (T5), the group itself stays
        tok = api_login()
        api("DELETE", f"api/enquiries/{seeded['G3']['solo']}", tok)
        d3b = api("GET", f"api/groups/{g3}", tok)
        assert d3b["brief"] is None and d3b["review"] is None and not (d3b.get("intro") or {}).get("text") and d3b["status"] != "Dissolved", {k: d3b.get(k) for k in ("brief", "review", "intro", "status")}
        assert "Devi" not in json.dumps(d3b) and "Zelda" in json.dumps([m.get("first_name") for m in d3b["members"]])
        await open_group(pg, g3); t = await page_text(pg)
        assert "Devi" not in t and "Zelda" in t and "Not ready to circulate" in t, [t[max(0, i - 200):i + 100] for i in [t.find("Devi")]]
        await pg.screenshot(path=f"{SHOTS}/g3b-after-deletion-1280.png", full_page=True)
        ok("D3b", "after Devi's deletion no brief/review/intro about her remains; group kept with Astrid and Zelda")

        # D4 legacy rows: names withheld
        g4 = seeded["G4"]["gid"]; await open_group(pg, g4)
        t = await page_text(pg)
        assert "Written about an earlier set of members or facts, so the people in it are not named here." in t, t[:900]
        # the whole review-and-brief card, with the collapsed per-person notes and evidence opened
        await pg.evaluate("document.querySelectorAll('[data-role=\"review-brief\"] details').forEach(d => d.open = true)")
        brief = await pg.evaluate("document.querySelector('[data-role=\"review-brief\"]').innerText")
        for who in ("Astrid", "Devi", "Bo Berg", "Cy Lund"):
            assert who not in brief, brief[:400]
        assert ("one member" in brief or "another member" in brief) and not RAW_REF.search(brief), brief[:400]
        await pg.screenshot(path=f"{SHOTS}/g4-legacy-1280.png", full_page=True)
        ok("D4", "G4 legacy history: nobody named, label shown")
        assert errs == [], errs

        # M1 phone
        pm = await (await b.new_context(viewport={"width": 375, "height": 800})).new_page()
        await login(pm)
        for tag, gid in (("g1", g1), ("g2", g2)):
            await open_group(pm, gid)
            w = await pm.evaluate("[document.documentElement.scrollWidth, document.documentElement.clientWidth]")
            assert w[0] <= w[1] + 1, (tag, w)
            await pm.screenshot(path=f"{SHOTS}/{tag}-375.png", full_page=True)
        ok("M1", "375 px: no horizontal overflow on G1/G2 detail")
        ok("W1", "no rule codes in rendered group pages")
        await b.close()


if __name__ == "__main__":
    seeded = seed()
    print("seeded", {k: v for k, v in seeded.items() if k != "g3_detail"})
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
