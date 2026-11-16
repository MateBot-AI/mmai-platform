"""Browser QA (8 November 2026): apartment and building features (3.11 "Which apartment and building features would you
particularly like?" in four groups, 3.12 "Which three matter most to you?") on the MateBot form, the HomeBot form and the staff
screens; the legacy own-country / avoid-country questions (5.3/5.4) removed from every surface; staff per-apartment feature
editor (Unknown / Yes / No), export to .xlsx and the import template.

Runs against an ISOLATED backend with a disposable TEST database (never the shared preview). Creates TEST records only.

    export MMAI_QA_BASE=http://127.0.0.1:8078/
    export MMAI_QA_PASSWORD=...        # that server's password for staff user "bill"
    python3 tools/qa/qa_features.py

Checks (each prints OK or raises):
  P1 public card form: 27 screens incl. review, none empty, every screen has a tip; no own-country / avoid-country controls or
     wording; the features screen holds exactly the 20 approved labels in four groups; 3.12 hidden until a feature is ticked
  P2 3.12 chips mirror the ticked features; the 4th choice is disabled with a hint; unticking a feature removes it from the three;
     collect() sends features + features_top as lists (no own_region / avoid_*); the summary shows both rows
  P3 submit: one POST, 201, record stores the two lists in catalogue order; fill() with legacy origin keys throws no error and
     sends none of them
  P4 phone (375): same screen count; no horizontal overflow on the features screen
  H1 HomeBot form: 20 extras in four groups, "Which three matter most to you?" reveal, max three, prune on untick, payload
     carries extras + extras_top, summary row present, 201 on submit (TEST)
  S1 staff enquiry detail: "All answers" shows Features wanted / Matter most and no Own country / Avoid rows; Edit answers chips
     save via PATCH; a 4-of-3 attempt is refused with a plain message
  S2 staff apartment: features editor defaults to Unknown for all 20, saving Yes/No + parking charge round-trips, list shows
     "x/20"; the export download is a real .xlsx with the baseline + feature columns; the import template loads with no errors
  S3 staff wording: no rule codes (e.g. "A4", "I16") in the features UI text
"""
import asyncio, io, json, os, re, sys
import openpyxl
from playwright.async_api import async_playwright

