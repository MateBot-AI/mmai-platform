"""Browser QA (8 November 2026, I8 / I11(d)): sex, date of birth, age and guardian questions removed from every form surface;
ONE explicit declaration that everyone in the group will be 18 or older at move-in (payload `adults_confirm`, a real boolean).

Runs against an ISOLATED backend with a disposable TEST database (never the shared preview). Creates TEST records only.

    export MMAI_QA_BASE=http://127.0.0.1:8077/      # isolated QA server (default below)
    export MMAI_QA_PASSWORD=...                     # that server's password for staff user "bill"
    export MMAI_QA_DB=/path/to/that/server.db       # optional: lets the script age one record into a pre-8-Nov "never declared" enquiry
    python3 tools/qa/qa_adults_i11d.py

Checks (each prints OK or raises):
  P1 public card form: no control for any retired question; 27 screens incl. review (8 Nov 2026: origin screens removed; features and introduction-only details screens added), none empty; every screen has a MateBot tip
  P2 the party rows ask name, email, country of birth and the optional introduction-only sex / current age (no DOB); the summary shows no Sex / Date of birth / sex preference / age range
  P3 collect() sends adults_confirm === true (boolean), never consent_age or any retired key; party members carry 3 keys only
  P4 renewal: after the box is ticked, changing move-in from the summary clears it (note shown); Submit returns to the consent
     screen; ticking again then Submit sends ONE POST with adults_confirm true; the saved record says confirmed by the enquirer
  P5 fill() with legacy keys (dob, sex, consent_age: true) never pre-ticks adults_confirm and throws no error
  P6 phone (375) layout: same screen count, no horizontal overflow on the consent and party screens
  P7 save-and-finish-later: a resumed draft restores the enquirer's own declaration and party rows (3 keys); changing the first
     name on the resumed form clears the declaration again; no page errors
  S1 staff overview: 18+ to-do card, no guardian card; list filter offers "18+ to confirm", no guardian option
  S2 staff detail: declaration card "Confirmed by the enquirer…"; Edit answers has no dob/sex/sex_pref controls; All answers has
     no Sex / Age range / Parent or guardian / "18+" consent tick, and shows "Everyone 18+ at move-in"
  S3 Save answers with an EARLIER move-in sends no adults_confirmed key; the record then reads renew ("fresh confirmation"), the
     list badge "18+ to confirm" appears; "Record: confirmed with the enquirer" sends exactly {adults_confirmed: true}
  S4 Withdraw needs two clicks and sends exactly {adults_confirmed: false}; status returns to "to confirm"
  S5 staff phone entry (#/staff/new): section 6 holds adults_confirm; no retired controls; summary + email preview carry the 18+
     line and no Date of birth / Sex lines; one POST
  S6 (needs MMAI_QA_DB) a never-declared enquiry shows "Not yet declared" and the list badge; nothing is auto-affirmed
"""
import asyncio, json, os, sqlite3, sys
from playwright.async_api import async_playwright

BASE = os.environ.get('MMAI_QA_BASE', 'http://127.0.0.1:8077/').rstrip('/') + '/'
PW = os.environ.get('MMAI_QA_PASSWORD', '')
SHOTS = os.environ.get('MMAI_QA_SHOTS', '/tmp/mmai-qa-adults')
os.makedirs(SHOTS, exist_ok=True)
RETIRED = ['dob', 'sex', 'sex_other', 'sex_pref', 'age_pref', 'guardian_name', 'guardian_relationship', 'guardian_email', 'guardian_phone', 'consent_age']
GOOD = dict(first_name="Qa", last_name="Adults", email="qa.adults@example.com", phone_code="+61", phone="0400000011", contact_method="Email",
            birth_country_raw="Sweden", live_country_raw="Sweden", languages=["English"],
            city="Melbourne", move_in="2027-03-01", move_in_flex="± 1 week", move_out="2027-09-10", move_out_flex="Exact date",
            budget_option="2B2B-single", apt_sizes=["2 bedrooms"], mate_ideal="1", mate_max="3", rooms=["Own room"], bunk_ok="No", room_first="Own room",
            party="With friends", party_size="2", couples_answer="No",
            party_members=[{"name": "Pal Adults", "email": "pal.adults@example.com", "birth_country_raw": "Norway"}],
            doing="Working", work="Melbourne CBD",
            smoke="No", smoke_ok="Yes", source="Google", consent_store=True, consent_share=True, adults_confirm=True)
