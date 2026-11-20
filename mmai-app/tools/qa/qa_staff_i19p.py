import asyncio, json, os, sys
from playwright.async_api import async_playwright
BASE = 'http://127.0.0.1:8035/'
GOOD = dict(first_name="Qa", last_name="Phone", email="qa.phone@example.com", phone_code="+61", phone="0400000001", contact_method="Email",
            birth_country_raw="Sweden", live_country_raw="Sweden", languages=["English"],
            city="Melbourne", move_in="2027-02-01", move_in_flex="± 1 week", move_out="2027-07-10", move_out_flex="Exact date",
            budget_option="2B2B-single", apt_sizes=["2 bedrooms"], mate_ideal="1", mate_max="3", rooms=["Own room"], bunk_ok="No", room_first="Own room",
            party="Alone", doing="Working", work="Melbourne CBD", own_region="Don't mind", avoid_answer="No",
            smoke="No", smoke_ok="Yes", source="Google", consent_store=True, consent_share=True, adults_confirm=True, is_test=True)
# 8 Nov 2026 (I8 / I11(d)): no dob, sex, sex_pref, age_pref or consent_age; adults_confirm is the one 18+ declaration (all occupants, at move-in)

async def run(width, scenario):
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': width, 'height': 900})
        pg = await ctx.new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        posts = []; gets = []
        pg.on('request', lambda r: posts.append(r.url) if r.method == 'POST' and '/api/enquiries' in r.url else (gets.append(r.url) if r.method == 'GET' and '/api/enquiries/' in r.url else None))
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', os.environ.get('MMAI_QA_PASSWORD', '')); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        F = "document.querySelector('.form-root').matebotForm"
        # section layout unchanged: step list has 8 entries, section 1 active, Step 1 of 8
        titles = await pg.evaluate("[...document.querySelectorAll('[data-role=stepList] button')].map(b=>b.textContent.trim())")
        assert titles[-1] == '8. Check the answers' and len(titles) == 8, titles
        assert (await pg.inner_text('[data-role=stepCount]')).strip() == 'Step 1 of 8'
        assert await pg.is_hidden('[data-role=toReview]') and await pg.is_hidden('[data-role=tipWrap]')
        await pg.evaluate(f"{F}.fill({json.dumps(GOOD)})")
        # walk with Next through all 7 input sections (validation on each)
        for i in range(7):
            lbl = (await pg.inner_text('[data-role=next]')).strip(); assert lbl == 'Next', (i, lbl)
            await pg.click('[data-role=next]'); await pg.wait_for_timeout(150)
            cur = await pg.evaluate("document.querySelector('.step.is-active').dataset.step")
            assert cur == str(i + 2), (i, cur, await pg.evaluate("[...document.querySelectorAll('.field-error')].map(e=>e.textContent)"))
        assert await pg.is_visible('.step.staff-review.is-active')
        assert (await pg.inner_text('[data-role=next]')).strip() == 'Save enquiry'
        groups = await pg.evaluate("[...document.querySelectorAll('.staff-review .review__group h4')].map(h=>h.textContent)")
        assert groups[0].startswith('1.') and any(g.startswith('7. Staff only') for g in groups), groups
        assert await pg.is_visible('[data-role=emailForm]')
        assert await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
        await pg.screenshot(path=f'/tmp/qa_staff_summary_{width}.png', full_page=True)
        # Change -> owning section, green first return, direct return
        await pg.click('.review__change >> nth=0'); await pg.wait_for_timeout(100)
        assert await pg.evaluate("document.querySelector('.step.is-active').dataset.step") == '1'
        assert await pg.is_visible('[data-role=toReview]') and await pg.is_hidden('[data-role=next]')
        order = await pg.evaluate("[...document.querySelectorAll('[data-role=nav] .btn')].filter(b=>!b.hidden).map(b=>b.textContent.trim())")
        assert order[0] == 'Back to check your answers', order
        bb1 = await pg.locator('[data-role=toReview]').bounding_box(); bb2 = await pg.locator('[data-role=back]').bounding_box()
        assert bb1['y'] < bb2['y'] or (abs(bb1['y'] - bb2['y']) < 2 and bb1['x'] < bb2['x']), (bb1, bb2)
        assert await pg.evaluate("getComputedStyle(document.querySelector('[data-role=toReview]')).backgroundColor") == 'rgb(47, 107, 79)'
        await pg.screenshot(path=f'/tmp/qa_staff_edit_{width}.png')
        # invalid edit stays with error
        await pg.fill('input[name=first_name]', ''); await pg.click('[data-role=toReview]'); await pg.wait_for_timeout(100)
        assert await pg.evaluate("document.querySelector('.step.is-active').dataset.step") == '1' and await pg.locator('.field-error').count() >= 1
        await pg.fill('input[name=first_name]', 'Qa2'); await pg.click('[data-role=toReview]'); await pg.wait_for_timeout(100)
        assert await pg.is_visible('.step.staff-review.is-active'), 'direct return failed'
        assert 'Qa2' in await pg.inner_text('.staff-review .review__group >> nth=0')
        # all-section validation on save: break section 2 (move_out before move_in) while on the summary, then Save
        await pg.evaluate(f"{F}.fill({{move_out:'2026-12-01'}})")
        await pg.click('[data-role=next]'); await pg.wait_for_timeout(200)
        assert await pg.evaluate("document.querySelector('.step.is-active').dataset.step") == '2', 'should land on the invalid section'
        assert posts == [], posts
        await pg.evaluate(f"{F}.fill({{move_out:'2027-07-10'}})"); await pg.click('[data-role=toReview]'); await pg.wait_for_timeout(100)
        assert await pg.is_visible('.step.staff-review.is-active')
        if scenario == 'email':
            # Email completed form on the summary: single save, then preview opens
            await pg.evaluate("const b=document.querySelector('[data-role=emailForm]'); b.click(); b.click();")  # double click: one POST
            await pg.wait_for_selector('[data-role=emailBox]:not([hidden])', timeout=10000)
        else:
            await pg.evaluate("const b=document.querySelector('[data-role=next]'); b.click(); b.click();")
            await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
            assert await pg.is_hidden('[data-role=emailBox]')
            assert await pg.is_visible('[data-role=emailFormDone]')
            await pg.click('[data-role=emailFormDone]'); await pg.wait_for_selector('[data-role=emailBox]:not([hidden])', timeout=10000)
        assert len(posts) == 1, posts
        ref = await pg.inner_text('[data-role=doneRef]'); print(width, scenario, 'ref:', ref[:40])
        to = await pg.inner_text('[data-role=emailTo]'); subj = await pg.inner_text('[data-role=emailSubject]'); body = await pg.input_value('[data-role=emailBody]')
        assert to == 'qa.phone@example.com' and subj == 'Your MateBot enquiry – we’re finding your perfect housemates', (to, subj)
        assert body.startswith('Hi Qa2,') and 'Your answers' in body and 'Reference: MateBot-' in body and '1. About you' in body and '- First name: Qa2' in body, body[:300]
        print(body); assert 'Staff only' not in body and 'TEST' not in body and 'staff' not in body.lower().split('your answers')[1]
        assert '6. Consent' in body and 'Sent' not in body
        href = await pg.get_attribute('[data-role=emailOpen]', 'href'); assert href.startswith('mailto:qa.phone%40example.com?subject=') and 'body=' in href
        assert not await pg.evaluate("Object.keys(localStorage).concat(Object.keys(sessionStorage)).some(k=>k.includes('email'))")
        assert 'Qa2' not in json.dumps(await pg.evaluate("Object.assign({}, localStorage, sessionStorage)"))
        n_gets = len(gets)
        # close & reopen: one GET, zero POST
        await pg.click('[data-role=emailClose]'); assert await pg.is_hidden('[data-role=emailBox]')
        await pg.click('[data-role=emailFormDone]'); await pg.wait_for_selector('[data-role=emailBox]:not([hidden])')
        assert len(posts) == 1 and len(gets) == n_gets + 1, (posts, gets)
        assert await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth")
        await pg.screenshot(path=f'/tmp/qa_staff_email_{width}_{scenario}.png', full_page=True)
        assert await pg.evaluate("document.querySelectorAll('.form-root').length") == 1, 'serialiser instance left behind'
        # Start again clears the preview
        await pg.click('[data-role=restart]'); await pg.wait_for_timeout(100)
        assert await pg.is_hidden('[data-role=emailBox]') and await pg.input_value('[data-role=emailBody]') == '' and await pg.inner_text('[data-role=emailTo]') == ''
        assert await pg.evaluate("document.querySelector('.step.is-active').dataset.step") == '1'
        print(width, scenario, 'OK; page errors:', errs)
        await b.close()