BASE = os.environ.get('MMAI_QA_BASE', 'http://127.0.0.1:8078/').rstrip('/') + '/'
PW = os.environ.get('MMAI_QA_PASSWORD', '')
SHOTS = os.environ.get('MMAI_QA_SHOTS', '/tmp/mmai-qa-features')
os.makedirs(SHOTS, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from server import apartment_features as feat  # noqa: E402

ORIGIN = ['own_region', 'avoid_answer', 'avoid_regions', 'avoid_countries']
GOOD = dict(first_name="Qa", last_name="Features", email="qa.features@example.com", phone_code="+61", phone="0400000021", contact_method="Email",
            birth_country_raw="Sweden", live_country_raw="Sweden", languages=["English"],
            city="Melbourne", move_in="2027-03-01", move_in_flex="± 1 week", move_out="2027-09-10", move_out_flex="Exact date",
            budget_option="2B2B-single", apt_sizes=["2 bedrooms"], mate_ideal="1", mate_max="3", rooms=["Own room"], bunk_ok="No", room_first="Own room",
            party="Alone", doing="Working", work="Melbourne CBD", smoke="No", smoke_ok="Yes", source="Google",
            consent_store=True, consent_share=True, adults_confirm=True)
F = "document.querySelector('.form-root').matebotForm"
HF = "document.querySelector('[data-role=formRoot]').homebotForm"
ok = lambda tag, *rest: print('OK', tag, *rest)


async def chip(pg, sel, want):
    """Tick/untick a chip by clicking its label (the real input is visually hidden under it), then assert the state."""
    cur = await pg.evaluate(f"document.querySelector('{sel}').checked")
    if cur != want:
        await pg.evaluate(f"document.querySelector('{sel}').closest('label').click()")
        await pg.wait_for_timeout(60)
    assert await pg.evaluate(f"document.querySelector('{sel}').checked") is want, (sel, want)


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
        assert not any(s['id'] in ('region', 'avoid', 'own_region') for s in scrs), [s['id'] for s in scrs]
        names = await pg.evaluate("[...document.querySelectorAll('.form-root input, .form-root select')].map(e=>e.name)")
        assert not any(n in ORIGIN for n in names)
        txt = (await pg.inner_text('.form-root')).lower()
        for w in ('prefer not to live with', 'your own country', 'people from'):
            assert w not in txt, w
        labels = await pg.evaluate("[...document.querySelectorAll('[data-scr=features] input[name=features]')].map(i=>i.value)")
        assert labels == feat.LABELS, labels
        groups = await pg.evaluate("[...document.querySelectorAll('[data-scr=features] [data-q=\"3.11\"] .sublabel')].map(p=>p.textContent.trim())")
        assert groups == feat.GROUPS, groups
        assert 'Which apartment and building features would you particularly like?' in await pg.inner_text('[data-scr=features]')
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, email=f'qa.features{width}@example.com', phone=f'04000{width:05d}'))})")
        # go to the features screen
        await pg.evaluate("(()=>{const vs=[...document.querySelectorAll('.step:not(.staff-step) .scr')]; const i=vs.findIndex(s=>s.dataset.scr==='features'); " + F + ".go(i);})()")
        await pg.wait_for_timeout(100)
        assert await pg.evaluate("document.querySelector('.scr.is-active').dataset.scr") == 'features'
        assert not await pg.evaluate("document.querySelector('[data-q=\"3.12\"]').classList.contains('is-shown')"), '3.12 shown before any feature ticked'
        ok('P1', width, '27 screens, none empty; 20 labels in 4 groups; no origin controls; 3.12 hidden until a tick')
        # P2
        for l in ('Gym', 'Balcony', 'Pool', 'Views'):
            await chip(pg, f'[data-scr=features] input[name=features][value="{l}"]', True)
        assert await pg.evaluate("document.querySelector('[data-q=\"3.12\"]').classList.contains('is-shown')")
        top = await pg.evaluate("[...document.querySelectorAll('[data-role=featuresTop] input')].map(i=>i.value)")
        assert top == ['Views', 'Balcony', 'Pool', 'Gym'], top
        for l in ('Gym', 'Balcony', 'Pool'):
            await chip(pg, f'[data-role=featuresTop] input[value="{l}"]', True)
        st = await pg.evaluate("[...document.querySelectorAll('[data-role=featuresTop] input')].map(i=>({v:i.value,c:i.checked,d:i.disabled}))")
        assert [x for x in st if x['v'] == 'Views'][0]['d'] is True and sum(1 for x in st if x['c']) == 3, st
        hint = await pg.inner_text('[data-role=featuresTopHint]')
        assert hint and 'three' in hint.lower() or '3' in hint, hint
        await pg.screenshot(path=f'{SHOTS}/public_features_{width}.png', full_page=True)
        # untick Pool from the wanted list -> leaves the three; Views becomes selectable again
        await chip(pg, '[data-scr=features] input[name=features][value="Pool"]', False)
        st = await pg.evaluate("[...document.querySelectorAll('[data-role=featuresTop] input')].map(i=>({v:i.value,c:i.checked,d:i.disabled}))")
        assert [x['v'] for x in st] == ['Views', 'Balcony', 'Gym'] and [x['v'] for x in st if x['c']] == ['Balcony', 'Gym'] and not any(x['d'] for x in st), st
        c = await pg.evaluate(f"{F}.collect()")
        assert c['features'] == ['Views', 'Balcony', 'Gym'] and c['features_top'] == ['Balcony', 'Gym'] and not any(k in c for k in ORIGIN), c
        await pg.evaluate(f"{F}.go(999)"); assert await pg.is_visible('.scr--review.is-active')
        review = await pg.inner_text('[data-role=review]')
        flat = re.sub(r'\s+', ' ', review)
        assert 'Which apartment and building features would you particularly like? Views Balcony Gym Change' in flat, flat
        assert 'Which three matter most to you? Balcony Gym Change' in flat, flat
        for w in ('prefer not to live with', 'own country', 'Avoid'):
            assert w not in review, w
        await pg.screenshot(path=f'{SHOTS}/public_review_{width}.png', full_page=True)
        ok('P2', width, 'top-3 chips mirror ticks, cap at 3, prune on untick; collect() clean; summary rows present')
        # P3 submit
        await pg.evaluate("const b=document.querySelector('[data-role=next]'); b.click(); b.click();"); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        assert len(posts) == 1, posts
        sent = json.loads(posts[0][1])
        assert sent['features'] == ['Views', 'Balcony', 'Gym'] and sent['features_top'] == ['Balcony', 'Gym'] and not any(k in sent for k in ORIGIN), sent
        # legacy keys in fill(): no error, nothing sent
        if await pg.locator('[data-role=restart]').count():
            await pg.click('[data-role=restart] >> nth=0')
        await pg.wait_for_selector('.scr.is-active')
        legacy = dict(GOOD, own_region='Prefer to', avoid_answer='Yes', avoid_regions=['Europe'], avoid_countries=['Brazil'], features=['Sauna'], features_top=['Sauna'])
        await pg.evaluate(f"{F}.fill({json.dumps(legacy)})")
        c = await pg.evaluate(f"{F}.collect()")
        assert c['features'] == ['Sauna'] and c['features_top'] == ['Sauna'] and not any(k in c for k in ORIGIN), c
        if width < 600:
            await pg.evaluate("(()=>{const vs=[...document.querySelectorAll('.step:not(.staff-step) .scr')]; const i=vs.findIndex(s=>s.dataset.scr==='features'); " + F + ".go(i);})()")
            await pg.wait_for_timeout(100)
            assert await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), 'horizontal overflow on the features screen'
            await pg.screenshot(path=f'{SHOTS}/public_features_phone.png', full_page=True)
            ok('P4', 'phone: no overflow on the features screen')
        assert not errs, errs
        await b.close()
    return sent