F = "document.querySelector('.form-root').matebotForm"
ok = lambda tag, *rest: print('OK', tag, *rest)


async def no_retired_controls(pg, scope='body'):
    names = await pg.evaluate(f"[...document.querySelectorAll('{scope} input, {scope} select, {scope} textarea')].map(e=>e.name||e.dataset.pm||'')")
    bad = [n for n in names if n in RETIRED or n.startswith('guardian')]
    assert not bad, bad
    txt = (await pg.inner_text(scope)).lower()
    for w in ('date of birth', 'sex preference', 'age range', 'guardian'):
        assert w not in txt, (w, scope)


async def public_checks(width):
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': width, 'height': 900})).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        posts = []
        pg.on('request', lambda r: posts.append((r.url, r.post_data)) if r.method == 'POST' and '/api/public/enquiries' in r.url else None)
        await pg.goto(BASE + '#/?test=1'); await pg.click('.btn--start'); await pg.wait_for_selector('.scr.is-active')
        # P1
        scrs = await pg.evaluate("[...document.querySelectorAll('.step:not(.staff-step) .scr')].map(s=>({id:s.dataset.scr, tip:!!s.dataset.tip, inputs:s.querySelectorAll('input,select,textarea').length}))")
        assert len(scrs) == 27, (len(scrs), [s['id'] for s in scrs])
        for s in scrs: assert s['tip'] and (s['inputs'] > 0 or s['id'] == 'review'), s
        assert not any(s['id'] in ('dob', 'sex', 'sexpref') for s in scrs)
        await no_retired_controls(pg, '.form-root')
        box = await pg.evaluate("(e=>e?({type:e.type, required:e.required, checked:e.checked}):null)(document.querySelector('.form-root [name=adults_confirm]'))")
        assert box == {'type': 'checkbox', 'required': True, 'checked': False}, box
        ok('P1', width, '27 screens, none empty, no retired controls')
        # P2 + P3
        g = dict(GOOD, email=f'qa.adults{width}@example.com', phone=f'04000{width:05d}')   # one TEST record per viewport run
        await pg.evaluate(f"{F}.fill({json.dumps(g)})")
        cells = await pg.evaluate("[...document.querySelectorAll('.party-row [data-pm]')].map(e=>e.dataset.pm)")
        assert cells == ['name', 'email', 'birth_country_raw', 'hint', 'intro_sex', 'intro_sex_other', 'intro_age'], cells   # intro_* = optional introduction-only details (Bill, 8 Nov 2026), not retired fields
        sub = (await pg.evaluate("[...document.querySelectorAll('[data-q=\"3.9\"] .sublabel')].map(p=>p.textContent).join(' ')")).lower()
        assert 'name, email and country of birth' in sub and 'sex' not in sub, sub
        await pg.evaluate(f"{F}.go(999)"); assert await pg.is_visible('.scr--review.is-active')
        review = await pg.inner_text('[data-role=review]')
        for w in ('Date of birth', 'Sex', 'age range', 'guardian', 'I confirm I will be 18'):
            assert w not in review, w
        assert '18 or older on the move-in date' in review and 'Pal Adults' in review and 'Norway' in review, review[-600:]
        await pg.screenshot(path=f'{SHOTS}/public_review_{width}.png', full_page=True)
        c = await pg.evaluate(f"{F}.collect()")
        assert c['adults_confirm'] is True and not any(k in c for k in RETIRED), c
        assert [sorted(m.keys()) for m in c['party_members']] == [['birth_country_raw', 'email', 'name']], c['party_members']
        ok('P2/P3', width, 'party rows + summary clean; adults_confirm true boolean')
        # P4 renewal from the summary
        idx = await pg.evaluate("[...document.querySelectorAll('.review__row dt')].findIndex(d=>d.textContent.includes('move-in date'))")
        await pg.click(f'.review__change >> nth={idx}')
        assert await pg.evaluate("!!document.querySelector('.scr.is-active [name=move_in]')")
        await pg.fill('.scr.is-active [name=move_in]', '2027-02-15'); await pg.dispatch_event('.scr.is-active [name=move_in]', 'change')
        assert not await pg.evaluate("document.querySelector('[name=adults_confirm]').checked"), 'tick carried over after a move-in change'
        await pg.click('[data-role=toReview]'); assert await pg.is_visible('.scr--review.is-active')
        assert 'Not ticked' in (await pg.inner_text('[data-role=review]'))
        await pg.click('[data-role=next]'); await pg.wait_for_timeout(200)
        assert await pg.evaluate("!!document.querySelector('.scr.is-active [name=adults_confirm]')"), 'Submit did not return to the consent screen'
        assert await pg.evaluate("document.querySelector('[name=adults_confirm]').closest('.consent').classList.contains('has-error')")
        assert await pg.is_visible('[data-role=adultsRenew]')
        assert not posts, 'a POST went out without the declaration'
        await pg.screenshot(path=f'{SHOTS}/public_consent_renew_{width}.png', full_page=True)
        assert await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), 'horizontal overflow on consent screen'
        await pg.check('[name=adults_confirm]'); assert await pg.is_hidden('[data-role=adultsRenew]')
        await pg.click('[data-role=toReview]'); assert await pg.is_visible('.scr--review.is-active')
        await pg.evaluate("const b=document.querySelector('[data-role=next]'); b.click(); b.click();"); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        assert len(posts) == 1, posts
        sent = json.loads(posts[0][1])
        assert sent['adults_confirm'] is True and sent['move_in'] == '2027-02-15' and not any(k in sent for k in RETIRED), sent
        ok('P4', width, 'renewal enforced; one POST; adults_confirm true')
        # P5 legacy keys
        await pg.click('[data-role=restart] >> nth=0') if await pg.locator('[data-role=restart]').count() else None
        await pg.wait_for_selector('.scr.is-active')
        legacy = dict(GOOD); legacy.pop('adults_confirm'); legacy.update(dob='2000-01-01', sex='Female', sex_pref='No preference', age_pref=['18–21'], consent_age=True, guardian_name='X')
        await pg.evaluate(f"{F}.fill({json.dumps(legacy)})")
        assert not await pg.evaluate("document.querySelector('[name=adults_confirm]').checked"), 'legacy consent_age pre-ticked the declaration'
        c = await pg.evaluate(f"{F}.collect()"); assert c['adults_confirm'] is False and not any(k in c for k in RETIRED)
        ok('P5', width, 'legacy keys ignored; never auto-affirmed')
        assert not errs, errs
        await b.close()
    return sent