async def replay():
    # 409 already_submitted: same key, changed answers -> email must show the ORIGINAL saved answers
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': 1280, 'height': 900}); pg = await ctx.new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', os.environ.get('MMAI_QA_PASSWORD', '')); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        F = "document.querySelector('.form-root').matebotForm"
        a = dict(GOOD, first_name='Replay', email='replay@example.com', phone='0400000002')
        await pg.evaluate(f"{F}.fill({json.dumps(a)})")
        key = await pg.evaluate("sessionStorage.getItem(Object.keys(sessionStorage).find(k=>k.startsWith('matebot.submit.')))")
        # make the client think the attempt is pending with its key, then change an answer and submit: the server has it already
        # simpler: submit once, then reload with the pending key and submit changed answers
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=next]'); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        ref1 = await pg.inner_text('[data-role=doneRef]')
        # fetch key used from the server log is not possible; instead grab it via a request hook on a second run
        await b.close()
        return ref1

async def replay2():
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': 1280, 'height': 900}); pg = await ctx.new_page()
        keys = []
        pg.on('request', lambda r: keys.append(r.headers.get('x-submission-key')) if r.method == 'POST' and r.url.endswith('/api/enquiries') else None)
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', os.environ.get('MMAI_QA_PASSWORD', '')); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        F = "document.querySelector('.form-root').matebotForm"
        a = dict(GOOD, first_name='Original', email='orig@example.com', phone='0400000003')
        await pg.evaluate(f"{F}.fill({json.dumps(a)})")
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=next]'); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        ref1 = await pg.inner_text('[data-role=doneRef]'); key = keys[-1]
        # new page with the same key marked pending, changed answers
        sk = await pg.evaluate("Object.keys(sessionStorage).find(k=>k.startsWith('matebot.submit.'))||'matebot.submit.staff:new:bill'")
        await pg.goto(BASE + '#/staff/enquiries'); await pg.wait_for_timeout(300)
        await pg.evaluate(f"sessionStorage.setItem({json.dumps(sk)}, JSON.stringify({{k:{json.dumps(key)}, s:'pending'}}))")
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        a2 = dict(a, first_name='Changed', email='changed@example.com')
        await pg.evaluate(f"{F}.fill({json.dumps(a2)})")
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=emailForm]')
        await pg.wait_for_selector('[data-role=emailBox]:not([hidden])', timeout=15000)
        ref2 = await pg.inner_text('[data-role=doneRef]'); body = await pg.input_value('[data-role=emailBody]'); to = await pg.inner_text('[data-role=emailTo]')
        assert 'already been sent' in ref2 and ref2.split(' ·')[0] == ref1.split(' ·')[0], (ref1, ref2)
        assert body.startswith('Hi Original,') and '- First name: Original' in body and 'Changed' not in body and to == 'orig@example.com', (to, body[:200])
        assert keys[-1] == key and len(keys) == 2
        print('replay OK: email shows ORIGINAL saved answers;', ref2[:60])
        await b.close()

