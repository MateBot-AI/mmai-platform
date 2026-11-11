"""Browser QA (8 November 2026): introduction-only sex and current age (Bill: restored solely for introductions, no role in
matching) on the MateBot form (enquirer screen 1.6/1.7 and each party member's own answers in 3.9), the summary, the receipt
record and the staff detail / edit. Date of birth, sex preference and age preference stay retired.

Runs against an ISOLATED backend with a disposable TEST database (never the shared preview). Creates TEST records only.

    export MMAI_QA_BASE=http://127.0.0.1:8078/
    export MMAI_QA_PASSWORD=...        # that server's password for staff user "bill"
    python3 tools/qa/qa_intro.py

Checks (each prints OK or raises):
  P1 public card form (1280 and 375): the "aboutyou" screen sits between countries and languages, carries the purpose notice
     ("Shared in introductions to proposed housemates; not used for matching"), a Sex select with exactly the agreed options and a
     Current age number box; both optional (Next proceeds with both blank); "The term you use" appears only for "Another term"
     and is required only then; no date-of-birth / preference controls anywhere
  P2 party rows: each other person has their own Sex / term / Current age controls, blank by default (never copied from the
     enquirer); the term box appears per row only for "Another term"
  P3 summary rows show the enquirer's values and "for introductions only: …" per person; collect() carries intro_sex /
     intro_sex_other / intro_age only when given (blank omitted); submit: one POST, 201; the stored record holds each person's own
     values; a blank person is flagged intro_profile_status 'incomplete'
  P4 phone (375): no horizontal overflow on the aboutyou and party screens
  S1 staff detail: "All answers" row "For introductions only (sex, current age)" per person; the missing-flag sentence names the
     blank person; Edit block saves via PATCH on the enquiry and on the member; adults declaration state unchanged after the save;
     "Prefer not to say" shows as given, not as missing
  S2 staff wording: no rule codes in the introduction-details UI text; no "date of birth" / "sex preference" / "age range"
"""
import asyncio, json, os, re, sys
from playwright.async_api import async_playwright

BASE = os.environ.get('MMAI_QA_BASE', 'http://127.0.0.1:8078/').rstrip('/') + '/'
PW = os.environ.get('MMAI_QA_PASSWORD', '')
SHOTS = os.environ.get('MMAI_QA_SHOTS', '/tmp/mmai-qa-intro')
os.makedirs(SHOTS, exist_ok=True)

RETIRED = ['dob', 'sex', 'sex_other', 'sex_pref', 'age_pref', 'guardian_name', 'consent_age']
OPTIONS = ['Female', 'Male', 'Another term', 'Prefer not to say']
PURPOSE = 'Shared in introductions to proposed housemates; not used for matching'
GOOD = dict(first_name="Qa", last_name="Intro", email="qa.intro@example.com", phone_code="+61", phone="0400000031", contact_method="Email",
            birth_country_raw="Sweden", live_country_raw="Sweden", languages=["English"],
            city="Melbourne", move_in="2027-03-01", move_in_flex="± 1 week", move_out="2027-09-10", move_out_flex="Exact date",
            budget_option="2B2B-single", apt_sizes=["2 bedrooms"], mate_ideal="2", mate_max="4", rooms=["Own room"], bunk_ok="No", room_first="Own room",
            party="With friends", party_size="3", couples_answer="No", doing="Working", work="Melbourne CBD", smoke="No", smoke_ok="Yes", source="Google",
            consent_store=True, consent_share=True, adults_confirm=True)
F = "document.querySelector('.form-root').matebotForm"
ok = lambda tag, *rest: print('OK', tag, *rest)


async def go_scr(pg, scr):
    await pg.evaluate("(()=>{const vs=[...document.querySelectorAll('.step:not(.staff-step) .scr')]; const i=vs.findIndex(s=>s.dataset.scr==='" + scr + "'); " + F + ".go(i);})()")
    await pg.wait_for_timeout(120)
    assert await pg.evaluate("document.querySelector('.scr.is-active').dataset.scr") == scr