async def draft_checks():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': 1280, 'height': 900})).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE + '#/?test=1'); await pg.click('.btn--start'); await pg.wait_for_selector('.scr.is-active')
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, email='qa.draft@example.com', phone='0400000012'))})"); await pg.evaluate(f"{F}.go(26)")
        await pg.click('[data-role=saveLater]'); await pg.wait_for_timeout(1200)
        await pg.wait_for_selector('[data-role=saveBox]:not([hidden])', timeout=8000); link = await pg.input_value('[data-role=saveLink]'); assert '#/resume?t=' in link, link
        pg2 = await (await b.new_context(viewport={'width': 1280, 'height': 900})).new_page(); pg2.on('pageerror', lambda e: errs.append(str(e)))
        await pg2.goto(BASE + link[link.index('#'):]); await pg2.wait_for_selector('.scr.is-active', timeout=15000)
        c = await pg2.evaluate(f"{F}.collect()")
        assert c['adults_confirm'] is True and [sorted(m) for m in c['party_members']] == [['birth_country_raw', 'email', 'name']] and not any(k in c for k in RETIRED), c
        await pg2.evaluate(f"{F}.go(0)"); await pg2.fill('.scr.is-active [name=first_name]', 'Qb'); await pg2.dispatch_event('[name=first_name]', 'change')
        assert not await pg2.evaluate("document.querySelector('[name=adults_confirm]').checked"), 'declaration survived a name change on a resumed draft'
        assert not errs, errs
        ok('P7', 'draft resume restores own declaration; cleared again on a change')
        await b.close()