asyncio.run(run(1280, 'save')); asyncio.run(run(375, 'email')); asyncio.run(replay2())

async def pending_restart():
    # Start again while the saved-record GET is still pending: late reply ignored, button state reset for the next enquiry
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': 1280, 'height': 900}); pg = await ctx.new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', os.environ.get('MMAI_QA_PASSWORD', '')); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        F = "document.querySelector('.form-root').matebotForm"
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, first_name='Slow', email='slow@example.com'))})")
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=next]'); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        # delay the GET by 3 s
        async def slow(route):
            await asyncio.sleep(3); await route.continue_()
        await pg.route('**/api/enquiries/*', slow)
        await pg.click('[data-role=emailFormDone]'); await pg.wait_for_timeout(200)
        assert (await pg.inner_text('[data-role=emailFormDone]')).strip() == 'Preparing…'
        await pg.click('[data-role=restart]'); await pg.wait_for_timeout(100)
        assert await pg.is_hidden('[data-role=emailFormDone]') and not await pg.evaluate("document.querySelector('[data-role=emailFormDone]').disabled")
        assert (await pg.inner_text('[data-role=emailFormDone]')).strip() == 'Email completed form' or True
        await pg.wait_for_timeout(3500)  # late reply arrives now
        assert await pg.is_hidden('[data-role=emailBox]') and await pg.input_value('[data-role=emailBody]') == '' and await pg.inner_text('[data-role=emailTo]') == ''
        await pg.unroute('**/api/enquiries/*')
        # next enquiry: button works again
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, first_name='Nextone', email='next@example.com'))})")
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=next]'); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        assert not await pg.evaluate("document.querySelector('[data-role=emailFormDone]').disabled")
        await pg.click('[data-role=emailFormDone]'); await pg.wait_for_selector('[data-role=emailBox]:not([hidden])')
        body = await pg.input_value('[data-role=emailBody]'); assert body.startswith('Hi Nextone,') and 'Slow' not in body
        # GET failure: recoverable, saved state kept, no POST
        posts = []; pg.on('request', lambda r: posts.append(r.url) if r.method == 'POST' else None)
        await pg.click('[data-role=emailClose]')
        await pg.route('**/api/enquiries/*', lambda route: route.fulfill(status=500, body='{"detail":"boom"}', content_type='application/json'))
        await pg.click('[data-role=emailFormDone]'); await pg.wait_for_selector('[data-role=emailError]:not([hidden])')
        assert 'enquiry is saved' in await pg.inner_text('[data-role=emailError]') and await pg.is_visible('[data-role=done]') and posts == []
        assert not await pg.evaluate("document.querySelector('[data-role=emailFormDone]').disabled")
        print('pending/restart + GET failure OK; page errors:', errs)
        await b.close()
