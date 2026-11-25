/* Hash router: #/ public form · #/enquire?t=TOKEN emailed form · #/mate?t=TOKEN "Bring a mate" form · #/resume?t=TOKEN saved form · #/respond?t=TOKEN group answer
   · #/staff[/view[/id]] dashboard. Add &test=1 to any public route to send a TEST enquiry (kept apart from real data). */
(() => {
  const api = window.MMAI_API;
  // Google Ads click id / campaign names (tracking.js) go with a public enquiry as one extra top-level `tracking` object, and only when
  // the visitor arrived from an ad. It is a copy, so the form's own answers are never changed. Staff phone entry (staff.js) never sends it.
  const withTracking = body => { const t = window.ALM_TRACKING; return t && Object.keys(t).length ? Object.assign({}, body, { tracking: Object.assign({}, t) }) : body; };
  let metaCache = null;
  async function meta(params) {
    const qs = new URLSearchParams(Object.entries(params || {}).filter(([, v]) => v)).toString();
    if (metaCache && !qs) return metaCache;
    const m = await api.get('/api/public/meta' + (qs ? '?' + qs : ''));
    if (!qs) metaCache = m;
    return m;
  }

  // I20(g): the public form may be embedded in the ALM website. The staff dashboard shares this page, so inside a frame it is
  // never rendered; the visitor gets a link that opens the staff sign-in in its own tab instead.
  let framed = false;
  try { framed = window.top !== window.self; } catch { framed = true; }
  if (framed) document.documentElement.classList.add('is-embedded');
  // Height reporting for the website's iframe: the parent page (WordPress snippet in DEPLOY_DIGITALOCEAN.md) listens for
  // {type:'matebot-height', height} and resizes the frame so there is no inner scrollbar. Plain data, any parent origin.
  function postHeight() {
    if (!framed) return;
    const h = Math.ceil(document.documentElement.scrollHeight);
    if (h && h !== postHeight.last) { postHeight.last = h; try { window.parent.postMessage({ type: 'matebot-height', height: h }, '*'); } catch { /* ignore */ } }
  }
  if (framed) {
    if (window.ResizeObserver) new ResizeObserver(() => postHeight()).observe(document.body);
    new MutationObserver(() => postHeight()).observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'class'] });
    window.addEventListener('load', postHeight);
    setInterval(postHeight, 1500);
  }

  async function route() {
    const hash = location.hash || '#/';
    const [path, query] = hash.slice(1).split('?');
    const parts = path.split('/').filter(Boolean);
    const q = new URLSearchParams(query || '');
    const pub = document.getElementById('view-public'), staff = document.getElementById('view-staff');
    const right = document.getElementById('topbarRight');
    const topbar = document.getElementById('topbar');
    try {
      if (parts[0] === 'staff') {
        pub.hidden = true; staff.hidden = false; topbar.hidden = false;
        if (framed) {
          right.innerHTML = '';
          const own = location.href;
          // target=_blank, not _top: embedding hosts without top-navigation permission block _top; a new tab on a real click works.
          document.getElementById('staffHost').innerHTML = `<div class="card"><p>The staff area does not open inside another website.</p><p><a class="btn btn--primary" href="${own.replace(/"/g, '&quot;')}" target="_blank" rel="noopener">Open the staff sign-in in a full tab</a></p></div>`;
          return;
        }
        if (!window.MMAI_STAFF.isLoggedIn()) right.innerHTML = '<a href="#/">Public form</a>';
        await window.MMAI_STAFF.mount(document.getElementById('staffHost'), await meta(), parts[1], parts[2]);
        return;
      }
      // I20: the public page has no staff top bar; it opens on the landing page and the form follows LET'S START
      staff.hidden = true; pub.hidden = false; topbar.hidden = true;
      const isTest = q.get('test') === '1';
      right.innerHTML = '<a href="#/staff">Staff sign in</a>';
      if (parts[0] === 'respond') {   // GP13: a person's own answer page for a proposed group; the token stays in the fragment
        const host = document.getElementById('publicFormHost'), landing = document.getElementById('landing');
        const key = `respond:${q.get('t') || ''}`;
        if (host.dataset.mounted === key) return;
        host.dataset.mounted = key; landing.hidden = true; host.hidden = false;
        await window.MMAI_RESPOND.mount(host, q.get('t') || '');
        return;
      }
      const emailToken = parts[0] === 'enquire' ? q.get('t') : null;
      const mateToken = parts[0] === 'mate' ? q.get('t') : null;
      const draftToken = parts[0] === 'resume' ? q.get('t') : null;
      const host = document.getElementById('publicFormHost');
      const landing = document.getElementById('landing');
      const key = `${parts[0] || 'public'}:${emailToken || mateToken || draftToken || ''}:${isTest ? 'test' : ''}`;
      if (host.dataset.mounted === key) return;
      host.dataset.mounted = key;
      // I20(a)/(b): landing page first (website visit, emailed link, Bring a mate link). A saved form (#/resume) has already
      // been started, so it reopens straight on the saved screen (build reading, noted for Bill).
      const showLanding = !draftToken;
      landing.hidden = !showLanding; host.hidden = showLanding;
      host.innerHTML = '<p class="loading">Loading the form…</p>';
      // CR6: LET'S START must work from the first click, even before the metadata has arrived (a cold load in the website
      // frame). The early handler shows the "Loading the form…" host at once; once the form is mounted below, the real
      // reveal (scroll + focus) runs, immediately if the visitor already clicked. Nothing else about I20 changes.
      let startedEarly = false;
      const start = showLanding ? document.getElementById('startBtn') : null;
      if (start) start.onclick = () => { startedEarly = true; landing.hidden = true; host.hidden = false; };
      let draft = null, resumeError = null;
      if (draftToken) {
        try { draft = await api.get('/api/public/drafts/' + encodeURIComponent(draftToken)); }
        catch (e) { resumeError = e.status === 410 ? 'That saved form has expired, so here is a fresh one.' : 'That saved form was not found (it may have been sent already), so here is a fresh one.'; }
      }
      const effEmail = emailToken || (draft && draft.email_token) || null;
      const effMate = mateToken || (draft && draft.mate_token) || null;
      const m = await meta({ email_token: effEmail, mate_token: effMate });
      // a friend of a TEST enquirer is TEST data too (the server enforces this; the badge just says so)
      const effTest = isTest || !!(draft && draft.is_test) || !!(m.mate_link && m.mate_link.valid && m.mate_link.is_test);
      document.getElementById('topbarTest').hidden = !effTest;
      document.getElementById('publicTest').hidden = !effTest;
      // an unrecognised or full share link is explained in the banner; the enquiry is then sent as a normal one
      const usableMate = m.mate_link && m.mate_link.valid && m.mate_link.room_left > 0 ? effMate : null;
      const qs = new URLSearchParams(Object.entries({ email_token: effEmail, mate_token: usableMate }).filter(([, v]) => v));
      window.mountForm(host, {
        mode: 'public', meta: m, emailToken: effEmail, emailLink: m.email_link, mateToken: effMate, mateLink: m.mate_link, isTest: effTest, draft,
        storageKey: key,  // where the form keeps its opaque submission key while an attempt's outcome is uncertain (no answers are stored)
        onSubmit: (answers, o) => { if (effTest) answers.is_test = true; const p = new URLSearchParams(qs); if (draft && draft.token) p.set('draft_token', draft.token); const s = p.toString(); return api.post('/api/public/enquiries' + (s ? '?' + s : ''), withTracking(answers), { headers: { 'X-Submission-Key': o.key } }); },
        onSaveDraft: (answers, step, token, o) => {
          const body = { answers, step, email: answers.email || '', is_test: effTest, mate_token: effMate, email_token: effEmail, submit_key: o && o.key };
          return token ? api.put('/api/public/drafts/' + encodeURIComponent(token), body) : api.post('/api/public/drafts', body);
        },
      });
      if (resumeError) { const b = host.querySelector('[data-role="banner"]'); if (b) { b.hidden = false; b.textContent = resumeError; } }
      if (showLanding) {
        const reveal = () => {
          landing.hidden = true; host.hidden = false;
          window.scrollTo({ top: 0, behavior: 'auto' });
          const first = host.querySelector('.scr.is-active .field input, .scr.is-active .field select, .scr.is-active .field textarea');
          if (first && window.matchMedia('(min-width: 760px)').matches) first.focus({ preventScroll: true });
        };
        start.onclick = reveal;
        if (startedEarly) reveal();
      }
    } catch (e) {
      const host = parts[0] === 'staff' ? document.getElementById('staffHost') : document.getElementById('publicFormHost');
      if (parts[0] !== 'staff') { host.hidden = false; document.getElementById('landing').hidden = true; }
      host.innerHTML = '<div class="card"><p class="msg err"></p></div>';
      host.querySelector('.msg').textContent = `The MateBot AI service is not reachable right now (${e.message}). Please try again shortly.`;
      if (parts[0] !== 'staff') host.dataset.mounted = '';
    }
  }
  window.addEventListener('hashchange', route);
  route();
})();