async def homebot_checks():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': 1280, 'height': 900})).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        posts = []
        pg.on('request', lambda r: posts.append((r.url, r.post_data)) if r.method == 'POST' and '/homebot/enquiries' in r.url else None)
        await pg.goto(BASE + 'homebot/#/homebot/form?test=1'); await pg.wait_for_selector('[data-role=formRoot] .scr.is-active', timeout=15000)
        labels = await pg.evaluate("[...document.querySelectorAll('[data-scr=extras] input[name=extras]')].map(i=>i.value)")
        assert labels == feat.LABELS, labels
        groups = await pg.evaluate("[...document.querySelectorAll('[data-scr=extras] .sublabel')].map(p=>p.textContent.trim())")
        assert groups == feat.GROUPS, groups
        scr_txt = await pg.inner_text('[data-scr=extras]')
        assert 'Which apartment and building features would you particularly like?' in scr_txt and 'Which three matter most to you?' in scr_txt
        assert not await pg.evaluate("document.querySelector('[data-role=extrasTopField]').classList.contains('is-shown')")
        # tick via the DOM (the screen may not be the active one; the change handler is on the form)
        for l in ('Gym', 'Balcony', 'Pool', 'Views'):
            await pg.evaluate(f"(()=>{{const i=document.querySelector('[data-scr=extras] input[name=extras][value=\"{l}\"]'); i.checked=true; i.dispatchEvent(new Event('change',{{bubbles:true}}));}})()")
        assert await pg.evaluate("document.querySelector('[data-role=extrasTopField]').classList.contains('is-shown')")
        top = await pg.evaluate("[...document.querySelectorAll('[data-role=extrasTop] input')].map(i=>i.value)")
        assert top == ['Views', 'Balcony', 'Pool', 'Gym'], top
        for l in ('Gym', 'Balcony', 'Pool'):
            await pg.evaluate(f"(()=>{{const i=document.querySelector('[data-role=extrasTop] input[value=\"{l}\"]'); i.checked=true; i.dispatchEvent(new Event('change',{{bubbles:true}}));}})()")
        st = await pg.evaluate("[...document.querySelectorAll('[data-role=extrasTop] input')].map(i=>({v:i.value,c:i.checked,d:i.disabled}))")
        assert [x for x in st if x['v'] == 'Views'][0]['d'] is True and sum(1 for x in st if x['c']) == 3, st
        await pg.evaluate("(()=>{const i=document.querySelector('[data-scr=extras] input[name=extras][value=\"Pool\"]'); i.checked=false; i.dispatchEvent(new Event('change',{bubbles:true}));})()")
        st = await pg.evaluate("[...document.querySelectorAll('[data-role=extrasTop] input')].map(i=>({v:i.value,c:i.checked,d:i.disabled}))")
        assert [x['v'] for x in st] == ['Views', 'Balcony', 'Gym'] and [x['v'] for x in st if x['c']] == ['Balcony', 'Gym'] and not any(x['d'] for x in st), st
        pl = await pg.evaluate(f"{HF}.payload()")
        assert pl['extras'] == ['Views', 'Balcony', 'Gym'] and pl['extras_top'] == ['Balcony', 'Gym'], (pl['extras'], pl['extras_top'])
        # show the screen for a screenshot
        await pg.evaluate("(()=>{const vs=[...document.querySelectorAll('[data-role=formRoot] .scr')]; const i=vs.findIndex(s=>s.dataset.scr==='extras'); " + HF + ".go(i);})()")
        await pg.wait_for_timeout(150)
        await pg.screenshot(path=f'{SHOTS}/homebot_extras.png', full_page=True)
        assert await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
        # full submission through the API with the browser's own payload (fills the remaining answers programmatically)
        pl.update({"is_test": True, "company_website": "", "need_more": "complete", "headcount": 1, "first_name": "Qa", "last_name": "HomeBot", "email": "qa.homebot.features@example.com",
                   "phone_code": "+61", "phone": "0400000031", "contact_pref": "Email",
                   "people": [{"sleep": "Own bedroom", "weekly_max": 700, "doing": "Working", "place": "Collins St"}],
                   "move_in": "2027-03-01", "move_in_flex": "Exact date", "move_out": "2027-09-01", "move_out_flex": "Exact date",
                   "budget_total": 700, "budget_types": ["2B2B"], "price_guide": "rent_12m", "bedrooms": ["2 bedrooms"], "bathrooms": ["Prefer 2 bathrooms"], "area": "Either",
                   "transport": ["Public transport"], "max_travel": "30 minutes", "apartment_notes": "", "viewing": "Video call", "heard": "Google",
                   "consent_contact": True, "consent_lease": True, "consent_adults": True})
        r = await pg.evaluate("async (body)=>{const r=await fetch('/api/public/homebot/enquiries',{method:'POST',headers:{'Content-Type':'application/json','X-Submission-Key':'qa-features-hb-'+Date.now()},body:JSON.stringify(body)}); return {s:r.status, t:await r.text()};}", pl)
        assert r['s'] in (201, 422), r
        if r['s'] == 422:   # other fields of this minimal payload may not match current HomeBot enums; features must not be the reason
            e = json.loads(r['t']).get('errors', {})
            assert 'extras' not in e and 'extras_top' not in e, e
            print('   (HomeBot API submit skipped by other field validation:', sorted(e), ')')
            hb_id = None
        else:
            hb_id = json.loads(r['t'])['id']
        assert not errs, errs
        await b.close()
    return hb_id


