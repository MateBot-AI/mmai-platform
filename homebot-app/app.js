/* HomeBot router. Hash routes (deep-linkable):
     #/              chooser: MateBot or HomeBot (H1/H2) — the MateBot card is an ordinary link to the working MateBot form
     #/homebot       HomeBot landing (H2)
     #/homebot/form  HomeBot questionnaire (saves to the shared MMAI backend; see form.js)
     #/matebot       (old leaving-notice route) just shows the chooser; there is no leaving dialog any more (H2, 6 Nov 2026)
   Flags, read once at load:
     ?test=1 anywhere in the hash query (T14) → sticky test mode for this page: the TEST strip shows, every in-page link keeps
       test=1 and the saved enquiry is marked is_test (kept apart from real enquiries, wipeable by staff).
     <meta name="homebot-review-build" content="on"> → REVIEW BUILD strip, review-workspace wording on the done screen;
       HomeBot submissions and the MateBot handoff are always TEST. Query-string test=1 also works outside review mode.
       In production mode ("off") MateBot opens in test mode only when this page itself is in test mode.
   The MateBot links (chooser card and hand-off screen, data-role=matebotLink) point at <meta name="matebot-url"> and navigate
   this same page/frame (target=_self; CR6, 6 Nov 2026, H2/I20(g)): a new tab is silently refused inside a sandboxed frame without
   allow-popups, and ALM posts the combined choice page, so the choice must work in place. The URL may be absolute (separate MateBot
   site) or relative (MateBot bundled next to this page, e.g. matebot/index.html#/). H5 (9 Nov 2026): the hand-off screen saves
   the party's size answers as a MateBot saved form through the shared backend (form.js prepareHandoff) and points its link at
   MateBot's #/resume so they are prefilled for review; if that fails the link stays the plain MateBot form and the notice says
   nothing was carried. No other HomeBot answer exists at that point and none is passed. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const meta = (n) => { const m = document.querySelector(`meta[name="${n}"]`); return m ? (m.getAttribute('content') || '').trim() : ''; };
  const REVIEW = (meta('homebot-review-build') || 'on').toLowerCase() !== 'off';
  const MATEBOT_URL = meta('matebot-url') || '/#/';
  const TEST = REVIEW || /(^|[?&])test=1(&|$)/.test((location.hash.split('?')[1] || '')) || /(^|[?&])test=1(&|$)/.test(location.search);
  const views = Array.from(document.querySelectorAll('[data-view]'));
  let formApi = null;

  // Working MateBot form (configured host; same origin in the self-hosted review package). ?test=1 keeps that enquiry apart from real data (I19(a)/T14).
  const openMateBotAsTest = REVIEW || TEST;
  const matebotHref = openMateBotAsTest ? MATEBOT_URL + (MATEBOT_URL.includes('?') ? '&' : '?') + 'test=1' : MATEBOT_URL;
  document.querySelectorAll('[data-role=matebotLink]').forEach(a => { a.href = matebotHref; a.target = '_self'; a.removeAttribute('rel'); });
  document.querySelectorAll('[data-role=matebotTestHint]').forEach(el => { el.hidden = !openMateBotAsTest; });

  const rb = $('[data-role=reviewBadge]'); if (rb) rb.hidden = !REVIEW;
  const tb = $('[data-role=testBadge]'); if (tb) tb.hidden = !TEST;
  document.querySelectorAll('[data-role=doneReviewNote]').forEach(el => { el.hidden = !REVIEW; });
  // keep test mode sticky across in-page navigation
  const withTest = (hash) => { if (!TEST) return hash; if (/(^|[?&])test=1(&|$)/.test(hash.split('?')[1] || '')) return hash; return hash + (hash.includes('?') ? '&' : '?') + 'test=1'; };
  if (TEST) document.querySelectorAll('a[href^="#/"]').forEach(a => { a.setAttribute('href', withTest(a.getAttribute('href'))); });

  function show(name) {
    views.forEach(v => { v.hidden = v.dataset.view !== name; });
    window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
    const base = name === 'chooser' ? 'Apartment Living Melbourne · MateBot and HomeBot' : 'Apartment Living Melbourne · HomeBot';
    document.title = base + (REVIEW ? ' — review build' : '') + (TEST ? ' — test' : '');
  }
  function mountForm() {
    if (formApi) return;
    // The hand-off saved form is written to the SAME dataset the MateBot link opens (test in the review build or test mode), so the
    // link, the hint and the saved form never disagree.
    formApi = window.mountHomeBotForm($('[data-role=formRoot]'), { isTest: () => TEST, handoffIsTest: () => openMateBotAsTest });
  }

  function route() {
    const h = location.hash || '#/';
    const path = h.replace(/^#\/?/, '').split('?')[0].replace(/\/+$/, '');
    if (path === 'matebot') { location.hash = withTest('#/'); return; }
    if (path === 'homebot') { show('homebot'); return; }
    if (path === 'homebot/form') {
      show('form'); mountForm();
      // intro-page gate: #/homebot/form?need=complete|more preselects the first question and moves to the next screen
      const q = new URLSearchParams(h.split('?')[1] || '');
      const need = q.get('need');
      if (need === 'complete' || need === 'more') { formApi.preset('need_more', need); history.replaceState(null, '', withTest('#/homebot/form')); }
      return;
    }
    show('chooser');
  }

  window.addEventListener('hashchange', route);
  route();
})();
