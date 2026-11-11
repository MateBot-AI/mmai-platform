import asyncio, json, os
from playwright.async_api import async_playwright
exec(open(os.path.join(os.path.dirname(__file__), 'qa_staff_i19p.py')).read().split("async def run(")[0])
async def go():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await (await b.new_context(viewport={'width': 1280, 'height': 900})).new_page()
        errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
        posts=[]; pg.on('request', lambda r: posts.append(r.url) if r.method=='POST' else None)
        await pg.goto(BASE + '#/?test=1'); await pg.click('.btn--start'); await pg.wait_for_selector('.scr.is-active')
        F = "document.querySelector('.form-root').matebotForm"
        a=dict(GOOD); a.pop('is_test')
        await pg.evaluate(f"{F}.fill({json.dumps(a)})"); await pg.evaluate(f"{F}.go(999)")
        assert await pg.is_visible('.scr--review.is-active')
        n = await pg.locator('.review__change').count()
        # Change on move-in date -> 'Your stay' screen with move_in
        idx = await pg.evaluate("[...document.querySelectorAll('.review__row dt')].findIndex(d=>d.textContent.includes('move-in date'))")
        await pg.click(f'.review__change >> nth={idx}')
        assert await pg.evaluate("!!document.querySelector('.scr.is-active [name=move_in]')"), 'wrong screen'
        assert await pg.is_visible('[data-role=toReview]') and await pg.is_hidden('[data-role=next]')
        await pg.click('[data-role=toReview]'); assert await pg.is_visible('.scr--review.is-active')
        assert (await pg.inner_text('[data-role=next]')).strip()=='Submit' and await pg.is_hidden('[data-role=emailForm]') and await pg.locator('[data-role=emailBox]').count()==1
        await pg.evaluate("const b=document.querySelector('[data-role=next]'); b.click(); b.click();"); await pg.wait_for_selector('[data-role=done]:not([hidden])', timeout=10000)
        print('POSTs', posts); assert len([u for u in posts if 'enquiries' in u])==1, posts
        assert await pg.is_hidden('[data-role=emailFormDone]') and await pg.is_hidden('[data-role=emailBox]')
        print('public Change/return/Submit OK, change links:', n, 'errors', errs)
        await b.close()
asyncio.run(go())