async def staff_checks():
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': 1280, 'height': 900}); pg = await ctx.new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        patches = []; posts = []
        pg.on('request', lambda r: patches.append((r.url, r.post_data)) if r.method == 'PATCH' else (posts.append((r.url, r.post_data)) if r.method == 'POST' and r.url.endswith('/api/enquiries') else None))
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', PW); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        # S1 overview + list; the QA records are TEST data, so switch the dashboard to the test dataset first
        await pg.goto(BASE + '#/staff/overview'); await pg.wait_for_selector('.card')
        await pg.check('[data-role=dataset] input[value=test]'); await pg.wait_for_timeout(400); await pg.wait_for_selector('.card')
        ov = await pg.inner_text('#view-staff')
        assert 'To do: 18 or older at move-in' in ov and 'guardian' not in ov.lower(), ov[:400]
        await pg.goto(BASE + '#/staff/enquiries'); await pg.wait_for_selector('select[name=todo]')
        opts = await pg.evaluate("[...document.querySelectorAll('select[name=todo] option')].map(o=>o.textContent)")
        assert '18+ to confirm' in opts and not any('uardian' in o for o in opts), opts
        ok('S1', 'overview card + list filter')
        # find the public QA enquiry (test dataset)
        api = lambda path: pg.evaluate(f"fetch('{BASE}api/{path}', {{credentials:'include'}}).then(r=>r.json())")
        lst = await api('enquiries?dataset=test&q=qa.adults1280%40example.com')
        assert lst['items'], lst
        eid = lst['items'][0]['id']
        row = lst['items'][0]
        assert not any(k in row for k in RETIRED) and not row.get('guardian_todo'), row
        await pg.goto(BASE + f'#/staff/enquiries/{eid}'); await pg.wait_for_selector('[data-action=adults-withdraw], [data-action=adults-confirm]')
        det = await pg.inner_text('#view-staff')
        assert 'Confirmed by the enquirer on the form' in det and 'for move-in 15 Feb 2027' in det, det[:800]
        assert '18+ to confirm' not in det
        await no_retired_controls(pg, '.editgrid')
        answers = await pg.inner_text('.kv')
        for w in ('Sex', 'Age range', 'Parent or guardian', '· 18+', 'DOB', 'UNDER 18'):
            assert w not in answers, w
        assert 'Everyone 18+ at move-in' in answers and 'Pal Adults' in answers
        await pg.screenshot(path=f'{SHOTS}/staff_detail_confirmed.png', full_page=True)
        ok('S2', 'detail shows the declaration; no retired controls or rows')
        # S3 earlier move-in via Save answers -> renew; explicit staff confirmation
        patches.clear()
        await pg.fill('.editgrid [name=move_in]', '2027-02-01'); await pg.click('[data-action=save-answers]'); await pg.wait_for_timeout(900)
        sent = [json.loads(d) for u, d in patches if u.endswith(f'/api/enquiries/{eid}')]
        assert len(sent) == 1 and 'adults_confirmed' not in sent[0] and not any(k in sent[0] for k in RETIRED), sent
        await pg.wait_for_selector('[data-action=adults-confirm]', timeout=8000)
        det = await pg.inner_text('#view-staff')
        assert 'A fresh confirmation is needed' in det and '18+ to confirm' in det, det[:800]
        await pg.screenshot(path=f'{SHOTS}/staff_detail_renew.png', full_page=True)
        rec = await api(f'enquiries/{eid}'); assert rec['adults_status'] == 'renew' and rec['adults_confirmed'] is True, {k: rec.get(k) for k in rec if k.startswith('adults')}
        lst = await api('enquiries?dataset=test&todo=adults'); assert any(i['id'] == eid for i in lst['items']), 'renew record missing from the 18+ to-do filter'
        await pg.goto(BASE + '#/staff/enquiries'); await pg.select_option('select[name=todo]', 'adults'); await pg.click('.toolbar button[type=submit]'); await pg.wait_for_selector(f'tr[data-id="{eid}"]')
        assert '18+ to confirm' in (await pg.inner_text(f'tr[data-id="{eid}"]'))
        await pg.goto(BASE + f'#/staff/enquiries/{eid}'); await pg.wait_for_selector('[data-action=adults-confirm]')
        patches.clear(); await pg.click('[data-action=adults-confirm]'); await pg.wait_for_selector('[data-action=adults-withdraw]', timeout=8000)
        sent = [json.loads(d) for u, d in patches]; assert sent == [{'adults_confirmed': True}], sent
        rec = await api(f'enquiries/{eid}'); assert rec['adults_status'] == 'confirmed' and str(rec['adults_confirmed_by']).startswith('staff') and rec['adults_confirmed_move_in'] == '2027-02-01', rec['adults_confirmed_by']
        assert 'Confirmed by staff' in (await pg.inner_text('#view-staff'))
        ok('S3', 'earlier move-in -> renew; staff confirmation is explicit and strict')
        # S4 withdraw
        patches.clear(); await pg.click('[data-action=adults-withdraw]'); await pg.wait_for_timeout(200)
        assert not patches and 'Click again' in (await pg.inner_text('[data-action=adults-withdraw]'))
        await pg.click('[data-action=adults-withdraw]'); await pg.wait_for_selector('[data-action=adults-confirm]', timeout=8000)
        sent = [json.loads(d) for u, d in patches]; assert sent == [{'adults_confirmed': False}], sent
        rec = await api(f'enquiries/{eid}'); assert rec['adults_status'] == 'missing' and rec['adults_confirmed'] is False
        assert 'Not yet declared' in (await pg.inner_text('#view-staff'))
        ok('S4', 'withdraw: two clicks, strict false')
        # S5 staff phone entry
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        await no_retired_controls(pg, '.form-root')
        g = dict(GOOD); g.update(first_name='Qa', last_name='Phone18', email='qa.phone18@example.com', party='Alone', is_test=True); g.pop('party_members'); g.pop('party_size'); g.pop('couples_answer')
        await pg.evaluate(f"{F}.fill({json.dumps(g)})")
        assert await pg.evaluate("document.querySelector('.step[data-step=\"6\"] [name=adults_confirm]')?.checked") is True
        for i in range(7):
            await pg.click('[data-role=next]'); await pg.wait_for_timeout(150)
            cur = await pg.evaluate("document.querySelector('.step.is-active').dataset.step")
            assert cur == str(i + 2), (i, cur, await pg.evaluate("[...document.querySelectorAll('.field-error')].map(e=>e.textContent)"))
        summary = await pg.inner_text('.staff-review')
        assert '18 or older on the move-in date' in summary and 'Date of birth' not in summary and 'Sex' not in summary
        posts.clear(); await pg.click('[data-role=emailForm]'); await pg.wait_for_selector('[data-role=emailBox]:not([hidden])', timeout=15000)
        assert len(posts) == 1 and json.loads(posts[0][1])['adults_confirm'] is True, posts
        body = await pg.input_value('[data-role=emailBody]')
        assert any('18 or older on the move-in date' in l and l.endswith(': Yes') for l in body.splitlines()) and 'Date of birth' not in body and 'Sex' not in body and 'guardian' not in body.lower(), body
        await pg.screenshot(path=f'{SHOTS}/staff_phone_email.png', full_page=True)
        ok('S5', 'staff phone form + email copy clean; one POST')
        # S6 never-declared legacy record
        db = os.environ.get('MMAI_QA_DB')
        if db:
            c = sqlite3.connect(db); c.execute("UPDATE enquiries SET adults_confirmed=0, adults_confirmed_by=NULL, adults_confirmed_at=NULL, adults_confirmed_move_in=NULL, adults_confirmed_party=NULL WHERE email='qa.phone18@example.com'"); c.commit(); c.close()
            lst = await api('enquiries?dataset=test&q=qa.phone18%40example.com'); lid = lst['items'][0]['id']
            await pg.goto(BASE + f'#/staff/enquiries/{lid}'); await pg.wait_for_selector('[data-action=adults-confirm]')
            det = await pg.inner_text('#view-staff'); assert 'Not yet declared' in det and '18+ to confirm' in det
            rec = await api(f'enquiries/{lid}'); assert rec['adults_status'] == 'missing' and rec['adults_confirmed'] is False and rec.get('consent_age', 0) in (0, 1, None, True, False)
            await pg.screenshot(path=f'{SHOTS}/staff_detail_missing.png', full_page=True)
            ok('S6', 'never-declared record is flagged, not auto-affirmed')
        else:
            print('SKIP S6 (set MMAI_QA_DB to run)')
        assert not errs, errs
        await b.close()


async def main():
    await public_checks(1280)
    await public_checks(375)
    ok('P6', 'phone layout')
    await draft_checks()
    await staff_checks()
    print('ALL OK; screenshots in', SHOTS)

asyncio.run(main())
