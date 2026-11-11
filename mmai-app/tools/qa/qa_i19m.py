import asyncio, json, os
from playwright.async_api import async_playwright
BASE='http://127.0.0.1:8035/#/?test=1'
async def run(width):
    async with async_playwright() as p:
        b=await p.chromium.launch(); pg=await b.new_page(viewport={'width':width,'height':900})
        errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
        await pg.goto(BASE); await pg.wait_for_selector('.btn--start')
        txt=await pg.inner_text('.landing__copy'); assert 'It takes a couple of minutes.' in txt, txt[-80:]
        assert 'about 5 minutes' not in txt
        await pg.click('.btn--start'); await pg.wait_for_selector('.scr.is-active')
        nav=pg.locator('[data-role=nav]')
        assert await pg.is_hidden('[data-role=toReview]')
        # fill name on first screen and advance to check Next label unchanged
        assert (await pg.inner_text('[data-role=next]')).strip()=='Next'
        # jump to the review via QA hook
        total=await pg.evaluate("document.querySelectorAll('.form-root .scr').length")
        await pg.evaluate("document.querySelector('.form-root').matebotForm.go(999)")
        assert await pg.is_visible('.scr--review.is-active')
        nextTxt=(await pg.inner_text('[data-role=next]')).strip(); assert nextTxt=='Submit', nextTxt
        assert await pg.is_hidden('[data-role=toReview]')
        intro=await pg.inner_text('.review__intro'); assert 'Back to check your answers' in intro and 'Submit.' in intro and 'Submit my enquiry' not in intro
        # Press Back from review -> edit mode
        await pg.click('[data-role=back]')
        assert await pg.is_visible('[data-role=toReview]'), 'toReview not visible after back'
        assert await pg.is_hidden('[data-role=next]'), 'next still visible'
        # DOM order: toReview first visible button in nav
        order=await pg.evaluate("[...document.querySelectorAll('[data-role=nav] .btn')].filter(b=>!b.hidden).map(b=>b.textContent.trim())")
        assert order[0]=='Back to check your answers', order
        # visual order: bounding boxes
        bb1=await pg.locator('[data-role=toReview]').bounding_box(); bb2=await pg.locator('[data-role=back]').bounding_box()
        assert bb1['y']<bb2['y'] or (abs(bb1['y']-bb2['y'])<2 and bb1['x']<bb2['x']), (bb1,bb2)
        bg=await pg.evaluate("getComputedStyle(document.querySelector('[data-role=toReview]')).backgroundColor")
        assert bg=='rgb(47, 107, 79)', bg
        sw=await pg.evaluate("document.documentElement.scrollWidth<=window.innerWidth"); assert sw
        await pg.screenshot(path=f'/tmp/qa_i19m_edit_{width}.png', full_page=False)
        # click -> back to review directly (consent screen validation: ticks required -> should stay with error)
        await pg.click('[data-role=toReview]')
        onReview=await pg.is_visible('.scr--review.is-active')
        print(width,'consent return ok?',onReview, 'errors shown:', await pg.locator('.scr.is-active .err:visible, .scr.is-active .field-error:visible').count())
        # go to a screen with no required validation issue: first screen with name filled
        await pg.evaluate("document.querySelector('.form-root').matebotForm.go(999)")
        await pg.evaluate("document.querySelector('.form-root').matebotForm.go(0)")
        await pg.fill('input[name=first_name]','Qa')
        if await pg.locator('input[name=last_name]').count(): await pg.fill('input[name=last_name]','Test')
        assert await pg.is_visible('[data-role=toReview]')
        await pg.click('[data-role=toReview]')
        if not await pg.is_visible('.scr--review.is-active'):
            print('stayed on screen 0; visible errors:', await pg.evaluate("[...document.querySelectorAll('.scr.is-active .msg.err, .scr.is-active [class*=err]')].filter(e=>e.offsetParent).map(e=>e.textContent.trim()).slice(0,3)"))
        else: print(width,'direct return to summary OK')
        assert (await pg.inner_text('[data-role=next]')).strip()=='Submit'
        await pg.screenshot(path=f'/tmp/qa_i19m_review_{width}.png')
        print(width,'page errors',errs); await b.close()
asyncio.run(run(1280)); asyncio.run(run(375))