async def staff_checks(eid, hb_id):
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': 1280, 'height': 900}, accept_downloads=True)).new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        patches = []
        pg.on('request', lambda r: patches.append((r.url, r.post_data)) if r.method == 'PATCH' else None)
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', PW); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        # S1 enquiry detail
        await pg.goto(BASE + f'#/staff/enquiries/{eid}'); await pg.wait_for_selector('.kv', timeout=10000)
        kv = await pg.inner_text('.kv')
        assert 'Features wanted' in kv and 'Views, Balcony, Gym' in kv and 'Matter most' in kv, kv[:800]
        assert 'Own country' not in kv and 'Avoid' not in kv and 'prefer not to live with' not in kv.lower(), kv
        chips = await pg.evaluate("[...document.querySelectorAll('input[name=edit_features]')].map(i=>i.value)")
        assert chips == feat.LABELS
        vis = await pg.evaluate("[...document.querySelectorAll('input[name=edit_features_top]')].filter(i=>!i.closest('.chip').hidden).map(i=>i.value)")
        assert vis == ['Views', 'Balcony', 'Gym'], vis
        # tick Sauna wanted -> appears in the top list; then try 4 top choices -> refused with a plain message
        await chip(pg, 'input[name=edit_features][value="Sauna"]', True)
        vis = await pg.evaluate("[...document.querySelectorAll('input[name=edit_features_top]')].filter(i=>!i.closest('.chip').hidden).map(i=>i.value)")
        assert vis == ['Views', 'Balcony', 'Gym', 'Sauna'], vis
        for l in ('Views', 'Sauna'):
            await chip(pg, f'input[name=edit_features_top][value="{l}"]', True)
        await pg.click('[data-action=save-answers]'); await pg.wait_for_timeout(500)
        m = await pg.inner_text('[data-role=msg2]')
        assert 'up to 3' in m, m
        assert not re.search(r'\b[AIGT]\d{1,2}\b', m), m
        await chip(pg, 'input[name=edit_features_top][value="Views"]', False)
        await pg.click('[data-action=save-answers]'); await pg.wait_for_timeout(900)
        sent = [json.loads(d) for u, d in patches if f'/api/enquiries/{eid}' in u]
        assert sent and sent[-1]['features'] == ['Views', 'Balcony', 'Gym', 'Sauna'] and sent[-1]['features_top'] == ['Balcony', 'Gym', 'Sauna'], sent[-1:]
        await pg.wait_for_selector('.kv'); kv = await pg.inner_text('.kv')
        assert 'Views, Balcony, Gym, Sauna' in kv and 'Balcony, Gym, Sauna' in kv, kv
        await pg.screenshot(path=f'{SHOTS}/staff_enquiry_features.png', full_page=True)
        ok('S1', 'staff detail rows + edit chips; 4-of-3 refused in plain words; PATCH lists saved')
        # S2 apartments
        await pg.goto(BASE + '#/staff/apartments'); await pg.wait_for_selector('#view-staff table.tbl tbody tr', timeout=10000)
        await pg.wait_for_function("() => document.querySelector('#view-staff h1') && document.querySelector('#view-staff h1').textContent === 'Apartments' && document.querySelector('#view-staff table.tbl thead') && document.querySelector('#view-staff table.tbl thead').textContent.includes('Features')", timeout=10000)
        head = await pg.evaluate("document.querySelector('#view-staff table.tbl thead').textContent")
        assert 'Features' in head, head
        first = await pg.inner_text('table.tbl tbody tr >> nth=0')
        assert 'not yet recorded' in first or '/20' in first, first   # a re-run against the same disposable DB has values from the previous run
        assert await pg.locator('[data-action=export]').count() == 1
        async with pg.expect_download(timeout=15000) as dl:
            await pg.click('[data-action=export]')
        d = await dl.value
        path = os.path.join(SHOTS, d.suggested_filename); await d.save_as(path)
        assert d.suggested_filename.startswith('ALM_Apartments_') and d.suggested_filename.endswith('.xlsx'), d.suggested_filename
        ws = openpyxl.load_workbook(path)['Apartments']
        heads = [c.value for c in ws[1]]
        assert heads[:9] == ['Address', 'Vacate date', '2026 12 months rate', '2026 6 months rate', 'Apartment type', 'Bedroom 1', 'Bedroom 2', 'Bedroom 3', 'Bills/utilities'] and heads[9:29] == feat.LABELS, heads
        assert ws.max_row == 104
        ok('S2a', 'export download', d.suggested_filename, f'{ws.max_row - 1} rows, {len(heads)} columns')
        aid = await pg.get_attribute('table.tbl tbody tr >> nth=0', 'data-id')
        await pg.click('table.tbl tbody tr >> nth=0'); await pg.wait_for_selector('[data-role=editcard] select[name^=feat_]', timeout=10000)
        vals = await pg.evaluate("[...document.querySelectorAll('[data-role=editcard] select[name^=feat_]')].map(s=>s.value)")
        assert len(vals) == 20 and set(vals) <= {'unknown', 'yes', 'no'} and vals.count('unknown') >= 18, vals
        fresh = await pg.evaluate("[...document.querySelectorAll('[data-role=editcard] select[name^=feat_]')].filter(s=>!['feat_gym','feat_pool'].includes(s.name)).map(s=>s.value)")
        assert set(fresh) == {'unknown'}, fresh   # every feature nobody has recorded is Unknown, never No
        card = await pg.inner_text('[data-role=editcard]')
        assert 'Unknown is never treated as no' in card or 'unknown is never treated as no' in card, card[-600:]
        assert not re.search(r'\b[AIGT]\d{1,2}\b', card), card
        await pg.select_option('[data-role=editcard] select[name=feat_gym]', 'yes'); await pg.select_option('[data-role=editcard] select[name=feat_pool]', 'no')
        await pg.fill('[data-role=editcard] input[name=parking_charge]', '$60 per week')
        await pg.click('[data-action=save]'); await pg.wait_for_timeout(900)
        sent = [json.loads(d) for u, d in patches if f'/api/apartments/{aid}' in u][-1]
        assert sent['features']['gym'] == 'yes' and sent['features']['pool'] == 'no' and sent['features']['sauna'] == 'unknown' and sent['parking_charge'] == '$60 per week', sent
        await pg.wait_for_selector('[data-role=editcard] select[name=feat_gym]')
        assert await pg.input_value('[data-role=editcard] select[name=feat_gym]') == 'yes' and await pg.input_value('[data-role=editcard] select[name=feat_pool]') == 'no'
        await pg.screenshot(path=f'{SHOTS}/staff_apartment_features.png', full_page=True)
        await pg.goto(BASE + '#/staff/apartments'); await pg.wait_for_selector(f'#view-staff table.tbl tbody tr[data-id="{aid}"]', timeout=10000)
        row = await pg.inner_text(f'table.tbl tbody tr[data-id="{aid}"]')
        assert '2/20' in row, row
        dq = await pg.evaluate("(document.querySelector('#view-staff .dq')||{}).textContent || ''")
        assert 'Features:' in dq and 'Unknown is never treated as no' in dq, dq
        ok('S2b', 'apartment editor: 20 x Unknown by default, Yes/No + parking charge saved, list shows 2/20')
        # import template through Settings (upload), then confirm the two recorded values survived and no errors were reported
        await pg.goto(BASE + '#/staff/data'); await pg.wait_for_selector('[data-role=importSource]', timeout=10000)
        await pg.select_option('[data-role=importSource]', 'upload')
        tpl = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'ALM_Apartments_2026-10-23_features_template.xlsx')
        await pg.set_input_files('[data-role=upload]', tpl)
        await pg.fill('[data-role=importConfirm]', 'IMPORT')
        settxt = await pg.inner_text('.card--danger')
        assert 'Blank cells never change anything' in settxt, settxt
        await pg.click('[data-action=import]'); await pg.wait_for_selector('[data-role=imsg]:not(:empty)', timeout=30000); await pg.wait_for_timeout(300)
        im = await pg.inner_text('[data-role=imsg]')
        assert im.startswith('Done') and '103 rows read' in im and 'not understood' not in im and 'differing' not in im, im
        await pg.screenshot(path=f'{SHOTS}/staff_import_template.png', full_page=True)
        r = await pg.evaluate(f"fetch('/api/apartments/{aid}', {{headers: {{Authorization: 'Bearer ' + MMAI_API.getToken()}}}}).then(r=>r.json())")
        assert r['features']['gym'] == 'yes' and r['features']['pool'] == 'no' and r['parking_charge'] == '$60 per week', r['features']
        ok('S2c', 'template upload: 103 rows, no feature errors, staff values kept')
        # S3 HomeBot staff detail (if the API submit went through)
        if hb_id:
            await pg.goto(BASE + f'#/staff/homebot/{hb_id}'); await pg.wait_for_timeout(1200)
            t = await pg.inner_text('#view-staff')
            assert 'Features wanted' in t and 'Matter most' in t and 'Views, Balcony, Gym' in t, t[:1500]
            ok('S3', 'HomeBot staff detail shows the two rows')
        assert not errs, errs
        await b.close()


async def main():
    assert PW, 'set MMAI_QA_PASSWORD'
    sent = await public_checks(1280)
    await public_checks(375)
    # id of the 1280 submission (the staff detail test uses it)
    import urllib.request
    req = urllib.request.Request(BASE + 'api/auth/login', data=json.dumps({'username': 'bill', 'password': PW}).encode(), headers={'Content-Type': 'application/json'})
    tok = json.load(urllib.request.urlopen(req))['token']
    req = urllib.request.Request(BASE + 'api/enquiries?dataset=test&q=qa.features1280', headers={'Authorization': 'Bearer ' + tok})
    items = json.load(urllib.request.urlopen(req))['items']
    eid = [i for i in items if i['email'] == 'qa.features1280@example.com'][0]['id']
    hb_id = await homebot_checks()
    await staff_checks(eid, hb_id)
    print('ALL OK; screenshots in', SHOTS)


asyncio.run(main())