async def public_checks(width):
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': width, 'height': 900})).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        posts = []
        pg.on('request', lambda r: posts.append((r.url, r.post_data)) if r.method == 'POST' and '/api/public/enquiries' in r.url else None)
        await pg.goto(BASE + '#/?test=1'); await pg.click('.btn--start'); await pg.wait_for_selector('.scr.is-active')
        # P1
        ids = await pg.evaluate("[...document.querySelectorAll('.step:not(.staff-step) .scr')].map(s=>s.dataset.scr)")
        assert len(ids) == 27 and ids.index('aboutyou') == ids.index('countries') + 1 == ids.index('languages') - 1, ids
        names = await pg.evaluate("[...document.querySelectorAll('.form-root input, .form-root select')].map(e=>e.name||e.dataset.pm||'')")
        assert not any(n in RETIRED for n in names), [n for n in names if n in RETIRED]
        txt = (await pg.inner_text('.form-root')).lower()
        for w in ('date of birth', 'sex preference', 'age range', 'guardian'):
            assert w not in txt, w
        await go_scr(pg, 'aboutyou')
        scr = await pg.inner_text('.scr.is-active')
        assert PURPOSE in scr and 'Sex (optional)' in scr and 'Current age (optional)' in scr, scr
        opts = await pg.evaluate("[...document.querySelectorAll('.scr.is-active select[name=intro_sex] option')].map(o=>o.value)")
        assert opts == [''] + OPTIONS, opts
        assert await pg.evaluate("document.querySelector('.scr.is-active input[name=intro_age]').type") == 'number'
        assert not await pg.evaluate("document.querySelector('.scr.is-active input[name=intro_sex_other]').closest('.reveal').classList.contains('is-shown')")
        assert not await pg.evaluate("document.querySelector('.scr.is-active input[name=intro_sex_other]').required")
        # both blank -> Next proceeds (optional)
        await pg.click('[data-role=next]'); await pg.wait_for_timeout(150)
        assert await pg.evaluate("document.querySelector('.scr.is-active').dataset.scr") == 'languages', 'blank intro details blocked Next'
        await go_scr(pg, 'aboutyou')
        await pg.select_option('.scr.is-active select[name=intro_sex]', 'Another term'); await pg.wait_for_timeout(100)
        assert await pg.evaluate("document.querySelector('.scr.is-active input[name=intro_sex_other]').closest('.reveal').classList.contains('is-shown')")
        assert await pg.evaluate("document.querySelector('.scr.is-active input[name=intro_sex_other]').required")
        await pg.click('[data-role=next]'); await pg.wait_for_timeout(150)
        assert await pg.evaluate("document.querySelector('.scr.is-active').dataset.scr") == 'aboutyou', '"Another term" without the term was allowed through'
        await pg.fill('.scr.is-active input[name=intro_sex_other]', 'Non-binary'); await pg.fill('.scr.is-active input[name=intro_age]', '24')
        await pg.screenshot(path=f'{SHOTS}/public_aboutyou_{width}.png', full_page=True)
        if width < 500:
            assert not await pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"), 'horizontal overflow on aboutyou'
        ok('P1', width, 'aboutyou screen: purpose notice, agreed options, optional, term only for Another term')
        # P2 party rows
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, email=f'qa.intro{width}@example.com', phone=f'04000{width:05d}'))})")
        await go_scr(pg, 'party')
        rows = await pg.evaluate("[...document.querySelectorAll('.party-row')].map(r=>({sex:r.querySelector('[data-pm=intro_sex]').value, age:r.querySelector('[data-pm=intro_age]').value, other:r.querySelector('.party-cell--other').classList.contains('is-shown'), opts:[...r.querySelectorAll('[data-pm=intro_sex] option')].map(o=>o.value)}))")
        assert len(rows) == 2 and all(r['sex'] == '' and r['age'] == '' and not r['other'] and r['opts'] == [''] + OPTIONS for r in rows), rows   # blank: never copied from the enquirer
        cap = (await pg.inner_text('.party-row')).lower()
        assert 'for introductions only' in cap and 'not used for matching' in cap, cap
        await pg.fill('.party-row[data-index="0"] [data-pm=name]', 'Bo Friend'); await pg.fill('.party-row[data-index="0"] [data-pm=email]', f'bo{width}@example.com'); await pg.fill('.party-row[data-index="0"] [data-pm=birth_country_raw]', 'Norway')
        await pg.select_option('.party-row[data-index="0"] [data-pm=intro_sex]', 'Another term'); await pg.wait_for_timeout(100)
        assert await pg.evaluate("document.querySelector('.party-row[data-index=\"0\"] .party-cell--other').classList.contains('is-shown')")
        assert not await pg.evaluate("document.querySelector('.party-row[data-index=\"1\"] .party-cell--other').classList.contains('is-shown')"), 'term box leaked to another row'
        await pg.fill('.party-row[data-index="0"] [data-pm=intro_sex_other]', 'Agender'); await pg.fill('.party-row[data-index="0"] [data-pm=intro_age]', '19')
        await pg.fill('.party-row[data-index="1"] [data-pm=name]', 'Cy Friend'); await pg.fill('.party-row[data-index="1"] [data-pm=email]', f'cy{width}@example.com'); await pg.fill('.party-row[data-index="1"] [data-pm=birth_country_raw]', 'Chile')
        await pg.select_option('.party-row[data-index="1"] [data-pm=intro_sex]', 'Prefer not to say')   # no age: Cy stays "incomplete" for staff
        await pg.screenshot(path=f'{SHOTS}/public_party_{width}.png', full_page=True)
        if width < 500:
            assert not await pg.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"), 'horizontal overflow on party rows'
        ok('P2', width, 'party rows: own blank controls per person; term box per row')
        # P3 summary + collect + submit. Editing the party rows clears the 18+ declaration on purpose (it was made about other
        # people), so it is ticked again here exactly as a visitor would.
        await pg.evaluate("document.activeElement && document.activeElement.blur()")   # commit the last typed party field (its change clears the tick, as intended)
        assert not await pg.evaluate("document.querySelector('[name=adults_confirm]').checked"), 'declaration survived a party change'
        await pg.evaluate("document.querySelector('[name=adults_confirm]').click()")
        # ...but a later change to someone's introduction-only details does NOT clear it (they are not part of who the people are)
        await pg.fill('.party-row[data-index="0"] [data-pm=intro_age]', '20'); await pg.dispatch_event('.party-row[data-index="0"] [data-pm=intro_age]', 'change')
        assert await pg.evaluate("document.querySelector('[name=adults_confirm]').checked"), 'intro-only edit cleared the declaration'
        await pg.fill('.party-row[data-index="0"] [data-pm=intro_age]', '19')
        await pg.evaluate(f"{F}.go(999)"); assert await pg.is_visible('.scr--review.is-active')
        flat = re.sub(r'\s+', ' ', await pg.inner_text('[data-role=review]'))
        assert 'Sex (optional) Another term' in flat and 'The term you use Non-binary' in flat and 'Current age (optional) 24' in flat, flat
        assert 'for introductions only: Agender, 19' in flat and 'for introductions only: Prefer not to say' in flat, flat
        for w in ('Date of birth', 'age range', 'sex preference'):
            assert w not in flat, w
        c = await pg.evaluate(f"{F}.collect()")
        assert (c['intro_sex'], c['intro_sex_other'], c['intro_age']) == ('Another term', 'Non-binary', '24'), c
        assert c['party_members'][0]['intro_sex'] == 'Another term' and c['party_members'][0]['intro_sex_other'] == 'Agender' and c['party_members'][0]['intro_age'] == '19', c['party_members']
        assert c['party_members'][1]['intro_sex'] == 'Prefer not to say' and 'intro_age' not in c['party_members'][1] and 'intro_sex_other' not in c['party_members'][1], c['party_members']
        assert not any(k in c for k in RETIRED) and not any(k in m for m in c['party_members'] for k in RETIRED)
        await pg.evaluate("const b=document.querySelector('[data-role=next]'); b.click(); b.click();"); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=45000)   # party-member country lookups can take a while
        assert len(posts) == 1, posts
        sent = json.loads(posts[0][1])
        assert sent['intro_sex'] == 'Another term' and sent['party_members'][1]['intro_sex'] == 'Prefer not to say', sent
        assert not errs, errs
        ok('P3', width, 'intro-only edit keeps the 18+ declaration; review summary, collect() and the submitted enquiry carry the answers')
        await b.close()
    return sent