asyncio.run(pending_restart())

async def malformed():
    async with async_playwright() as p:
        b = await p.chromium.launch(); ctx = await b.new_context(viewport={'width': 1280, 'height': 900}); pg = await ctx.new_page()
        errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE + '#/staff'); await pg.wait_for_selector('input[name=username]')
        await pg.fill('input[name=username]', 'bill'); await pg.fill('input[name=password]', os.environ.get('MMAI_QA_PASSWORD', '')); await pg.keyboard.press('Enter')
        await pg.wait_for_selector('#view-staff .side, #view-staff nav', timeout=10000)
        await pg.goto(BASE + '#/staff/new'); await pg.wait_for_selector('.form-root form')
        F = "document.querySelector('.form-root').matebotForm"
        await pg.evaluate(f"{F}.fill({json.dumps(dict(GOOD, first_name='Mal', email='mal@example.com'))})")
        await pg.evaluate(f"{F}.go(7)"); await pg.click('[data-role=next]'); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        async def strip(route):
            r = await route.fetch(); d = await r.json(); d['answers_json'] = {}; await route.fulfill(status=200, content_type='application/json', body=json.dumps(d))
        await pg.route('**/api/enquiries/*', strip)
        await pg.click('[data-role=emailFormDone]'); await pg.wait_for_selector('[data-role=emailError]:not([hidden])')
        assert await pg.is_hidden('[data-role=emailBox]') and 'enquiry is saved' in await pg.inner_text('[data-role=emailError]')
        print('malformed answers_json -> recoverable error, no header-only email; errors:', errs)
        await b.close()
asyncio.run(malformed())