async def staff_checks():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': 1280, 'height': 900})).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        patches = []
        pg.on('request', lambda r: patches.append((r.url, r.post_data)) if r.method == 'PATCH' else None)
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', PW); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        api = lambda path: pg.evaluate(f"fetch('{BASE}api/{path}', {{headers:{{Authorization:'Bearer '+MMAI_API.getToken()}}}}).then(r=>r.json())")
        lst = await api('enquiries?dataset=test&q=qa.intro1280%40example.com')
        assert lst['items'], lst
        eid = lst['items'][0]['id']
        rec = await api(f'enquiries/{eid}')
        assert (rec['intro_sex'], rec['intro_sex_other'], rec['intro_age']) == ('Another term', 'Non-binary', 24), rec
        bo, cy = rec['party_members']
        assert (bo['intro_sex'], bo['intro_sex_other'], bo['intro_age']) == ('Another term', 'Agender', 19) and (cy['intro_sex'], cy['intro_age']) == ('Prefer not to say', None)
        assert rec['intro_profile_status'] == 'incomplete' and rec['intro_missing'] == ['Cy Friend'] and rec['adults_status'] == 'confirmed', rec
        assert not any(k in rec for k in RETIRED)
        await pg.goto(BASE + f'#/staff/enquiries/{eid}'); await pg.wait_for_selector('.kv', timeout=10000)
        det = await pg.inner_text('#view-staff')
        assert 'For introductions only (sex, current age)' in det and 'Qa: Non-binary, 24' in det and 'Bo Friend: Agender, 19' in det and 'Cy Friend: Prefer not to say' in det, det[:1500]
        assert 'Not yet given by: Cy Friend' in det and 'Introduction details' in det and PURPOSE in det, det[:1500]
        low = det.lower()
        for w in ('date of birth', 'sex preference', 'age range'):
            assert w not in low, w
        # S1 edits: enquirer -> Female 25; Cy -> age 21 (Prefer not to say kept)
        await pg.select_option('[name=lead_intro_sex]', 'Female'); await pg.fill('[name=lead_intro_age]', '25')
        await pg.click('[data-action=save-intro]')
        await pg.wait_for_function("(document.querySelector('[data-role=msg2]')||{}).textContent.includes('Introduction details saved')", timeout=3000)
        await pg.wait_for_timeout(900)   # the detail re-renders 600 ms after a save
        row_sel = f'.member-row[data-member="{cy["id"]}"]'
        await pg.wait_for_selector(row_sel)
        await pg.fill(f'{row_sel} [name=m{cy["id"]}_intro_age]', '21')
        await pg.click(f'{row_sel} [data-action=save-member-intro]'); await pg.wait_for_timeout(900)
        sent = [(u, json.loads(d)) for u, d in patches]
        lead = [d for u, d in sent if u.endswith(f'/api/enquiries/{eid}')]
        mem = [d for u, d in sent if u.endswith(f'/api/enquiries/{eid}/members/{cy["id"]}')]
        assert lead == [{'intro_sex': 'Female', 'intro_sex_other': None, 'intro_age': '25'}], lead
        assert mem == [{'intro_sex': 'Prefer not to say', 'intro_sex_other': None, 'intro_age': '21'}], mem
        rec2 = await api(f'enquiries/{eid}')
        assert (rec2['intro_sex'], rec2['intro_sex_other'], rec2['intro_age']) == ('Female', None, 25) and rec2['party_members'][1]['intro_age'] == 21, rec2
        assert rec2['intro_profile_status'] == 'complete' and rec2['adults_status'] == 'confirmed' and rec2['adults_confirmed_at'] == rec['adults_confirmed_at'], rec2
        det2 = await pg.inner_text('#view-staff')
        assert 'Not yet given by' not in det2 and 'Qa: Female, 25' in det2 and 'Cy Friend: Prefer not to say, 21' in det2, det2[:1500]
        await pg.screenshot(path=f'{SHOTS}/staff_intro.png', full_page=True)
        ok('S1', 'staff detail rows, missing flag, enquirer + member edits via PATCH; declaration untouched')
        # S2 wording
        block = await pg.evaluate("(()=>{const h=[...document.querySelectorAll('#view-staff h4')].find(x=>x.textContent.trim()==='Introduction details'); let t=''; for(let n=h; n && n.tagName!=='H4' || n===h; n=n.nextElementSibling){ t+=' '+n.textContent; if(n!==h && n.tagName==='H4') break;} return t;})()")
        assert not re.search(r'\b(GP|T|I|A|H|M)\d{1,2}\b', block), block
        assert not errs, errs
        ok('S2', 'plain staff wording, no rule codes')
        await b.close()


async def main():
    await public_checks(1280)
    await public_checks(375)
    ok('P4', 'phone layout: no overflow on aboutyou / party screens')
    await staff_checks()
    print('ALL OK; screenshots in', SHOTS)


if __name__ == '__main__':
    asyncio.run(main())
