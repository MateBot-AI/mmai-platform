/* Staff dashboard (login required). All personal data lives behind the Bearer token (brief 2.3). */
(() => {
  const api = window.MMAI_API;
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // Introduction-only sex / current age (Bill, 8 Nov 2026): shown to staff and used in introductions only; never a matching input.
  const INTRO_SEX_OPTIONS = ['Female', 'Male', 'Another term', 'Prefer not to say'];
  const introText = r => { const bits = []; if (r.intro_sex === 'Another term') bits.push(r.intro_sex_other || 'Another term'); else if (r.intro_sex) bits.push(r.intro_sex); if (r.intro_age !== null && r.intro_age !== undefined && r.intro_age !== '') bits.push(String(r.intro_age)); return bits.length ? bits.join(', ') : 'Not given'; };
  const introEdit = (prefix, r) => `<div class="field"><label>Sex<select name="${prefix}intro_sex"><option value="" ${!r.intro_sex ? 'selected' : ''}>Not given</option>${INTRO_SEX_OPTIONS.map(o => `<option ${r.intro_sex === o ? 'selected' : ''}>${o}</option>`).join('')}</select></label></div>
      <div class="field"><label>Term they use (only with “Another term”)<input name="${prefix}intro_sex_other" maxlength="40" value="${esc(r.intro_sex_other || '')}"></label></div>
      <div class="field"><label>Current age<input name="${prefix}intro_age" type="number" min="0" max="999" step="1" value="${r.intro_age === null || r.intro_age === undefined ? '' : esc(String(r.intro_age))}"></label></div>`;
  const introPatch = (root, prefix) => { const v = k => root.querySelector(`[name="${prefix}${k}"]`).value; const sex = v('intro_sex'); return { intro_sex: sex || null, intro_sex_other: sex === 'Another term' ? v('intro_sex_other') : null, intro_age: v('intro_age').trim() === '' ? null : v('intro_age').trim() }; };
  const fmtD = s => s ? new Date(s.slice(0, 10) + 'T00:00:00').toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' }) : '—';
  const fmtDT = s => s ? new Date(s).toLocaleString('en-AU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
  const money = n => n == null ? '—' : '$' + Number(n).toFixed(0);
  // The queue's last_error is a fixed category from the server (never provider prose). Staff see one of these plain sentences
  // and never the category code or an unknown raw string: anything unrecognised gets the generic line. (T10 / STATE fix, 8 Nov 2026)
  const AI_ERROR_TEXT = {
    PROVIDER_CREDENTIALS: 'The AI service connection or set-up needs attention before assessments can run. Retrying will not help until it is fixed; tell the developer.',
    PROVIDER_ERROR: 'The last AI review did not complete. Check the affected groups on this page before retrying.',
    UNEXPECTED: 'The last AI batch stopped because of an unexpected problem inside MateBot AI. Retry the failed groups; if it happens again, tell the developer.',
    QUEUE_WORKER: 'The background AI worker hit a problem on its last pass and is continuing. If it keeps appearing, tell the developer.'
  };
  const aiErrorText = code => AI_ERROR_TEXT[String(code ?? '').split(':')[0].trim().toUpperCase()] || 'The last AI run reported a problem. Retry the failed groups; if it keeps appearing, tell the developer.';
  const badgeClass = st => ({ 'New': 'new', 'Proposed in group': 'proposed', 'Proposed': 'proposed', 'Introduced': 'introduced', 'Group confirmed': 'confirmed', 'Leased': 'leased', 'Withdrawn': 'withdrawn', 'Dissolved': 'dissolved', 'Awaiting replacement': 'awaiting' }[st] || '');
  const badge = (st, extra = '') => `<span class="badge badge--${badgeClass(st)} ${extra}">${esc(st)}</span>`;
  const noCurrentApartment = g => g && g.apartment_id == null && ['Proposed', 'Introduced', 'Group confirmed'].includes(g.status);
  const stateBadge = g => !g || ['Leased', 'Dissolved'].includes(g.status) || (g.status === 'Awaiting replacement' && g.ai_status !== 'failed') ? '' : noCurrentApartment(g) ? '<span class="badge badge--warn">Held: no current apartment</span>' : g.review_state === 'Ready for reservations' ? '<span class="badge badge--alt" data-role="ready-badge" title="The AI review approved this group under the current rules, the staff brief is current and nobody is in another circulating group. Travel to campus or work is checked last, on the group page, and is not saved: until that check passes, the apartment offered is provisional">Ready for reservations · travel not confirmed</span>' : g.review_state === 'AI assessed; not ready' ? `<span class="badge badge--flag" title="${esc(((g.circulation || {}).reasons || []).join('; '))}">Assessed, not ready</span>`
    : g.review_state === 'AI assessment failed' ? `<span class="badge badge--warn" title="${esc(g.ai_error || 'The AI call failed')}">AI assessment failed · retry</span>`
    : g.review_state === 'AI assessment held' ? `<span class="badge badge--warn" title="${esc((g.hold && g.hold.label) || 'The AI could not read a member\'s notes after the automatic retries')}">AI assessment held · retry</span>` : '<span class="badge badge--pending" title="The AI is still assessing this group and ranking its apartments; it is not ready for reservations yet">AI assessment pending</span>';
  const barredBadge = e => e && e.barred_at ? `<span class="badge badge--warn" title="${e.barred_person ? esc(e.barred_person) + ' is barred from matching by staff; the rest of the form left with them' : 'Barred from matching by staff'}${e.barred_reason ? ': ' + esc(e.barred_reason) : ''}">${e.barred_person ? 'barred: ' + esc(e.barred_person) : 'barred'}</span>` : '';
  // 6.4 / I11(d): whether the whole group has been declared 18+ for the move-in date. The server works the status out
  // (confirmed / renew / missing); the UI only says it in plain words (T10) and never infers it from anything else.
  const adultsBy = e => e.adults_confirmed_by === 'enquirer' ? 'the enquirer on the form' : e.adults_confirmed_by ? `staff (${esc(String(e.adults_confirmed_by).replace(/^staff:?/, '') || 'name not recorded')})` : 'not recorded';
  const adultsText = e => {
    if (e.adults_status === 'confirmed') return `Confirmed by ${adultsBy(e)} on ${fmtDT(e.adults_confirmed_at)} for move-in ${fmtD(e.adults_confirmed_move_in)}.`;
    if (e.adults_status === 'renew') return `A fresh confirmation is needed. The last one (by ${adultsBy(e)}, for move-in ${fmtD(e.adults_confirmed_move_in)}) no longer matches this enquiry: the move-in date is now earlier, or the enquirer or the people in the group have changed. Check with the enquirer, then record it here.`;
    return 'Not yet declared: this enquiry was saved before the question existed. Check with the enquirer that everyone in the group, including them, will be 18 or older on the move-in date, then record it here.';
  };
  const adultsBadge = e => e && e.adults_status && e.adults_status !== 'confirmed' ? `<span class="badge badge--warn" title="${e.adults_status === 'renew' ? 'The 18+ at move-in confirmation needs renewing (move-in earlier or group changed)' : 'Nobody has yet confirmed that everyone in the group will be 18 or older at move-in'}">18+ to confirm</span>` : '';

  const state = { user: null, meta: null, ref: null, dataset: 'real', view: 'overview', param: null, host: null, cache: {} };
  // public address for links given to enquirers: MMAI_PUBLIC_BASE_URL from the server when set, else this page's address
  const publicBase = () => (state.meta && state.meta.public_base_url) ? state.meta.public_base_url + '/' : location.href.split('#')[0];

  // ---------------- Shell ----------------
  // Simple navigation: five tabs. Sub-screens (phone enquiry, emailed form, matching detail) live under their tab.
  const NAV_OF = { overview: 'overview', enquiries: 'enquiries', new: 'enquiries', email: 'enquiries', homebot: 'enquiries', groups: 'groups', matching: 'groups', outcomes: 'groups', apartments: 'apartments', data: 'data', password: 'data' };
  const ROLE_LABEL = { owner: 'Manager', staff: 'Reservations team' };
  function shell() {
    const h = state.host;
    h.innerHTML = `
      <div class="dash">
        <aside class="side">
          <p class="who"><b>${esc(state.user.display_name)}</b>Signed in · ${esc(ROLE_LABEL[state.user.role] || state.user.role)}${state.user.expires_at ? `<br><span class="small muted">Remembered on this device until ${fmtD(state.user.expires_at)}</span>` : ''}</p>
          <nav data-role="nav">
            ${[['overview', 'Overview'], ['enquiries', 'Enquiries'], ['groups', 'Groups'], ['apartments', 'Apartments'], ['data', 'Settings']]
              .map(([k, l]) => `<button type="button" data-view="${k}" ${NAV_OF[state.view] === k ? 'aria-current="page"' : ''}>${l}</button>`).join('')}
            <button type="button" data-view="password" class="side-minor" ${state.view === 'password' ? 'aria-current="page"' : ''}>Change my password</button>
            <button type="button" data-action="logout">Sign out</button>
          </nav>
          <div class="dataset ${state.dataset === 'test' ? 'is-test' : ''}" data-role="dataset">
            <b>Showing</b>
            <label><input type="radio" name="dataset" value="real" ${state.dataset === 'real' ? 'checked' : ''}> Real enquiries</label>
            <label><input type="radio" name="dataset" value="test" ${state.dataset === 'test' ? 'checked' : ''}> Test data</label>
          </div>
        </aside>
        <section class="main-pane" data-role="pane"></section>
      </div>`;
    h.querySelector('[data-role="nav"]').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      if (b.dataset.action === 'logout') return logout();
      navigate(b.dataset.view);
    });
    h.querySelector('[data-role="dataset"]').addEventListener('change', e => { state.dataset = e.target.value; document.getElementById('topbarTest').hidden = state.dataset !== 'test'; if (state.param && ['homebot', 'enquiries', 'groups'].includes(state.view)) { navigate(state.view); return; } render(); });
    document.getElementById('topbarTest').hidden = state.dataset !== 'test';
  }

  function navigate(view, param = null) { state.view = view; state.param = param; location.hash = `#/staff/${view}${param != null ? '/' + param : ''}`; }
  function pane() { return state.host.querySelector('[data-role="pane"]'); }
  function msg(el, text, ok = true) { el.textContent = text; el.className = `msg ${ok ? 'ok' : 'err'}`; }

  async function render() {
    if (!state.user) return renderLogin();
    if (!state.host.querySelector('.dash')) shell();
    state.host.querySelectorAll('[data-view]').forEach(b => { if (b.dataset.view === NAV_OF[state.view]) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
    state.host.querySelector('.dataset').classList.toggle('is-test', state.dataset === 'test');
    // a fresh pane element on every render: view handlers attach click listeners to the pane, and reusing the same element
    // across renders/navigations would stack them (one click → several actions, on earlier records too)
    const old = pane(); const p = old.cloneNode(false); old.replaceWith(p); p.innerHTML = '<p class="loading">Loading…</p>';
    try {
      const fn = views[state.view] || views.overview;
      // T9: every table gets per-cell labels (from its header row) so it can switch to a card layout on narrow screens
      new MutationObserver(() => { labelTables(p); plainLanguage(p); }).observe(p, { childList: true, subtree: true, characterData: true });
      await fn(p);
      labelTables(p); plainLanguage(p);
    } catch (e) {
      if (e.status === 401) { state.user = null; api.setToken(null); return renderLogin('Your session has ended. Please sign in again.'); }
      p.innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    }
  }

  // Plain language for reservations staff (T10 is met at source: every label, note and message is written in plain English in
  // this file and on the server). This is only a development safety net: it looks for a register code (GP8, A4(b), I16(f), T6 …)
  // in rendered text and logs a console warning so the source can be fixed. It never rewrites what staff see.
  const DEV = /^(localhost|127\.0\.0\.1|0\.0\.0\.0)$/.test(location.hostname);
  const RE_CODE = /(?:^|[\s(,;:>])(?:GP|A|B|I|M|T)\d{1,2}(?:\([a-z]\))*(?=$|[\s),;:.<])/;
  const warned = new Set();
  function plainLanguage(root) {
    if (!DEV) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const n = walker.currentNode; const v = n.nodeValue;
      if (!v || !/\d/.test(v) || !n.parentElement || ['SCRIPT', 'STYLE'].includes(n.parentElement.tagName) || !RE_CODE.test(v)) continue;
      const key = v.trim().slice(0, 120);
      if (!warned.has(key)) { warned.add(key); console.warn('[MMAI_T10] register code in staff text; fix at source:', key); }
    }
    root.querySelectorAll('[title], [placeholder]').forEach(el => { for (const a of ['title', 'placeholder']) { const v = el.getAttribute(a); if (v && RE_CODE.test(v) && !warned.has(v)) { warned.add(v); console.warn('[MMAI_T10] register code in ' + a + '; fix at source:', v); } } });
  }

  function labelTables(root) {
    root.querySelectorAll('table.tbl:not([data-labelled])').forEach(t => {
      const heads = [...t.querySelectorAll('thead th')].map(th => th.textContent.trim());
      if (!heads.length) return;
      t.dataset.labelled = '1';
      t.querySelectorAll('tbody tr').forEach(tr => [...tr.children].forEach((td, i) => { if (heads[i] && !td.dataset.label) td.dataset.label = heads[i]; }));
    });
  }

  // ---------------- Login ----------------
  function renderLogin(note) {
    state.host.innerHTML = `
      <div class="card login">
        <h1>Staff sign in</h1>
        <p>MateBot AI staff dashboard. Sign in with your own username and password. Personal data is only shown after signing in. Once signed in, this device stays signed in for 30 days or until you sign out.</p>
        ${note ? `<p class="msg err">${esc(note)}</p>` : ''}
        <form data-role="login">
          <div class="field"><label>Username<input name="username" autocomplete="username" required></label></div>
          <div class="field"><label>Password<input name="password" type="password" autocomplete="current-password" required></label></div>
          <button class="btn" type="submit">Sign in</button>
          <p class="msg err" data-role="err" hidden></p>
        </form>
      </div>`;
    state.host.querySelector('[data-role="login"]').addEventListener('submit', async e => {
      e.preventDefault(); const f = e.target; const err = f.querySelector('[data-role="err"]'); err.hidden = true;
      try {
        const r = await api.post('/api/auth/login', { username: f.username.value, password: f.password.value });
        api.setToken(r.token); state.user = r; state.resumed = null; state.ref = await api.get('/api/reference');
        document.getElementById('topbarRight').innerHTML = `<span>${esc(r.display_name)}</span><a href="#/">Public form</a>`;
        render();
      } catch (ex) { err.hidden = false; err.textContent = ex.message; }
    });
  }
  async function logout() { try { await api.post('/api/auth/logout'); } catch { } api.setToken(null); state.user = null; document.getElementById('topbarRight').innerHTML = '<a href="#/">Public form</a>'; renderLogin(); }

  // ---------------- Views ----------------
  const views = {};

  views.overview = async p => {
    const test = state.dataset === 'test';
    const s = await api.get('/api/stats?dataset=' + state.dataset);
    const dq = await api.get('/api/apartments-data-quality');
    const setName = test ? 'test' : 'real';
    const barred = test ? s.enquiries_barred_test : s.enquiries_barred;
    const adultsTodo = test ? s.adults_todo_test : s.adults_todo;
    const countries = test ? s.unconfirmed_countries_test : s.unconfirmed_countries;
    const memberCountries = test ? s.unconfirmed_member_countries_test : s.unconfirmed_member_countries;
    p.innerHTML = `
      <div class="pane-head"><h1>Overview</h1><span class="badge ${test ? 'badge--test' : ''}">${test ? 'Test data' : 'Real enquiries'}</span><span class="spacer"></span>${s.ai_configured ? '<span class="badge badge--ai">AI configured: readiness requires a valid review</span>' : '<span class="badge badge--warn">AI not connected: new groups wait for AI review</span>'}</div>
      <div class="kpis">
        <div class="kpi"><b>${test ? s.enquiries_test : s.enquiries_real}</b><span>${test ? 'Test' : 'Real'} enquiries</span></div>
        <div class="kpi"><b>${s.apartments_available} / ${s.apartments}</b><span>Apartments available${s.apartments_leased ? ` · ${s.apartments_leased} leased` : ''}</span></div>
        <div class="kpi"><b>${s.groups_proposed}</b><span>Proposed groups${s.groups_alternative ? ` (+${s.groups_alternative} alternatives)` : ''}</span></div>
        <div class="kpi ${s.groups_ai_pending_primary || s.groups_ai_failed ? 'kpi--flag' : ''}" title="A group is ready only once the AI has assessed it and ranked its apartments. Travel is checked last, on each group's page, and is not part of this count"><b>${s.groups_ready}</b><span>Ready for reservations (travel not confirmed)${s.groups_ai_pending_primary ? ` · ${s.groups_ai_pending_primary} waiting for the AI` : ''}${s.groups_ai_failed ? ` · <a href="#/staff/groups" data-ai-failed>${s.groups_ai_failed} AI failed – retry</a>` : ''}</span></div>
        <div class="kpi"><b>${s.groups_active}</b><span>Introduced or confirmed</span></div>
        <div class="kpi"><b>${s.groups_leased}</b><span>Leased</span></div>
        ${s.groups_awaiting ? `<div class="kpi kpi--flag"><b>${s.groups_awaiting}</b><span>Awaiting a replacement${s.groups_replacement_found ? ` · ${s.groups_replacement_found} with a replacement to review` : ''}</span></div>` : ''}
      </div>
      <div class="detail detail--2">
        <div class="card card--tight sched"><h3>Hourly matching</h3>${schedulerHtml(s.scheduler)}
          <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="groups">Open groups</button></div></div>
        ${s['homebot_open' + (test ? '_test' : '')] || s['homebot_new' + (test ? '_test' : '')] ? `<div class="card card--tight"><h3>HomeBot apartment enquiries</h3><p class="msg"><span class="badge badge--homebot">HomeBot</span> <b>${s['homebot_new' + (test ? '_test' : '')] || 0}</b> new, <b>${s['homebot_open' + (test ? '_test' : '')] || 0}</b> open${s['homebot_flagged' + (test ? '_test' : '')] ? `, <b>${s['homebot_flagged' + (test ? '_test' : '')]}</b> with review flags` : ''}. Whole-apartment enquiries from the HomeBot form have their apartment options reviewed by reservations before an offer is communicated. These enquiries are never part of housemate matching. Submission receipts are separate from apartment offers.</p><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="homebot">Open HomeBot enquiries</button></div></div>` : ''}
        ${s.groups_reranked ? `<div class="card card--tight"><h3>Groups moved after a lease</h3><p class="msg"><span class="badge badge--flag">${s.groups_reranked}</span> group(s) moved to their next best apartment because an apartment was leased. Open Groups to review and clear the flags.</p><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="groups">Open groups</button></div></div>` : ''}
        ${s.mate_requests_pending ? `<div class="card card--tight"><h3>Friends waiting to join a group</h3><p class="msg"><span class="badge badge--flag">${s.mate_requests_pending}</span> friend(s) came through a member's "Bring a mate" link and fit a group that has already been shared with its members. Open the group to approve or decline the change (groups not yet shared, or awaiting a replacement, take friends without approval).</p><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="groups">Open groups</button></div></div>` : ''}
        ${s.groups_awaiting ? `<div class="card card--tight"><h3>Groups awaiting a replacement</h3><p class="msg"><span class="badge badge--flag">${s.groups_awaiting}</span> group(s) lost a member (rejected or barred) and no replacement passed every rule. They are kept together with an empty bed for you to handle${s.groups_replacement_found ? `; <b>${s.groups_replacement_found}</b> now have a replacement for you to accept or decline (nothing is applied automatically)` : ''}.</p><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="groups" data-go-filter="Awaiting replacement">Open awaiting groups</button></div></div>` : ''}
        ${barred ? `<div class="card card--tight"><h3>Barred from matching</h3><p class="msg"><span class="badge badge--warn">${barred}</span> ${setName} enquiry(ies) are barred by staff: they are left out of every run and every replacement until the bar is lifted.</p><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-go="enquiries" data-go-todo="barred">Show barred people</button></div></div>` : ''}
        ${!test && s.couples_unanswered ? `<div class="card card--tight"><h3>To do: couples question</h3><p class="msg"><span class="badge badge--warn">${s.couples_unanswered}</span> "With friends" enquiry(ies) were saved before the couples question was added to the form. MateBot AI treats them as having no couple until you confirm by phone and record the answer in the enquiry.</p></div>` : ''}
      </div>
      <div class="detail detail--2">
        <div class="card card--tight"><h3>${test ? 'Test' : 'Real'} enquiries by status</h3>
          <dl class="kv">${Object.entries(test ? s.by_status_test : s.by_status).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join('') || '<dt>None yet</dt><dd></dd>'}</dl>
          ${countries ? `<p class="msg">${countries} enquiry(ies) have a country of birth MateBot AI could not recognise. Confirm them in the enquiry.</p>` : ''}
          ${memberCountries ? `<p class="msg">${memberCountries} party member(s) have a country of birth to confirm.</p>` : ''}
        </div>
        <div class="card card--tight"><h3>To do: 18 or older at move-in</h3>
          ${adultsTodo ? `<p class="msg"><span class="badge badge--warn">${adultsTodo}</span> enquiry(ies) need someone to confirm that everyone in the group will be 18 or older on the move-in date: either the enquiry was saved before the question existed, or the move-in date or group changed since it was answered. Check with the enquirer and record it in the enquiry.</p>` : '<p class="small muted">Every enquiry carries a current confirmation that everyone in the group will be 18 or older at move-in. When one needs renewing (earlier move-in or a changed group) or was never given, it is flagged here and in its detail.</p>'}
          <p class="small muted">Open the Enquiries list and filter by “18+ to confirm” to see them.</p>
        </div>
        <div class="card card--tight dq"><h3>Apartment list checks</h3>
          <ul>
            <li>${dq.apartment_count} apartments loaded${dq.a1_matches ? ', the full launch list' : ` <span class="badge badge--warn">launch list has ${dq.a1_expected}</span>`}.</li>
            <li>${dq.non_date_rows_live.length} apartment(s) not yet available: matched once staff enter a date.</li>
            <li>${dq.capacity_issues_live.length ? `<span class="badge badge--warn">${dq.capacity_issues_live.length}</span> apartment(s) with more beds than the type allows; review in Apartments.` : 'Bed counts within the limit for every apartment type.'}</li>
            <li>Minimum lease ${Object.keys(dq.min_lease_values || {}).length === 1 ? `${dq.min_lease_default} months on every apartment` : `varies by apartment (generally ${dq.min_lease_default} months)`}; staff can change it per apartment.</li>
          </ul>
          <button class="btn btn--ghost btn--sm" type="button" data-go="apartments">Open apartments</button>
        </div>
      </div>`;
    p.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => {
      if (b.dataset.goFilter) state.cache.grpFilter = { status: b.dataset.goFilter };
      if (b.dataset.goTodo) state.cache.enqFilter = { todo: b.dataset.goTodo };
      navigate(b.dataset.go);
    }));
  };

  // Hourly matching panel (Overview and Groups)
  function schedulerHtml(sc, full) {
    if (!sc) return '<p class="small muted">Scheduler status unavailable.</p>';
    const lr = sc.last_scheduled_run;
    const per = Object.entries(sc.per_dataset || {}).map(([d, v]) => `<li><b>${esc(d)}</b> data: ${v.last_completed_run ? `last completed run #${v.last_completed_run.id} started ${fmtDT(v.last_completed_run.started_at)}` : 'no completed run yet'}; <b>${v.new_applicants_since}</b> new applicant(s) since → next check will ${v.new_applicants_since ? 'run' : 'defer'}.</li>`).join('');
    const log = full && sc.log ? `<details class="more mt" open><summary>Hourly check log (${sc.log.length} most recent)</summary><div class="sched-log mt"><table><tbody>${sc.log.map(l => `<tr><td class="mono">${fmtDT(l.checked_at)}</td><td>${esc(l.dataset)}</td><td class="outcome--${esc(l.outcome)}">${esc(l.outcome)}</td><td>${esc(l.reason || '')}${l.run_id ? ` · run #${l.run_id}` : ''}</td><td class="small muted">${esc(l.trigger_kind || '')}</td></tr>`).join('') || '<tr><td>No checks yet.</td></tr>'}</tbody></table></div></details>` : '';
    return `<p class="small">Matching runs automatically every hour <b>only if at least one new applicant has arrived</b> since the last completed run; otherwise it is deferred to the next hour. Never two runs at once; the schedule survives a restart. ${sc.enabled ? '' : '<span class="badge badge--warn">scheduler off</span>'}</p>
      <div class="kpis kpis--tight"><div class="kpi"><b>${sc.last_check_at ? fmtDT(sc.last_check_at) : '—'}</b><span>last hourly check</span></div><div class="kpi"><b>${sc.next_check_at ? fmtDT(sc.next_check_at) : '—'}</b><span>next check</span></div><div class="kpi"><b>${lr ? fmtDT(lr.checked_at) : '—'}</b><span>last scheduled run${lr ? ` · #${lr.run_id} (${esc(lr.dataset)})` : ''}</span></div></div>
      <ul class="small">${per}</ul>${sc.running ? `<p class="msg">A run (#${sc.running[0]}, ${esc(sc.running[1])} data) is in progress now.</p>` : ''}${log}`;
  }

  // ---- Enquiries list ----
  views.enquiries = async p => {
    if (state.param) return enquiryDetail(p, +state.param);
    const f = state.cache.enqFilter || {};
    p.innerHTML = `
      <div class="pane-head"><h1>Enquiries</h1><span class="spacer"></span><button class="btn btn--sm" type="button" data-go="new">New phone enquiry</button> <button class="btn btn--ghost btn--sm" type="button" data-go="email">Send form by email</button>
        <p>${state.dataset === 'test' ? 'Showing test records only.' : 'Showing real enquiries only.'}</p></div>
      <nav class="subtabs" aria-label="Enquiry type"><button type="button" class="btn btn--sm" aria-current="page">MateBot · rooms and beds</button><button type="button" class="btn btn--ghost btn--sm" data-go="homebot">HomeBot · whole apartments</button></nav>
      <form class="toolbar" data-role="filters">
        <input type="search" name="q" placeholder="Search name, email or phone" value="${esc(f.q || '')}">
        <select name="status"><option value="">Any status</option>${state.ref.statuses.map(s => `<option ${f.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <select name="channel"><option value="">Any channel</option>${state.ref.channels.map(s => `<option ${f.channel === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <input type="date" name="move_in_from" value="${esc(f.move_in_from || '')}" title="Move-in from"><input type="date" name="move_in_to" value="${esc(f.move_in_to || '')}" title="Move-in to">
        <input type="number" name="budget_min" placeholder="Budget ≥" value="${esc(f.budget_min || '')}" style="width:110px"><input type="number" name="budget_max" placeholder="Budget ≤" value="${esc(f.budget_max || '')}" style="width:110px">
        <input name="school" placeholder="School" value="${esc(f.school || '')}" style="width:140px"><input name="country" placeholder="Country" value="${esc(f.country || '')}" style="width:130px">
        <select name="todo" title="Staff to-do"><option value="">Any to-do</option><option value="adults" ${f.todo === 'adults' ? 'selected' : ''}>18+ to confirm</option><option value="country" ${f.todo === 'country' ? 'selected' : ''}>Country to confirm</option><option value="barred" ${f.todo === 'barred' ? 'selected' : ''}>Barred from matching</option></select>
        <button class="btn btn--sm" type="submit">Filter</button><button class="btn btn--ghost btn--sm" type="button" data-role="clear">Clear</button>
      </form>
      <div data-role="list"></div>`;
    p.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));
    const form = p.querySelector('[data-role="filters"]');
    form.addEventListener('submit', e => { e.preventDefault(); const o = {}; new FormData(form).forEach((v, k) => { if (v) o[k] = v; }); state.cache.enqFilter = o; load(); });
    form.querySelector('[data-role="clear"]').addEventListener('click', () => { state.cache.enqFilter = {}; render(); });
    async function load() {
      const list = p.querySelector('[data-role="list"]'); list.innerHTML = '<p class="loading">Loading…</p>';
      const qs = new URLSearchParams({ dataset: state.dataset, ...(state.cache.enqFilter || {}) });
      const r = await api.get('/api/enquiries?' + qs);
      list.innerHTML = `<p class="small muted">${r.total} enquiry(ies)</p>
        <div class="table-wrap"><table class="tbl"><thead><tr><th>#</th><th>Name</th><th>Status</th><th>Move in → out</th><th>Budget</th><th>Country · school</th><th>Party</th><th>Enquired · channel</th></tr></thead><tbody>
        ${r.items.map(e => `<tr data-id="${e.id}"><td class="mono">${e.id}${e.is_test ? '<br><span class="badge badge--test">TEST</span>' : ''}</td>
          <td class="wrap-cell"><b>${esc(e.first_name)} ${esc(e.last_name)}</b><br><span class="small muted">${esc(e.email)}</span></td>
          <td>${badge(e.status)} ${barredBadge(e)}</td><td class="mono">${fmtD(e.move_in)} →<br>${fmtD(e.move_out)}</td><td class="mono">${money(e.budget_max)}<br><span class="small muted">${e.budget_guide}</span></td>
          <td class="wrap-cell">${esc(e.birth_country || e.birth_country_raw)}${e.birth_country_confirmed ? '' : ' <span class="badge badge--warn">confirm</span>'}<br><span class="small muted">${esc((e.school || '').split(' –')[0])}</span></td>
          <td>${esc(e.party === 'Alone' ? 'Alone' : e.party.startsWith('Couple') ? 'Couple' : 'Friends ×' + e.party_size)} ${adultsBadge(e)}</td>
          <td class="small mono">${fmtD(e.created_at)}<br><span class="muted">${esc(e.channel)}</span></td></tr>`).join('') || '<tr><td colspan="8" class="muted">No enquiries match.</td></tr>'}
        </tbody></table></div>`;
      list.querySelector('tbody').addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) navigate('enquiries', tr.dataset.id); });
    }
    await load();
  };

  /* ---------- HomeBot (whole-apartment) enquiries: a separate table, reviewed by hand. Never shown in MateBot matching. ---------- */
  const HB_STATUSES = ['New', 'Staff reviewing', 'Options sent', 'Viewing arranged', 'Leased', 'Closed'];
  const hbBadge = st => `<span class="badge ${st === 'New' ? 'badge--new' : st === 'Leased' ? 'badge--leased' : st === 'Closed' ? 'badge--withdrawn' : 'badge--introduced'}">${esc(st)}</span>`;
  views.homebot = async p => {
    if (state.param) return homebotDetail(p, +state.param);
    const f = state.cache.hbFilter || {};
    p.innerHTML = `
      <div class="pane-head"><h1>Enquiries</h1><span class="spacer"></span>
        <p>${state.dataset === 'test' ? 'Showing test records only.' : 'Showing real enquiries only.'}</p></div>
      <nav class="subtabs" aria-label="Enquiry type"><button type="button" class="btn btn--ghost btn--sm" data-go="enquiries">MateBot · rooms and beds</button><button type="button" class="btn btn--sm" aria-current="page">HomeBot · whole apartments</button></nav>
      <p class="small muted"><span class="badge badge--homebot">HomeBot</span> Whole-apartment enquiries from the HomeBot form, separate from MateBot housemate matching. Review the enquiry's current apartment options and processing status before contacting the applicant. A submission email is a copy of their answers, not an apartment offer.</p>
      <form class="toolbar" data-role="filters">
        <input type="search" name="q" placeholder="Search name, email, phone or reference (e.g. HomeBot-00012)" value="${esc(f.q || '')}" style="min-width:260px">
        <select name="status"><option value="">Any status</option>${HB_STATUSES.map(s => `<option ${f.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <select name="flag"><option value="">All</option><option value="flagged" ${f.flag === 'flagged' ? 'selected' : ''}>With review flags</option></select>
        <button class="btn btn--sm" type="submit">Filter</button><button class="btn btn--ghost btn--sm" type="button" data-role="clear">Clear</button>
      </form>
      <div data-role="list"></div>`;
    p.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));
    const form = p.querySelector('[data-role="filters"]');
    form.addEventListener('submit', e => { e.preventDefault(); const o = {}; new FormData(form).forEach((v, k) => { if (v) o[k] = v; }); state.cache.hbFilter = o; load(); });
    form.querySelector('[data-role="clear"]').addEventListener('click', () => { state.cache.hbFilter = {}; render(); });
    async function load() {
      const list = p.querySelector('[data-role="list"]'); list.innerHTML = '<p class="loading">Loading…</p>';
      const r = await api.get('/api/homebot/enquiries?' + new URLSearchParams({ dataset: state.dataset, ...(state.cache.hbFilter || {}) }));
      list.innerHTML = `<p class="small muted">${r.total} HomeBot enquiry(ies)</p>
        <div class="table-wrap"><table class="tbl"><thead><tr><th>Reference</th><th>Name</th><th>Status</th><th>People</th><th>Move in → out</th><th>Whole-apartment budget</th><th>Wants</th><th>Flags</th><th>Enquired</th></tr></thead><tbody>
        ${r.items.map(e => `<tr data-id="${e.id}"><td class="mono"><span class="badge badge--homebot">HomeBot</span><br>${esc(e.reference)}${e.is_test ? '<br><span class="badge badge--test">TEST</span>' : ''}</td>
          <td class="wrap-cell"><b>${esc(e.first_name)} ${esc(e.last_name)}</b><br><span class="small muted">${esc(e.email)}</span></td>
          <td>${hbBadge(e.status)}${e.staff_member ? `<br><span class="small muted">${esc(e.staff_member)}</span>` : ''}</td>
          <td>${e.party_mode === 'complete' ? `${e.headcount_now} · complete` : `${e.headcount_now} of ${e.headcount_target} <span class="badge badge--warn" title="Group still growing; they declined MateBot help">growing</span>`}</td>
          <td class="mono">${fmtD(e.move_in)} →<br>${fmtD(e.move_out)}</td><td class="mono">${money(e.budget_total_weekly)}/wk</td>
          <td class="wrap-cell small">${esc((e.bedrooms || []).join(', '))}<br><span class="muted">${esc(e.area || '')}</span></td>
          <td class="wrap-cell small">${(e.review_flags || []).map(x => `<span class="badge badge--flag badge--long">${esc(x)}</span>`).join('') || '<span class="muted">—</span>'}</td>
          <td class="small mono">${fmtDT(e.created_at)}</td></tr>`).join('') || '<tr><td colspan="9" class="muted">No HomeBot enquiries match.</td></tr>'}
        </tbody></table></div>`;
      list.querySelector('tbody').addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) navigate('homebot', tr.dataset.id); });
    }
    await load();
  };

  function setDataset(ds) {
    if (state.dataset === ds) return;
    state.dataset = ds;
    const r = state.host.querySelector(`[data-role="dataset"] input[value="${ds}"]`); if (r) r.checked = true;
    state.host.querySelector('.dataset').classList.toggle('is-test', ds === 'test');
    document.getElementById('topbarTest').hidden = ds !== 'test';
  }
  /* ---------- HomeBot requirement edit (H25/H29): staff change what the party is looking for after talking to them. The merged record
     goes through the same validator as the public form; a new requirement version is kept and a fresh rules check is queued.
     Contact details and consents are not editable here. Only the fields that were changed are sent. ---------- */
  async function homebotRequirements(box, id, e) {
    const meta = await api.get('/api/public/homebot/meta');
    const ch = meta.choices || {};
    const growing = e.party_mode === 'more_declined';
    const sel = (name, options, cur) => `<select name="${name}">${(options || []).map(o => `<option ${o === cur ? 'selected' : ''}>${esc(o)}</option>`).join('')}</select>`;
    const checks = (name, options, cur) => `<div class="chips">${(options || []).map(o => `<label class="chip"><input type="checkbox" name="${name}" value="${esc(o)}" ${(cur || []).includes(o) ? 'checked' : ''}> ${esc(o)}</label>`).join('')}</div>`;
    const dateVal = v => v || '';
    box.innerHTML = `<details class="card card--tight"><summary><b>Change the party's requirements</b> (after talking to them)</summary>
      <p class="small muted">Use this when the applicant tells you something has changed — dates, budget, size of apartment, area or travel. The change is checked like the form, kept as a new version with your name on it, and the options are prepared again. Names, contact details and consents are not changed here.</p>
      <div class="detail detail--2">
        <div><div class="field"><label>Move in<input type="date" name="move_in" value="${dateVal(e.move_in)}"></label></div>
          <div class="field"><label>Move-in flexibility${sel('move_in_flex', ch.flex, e.move_in_flex)}</label></div>
          <div class="field"><label>Move out<input type="date" name="move_out" value="${dateVal(e.move_out)}"></label></div>
          <div class="field"><label>Move-out flexibility${sel('move_out_flex', ch.flex, e.move_out_flex)}</label></div>
          <div class="field"><label>Whole-apartment budget per week<input type="number" name="budget_total_weekly" min="1" step="1" value="${e.budget_total_weekly ?? ''}"></label></div>
          ${growing ? `<div class="field"><label>People wanted in total<input type="number" name="headcount_target" min="${(e.headcount_now || 1) + 1}" max="${meta.max_people || 7}" step="1" value="${e.headcount_target ?? ''}"></label></div>` : ''}</div>
        <div><div class="field"><span class="small">Bedrooms</span>${checks('bedrooms', ch.bedrooms, e.bedrooms)}</div>
          <div class="field"><span class="small">Bathrooms</span>${checks('bathrooms', ch.bathrooms, e.bathrooms)}</div>
          <div class="field"><label>Area${sel('area', ch.areas, e.area)}</label></div>
          <div class="field"><span class="small">Transport</span>${checks('transport', (ch.transport || []).concat((e.transport || []).filter(o => !(ch.transport || []).includes(o))), e.transport)}${
            (e.transport || []).some(o => HB_TRANSPORT_EARLIER.includes(o)) ? '<p class="small muted">Tram, Train, Bus and Car are no longer offered on the form and those journeys are not measured, so travel for this party reads “needs confirmation”. They are not treated as “Public transport”, which is one combined choice and can include any tram, train, bus or ferry. Leave the earlier answer ticked unless the applicant has told you how they will travel; then untick it and tick what they said.</p>' : ''}</div>
          <div class="field"><label>Longest acceptable travel${sel('max_travel', ch.max_travel, e.max_travel)}</label></div>
          <div class="field"><label>Notes about the apartment<textarea name="apartment_notes" rows="2" maxlength="2000">${esc(e.apartment_notes || '')}</textarea></label></div></div>
      </div>
      <div class="actions"><button class="btn btn--sm" type="button" data-action="hb-req-save">Save the changed requirements</button></div>
      <p class="msg" data-role="hb-req-msg"></p></details>`;
    const m = box.querySelector('[data-role="hb-req-msg"]');
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    box.addEventListener('click', async ev => {
      const b = ev.target.closest('button[data-action="hb-req-save"]'); if (!b) return;
      const patch = {};
      const val = n => box.querySelector(`[name="${n}"]`).value;
      const list = n => Array.from(box.querySelectorAll(`[name="${n}"]:checked`)).map(x => x.value);
      ['move_in', 'move_out', 'move_in_flex', 'move_out_flex', 'area', 'max_travel', 'apartment_notes'].forEach(k => { const v = val(k).trim(); if ((v || '') !== (e[k] || '')) patch[k] = v; });
      ['bedrooms', 'bathrooms', 'transport'].forEach(k => { const v = list(k); if (!same(v, e[k] || [])) patch[k] = v; });
      const bud = val('budget_total_weekly'); if (bud !== '' && Number(bud) !== Number(e.budget_total_weekly)) patch.budget_total_weekly = Number(bud);
      if (growing) { const ht = val('headcount_target'); if (ht !== '' && Number(ht) !== Number(e.headcount_target)) patch.headcount_target = Number(ht); }
      if (!Object.keys(patch).length) { msg(m, 'Nothing has been changed.', false); return; }
      b.disabled = true;
      try { await api.patch(`/api/homebot/enquiries/${id}/requirements`, patch); msg(m, 'Saved as a new version of the requirements; the options are being prepared again.'); setTimeout(render, 1200); }
      catch (ex) { msg(m, ex.message, false); b.disabled = false; }
    });
  }

  async function homebotDetail(p, id) {
    const e = await api.get(`/api/homebot/enquiries/${id}`);
    setDataset(e.is_test ? 'test' : 'real');   // a deep link or reload shows the record's own set, never TEST under "Real"
    const people = e.people || [];
    const growing = e.party_mode !== 'complete';
    p.innerHTML = `
      <div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-role="back">← HomeBot enquiries</button>
        <h1>${esc(e.reference)} · ${esc(e.first_name)} ${esc(e.last_name)}</h1><span class="badge badge--homebot">HomeBot · whole apartment</span>${hbBadge(e.status)}${e.is_test ? '<span class="badge badge--test">TEST</span>' : ''}${growing ? '<span class="badge badge--warn">group still growing</span>' : ''}
        <span class="spacer"></span><button class="btn btn--danger btn--sm" type="button" data-action="delete">Delete</button></div>
      <p class="small muted">Enquired ${fmtDT(e.created_at)} · last staff contact ${e.last_contact_at ? fmtDT(e.last_contact_at) : 'none yet'}</p>
      <p class="msg"><b>Manual review.</b> ${esc((e.matching && e.matching.note) || 'No automatic apartment matching has run for this enquiry.')} Nothing has been emailed to the applicant from this system.</p>
      <div data-role="options"></div>
      <div data-role="exit-exceptions"></div>
      ${(e.review_flags || []).length ? `<div class="card card--tight"><h3>Review flags</h3><ul class="small">${e.review_flags.map(x => `<li><span class="badge badge--flag">flag</span> ${esc(x)}</li>`).join('')}</ul><p class="small muted">Flags point at things to check by hand — they do not exclude or qualify anyone.</p></div>` : ''}
      <div class="detail detail--2">
        <div class="card card--tight"><h3>Staff</h3>
          <div class="field"><label>Status<select name="status">${HB_STATUSES.map(s => `<option ${e.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
          <div class="field"><label>Staff member<input name="staff_member" value="${esc(e.staff_member || '')}"></label></div>
          <div class="field"><label>Staff notes<textarea name="staff_notes" rows="4">${esc(e.staff_notes || '')}</textarea></label></div>
          <div class="actions"><button class="btn btn--sm" type="button" data-action="save-staff">Save</button><button class="btn btn--ghost btn--sm" type="button" data-action="contact">Log a contact</button></div>
          <p class="msg" data-role="msg1"></p>
          <p class="small muted">Statuses here are HomeBot's own; "Options sent" and "Viewing arranged" record what a staff member has done — nothing is sent from this page.</p>
        </div>
        <div class="card card--tight"><h3>People (${people.length}${growing ? ` of ${e.headcount_target} wanted` : ''})</h3>
          <div class="table-wrap"><table class="tbl"><thead><tr><th>#</th><th>Name</th><th>Bed</th><th>Weekly max</th><th>Doing</th><th>Study</th><th>Work / campus detail</th><th>Main destination</th></tr></thead><tbody>
          ${people.map(m => `<tr><td class="mono">${m.position}</td><td>${esc(m.name || '—')}</td><td>${esc(m.sleep)}</td><td class="mono">${money(m.weekly_max)}</td><td>${esc(m.doing)}</td><td class="wrap-cell">${esc(m.institution === 'Other' ? `Other: ${m.institution_other || ''}` : m.institution || '—')}</td><td class="wrap-cell">${esc(m.place || '—')}</td><td>${esc(m.primary_destination || '—')}</td></tr>`).join('')}
          </tbody></table></div>
          ${growing ? `<p class="small muted">Only the ${people.length} people in the group today are listed; the applicant is finding the rest themselves.</p>` : ''}
          ${people.filter(m => m.sleep === 'Shares a bed with partner').length > 2 ? '<p class="small muted"><span class="badge badge--flag">flag</span> More than one couple: which people are partners is not recorded by the form — confirm the pairing with the applicant.</p>' : ''}
        </div>
      </div>
      <div data-role="reqs"></div>
      <div class="detail detail--2">
        ${(e.summary || []).map(sec => `<div class="card card--tight"><h3>${esc(sec.title)}</h3><dl class="kv">${sec.rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v == null || v === '' ? '<span class="muted">—</span>' : esc(v)}</dd>`).join('')}</dl></div>`).join('')}
        <div class="card card--tight"><h3>History</h3><ul class="small">${(e.history || []).map(h => `<li>${fmtDT(h.at)} · ${esc(h.staff_member || 'system')} · ${esc(h.action)}${h.detail ? ` — ${esc(h.detail)}` : ''}</li>`).join('') || '<li class="muted">No entries yet.</li>'}</ul></div>
      </div>`;
    p.querySelector('[data-role="back"]').addEventListener('click', () => navigate('homebot'));
    window.MMAI_STAFF_EXCEPTIONS.mount(p.querySelector('[data-role="exit-exceptions"]'), {
      bot: 'homebot', id, owner: e, canWrite: !['Leased', 'Closed'].includes(e.status), onChanged: render
    });
    if (e.matching && e.matching.automatic) homebotOptions(p.querySelector('[data-role="options"]'), id, e).catch(ex => { p.querySelector('[data-role="options"]').innerHTML = `<p class="msg err">${esc(ex.message)}</p>`; });
    if (e.matching && e.matching.automatic && !['Leased', 'Closed'].includes(e.status)) homebotRequirements(p.querySelector('[data-role="reqs"]'), id, e).catch(() => {});
    p.addEventListener('click', async ev => {
      const b = ev.target.closest('button[data-action]'); if (!b) return;
      if (b.closest('[data-role="options"]') || b.closest('[data-role="reqs"]') || b.closest('[data-role="exit-exceptions"]')) return;   // handled by their own panels
      const m1 = p.querySelector('[data-role="msg1"]');
      try {
        if (b.dataset.action === 'save-staff') { const patch = {}; ['status', 'staff_member', 'staff_notes'].forEach(k => patch[k] = p.querySelector(`[name="${k}"]`).value); await api.patch(`/api/homebot/enquiries/${id}`, patch); msg(m1, 'Saved.'); setTimeout(render, 600); }
        if (b.dataset.action === 'contact') { await api.post(`/api/homebot/enquiries/${id}/contact`, { note: 'Staff logged contact' }); msg(m1, 'Contact logged.'); setTimeout(render, 600); }
        if (b.dataset.action === 'delete') { if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to permanently delete'; return; } await api.del(`/api/homebot/enquiries/${id}`); navigate('homebot'); }
      } catch (ex) { msg(m1, ex.message, false); }
    });
  }

  /* ---------- HomeBot apartment options (integrated matching, slice H2): a rules check prepared in the background, shown in plain words for staff review.
     Nothing here contacts the applicant; "Options sent" is still recorded by staff by hand after they have chosen what to offer. ---------- */
  /* Live travel check: journey times for the options on the page, asked for when staff open the options or press the button.
     The reply is shown on this page only and is not saved anywhere; this small in-page note only stops the page asking again
     every time it redraws, and is cleared after 30 minutes or when the page is closed. */
  const hbTravelShown = new Map();
  // answers from earlier versions of the form that are kept as given and never measured (never read as "Public transport")
  const HB_TRANSPORT_EARLIER = ['Tram', 'Train', 'Bus', 'Car'];
  // the provider's name as plain text: never translated, wrapped or restyled (see .gmaps-text)
  const GMAPS_TEXT = '<span class="gmaps-text" translate="no">Google Maps</span>';
  // the provider's own logo, unaltered, beside its journey figures (its name is the label, and is what shows if the image cannot load)
  const GMAPS_ATTR = '<img class="gmaps-attr" src="/img/google_maps_logo.svg" alt="Google Maps" translate="no" width="98" height="18">';
  const HB_TRAVEL_KEEP_MS = 30 * 60 * 1000;
  async function homebotOptions(box, id, e) {
    const ov = await api.get(`/api/homebot/enquiries/${id}/options`);
    if (!ov.enabled) { box.innerHTML = `<p class="small muted">${esc(ov.plain)}</p>`; return; }
    const liveTravel = ov.live_travel || {};
    const travelKey = (key, cur) => `${id}:${key}:${cur.id}`;
    // old journey times are dropped for every enquiry, not only the one on screen, and never shown as current
    hbTravelShown.forEach(kept => { if (kept.reply && Date.now() - kept.at > HB_TRAVEL_KEEP_MS) kept.reply = null; });
    const travelKept = (key, cur) => hbTravelShown.get(travelKey(key, cur));
    const offer = o => o ? `${o.start} → ${o.end} · ${money(o.total_weekly_rent)}/week${o.months ? ` · ${o.months} months` : ''}` : '—';
    const routeMinutes = value => {
      if (value == null) return '';
      const parts = String(value).split('/');
      const n = Number(parts[0]) / (parts.length === 2 ? Number(parts[1]) : 1);
      return Number.isFinite(n) ? n.toFixed(1).replace(/\.0$/, '') : '';
    };
    const journeys = x => {
      const f = x.travel && x.travel.facts;
      if (!f) return '';
      const lines = (f.people || []).map(person => {
        const measured = person.fastest_minutes != null;
        const mins = measured ? routeMinutes(person.fastest_minutes) : '';
        const detail = measured ? `${mins} minutes (rounded) by ${(person.fastest_modes || []).join(' / ')}` :
          ((f.no_destination || []).includes(person.index) ? 'destination not yet known' :
            // an earlier answer (tram, train, bus, car) is never measured and never swapped for another way of travelling
            ((person.unknown_modes || []).some(o => HB_TRANSPORT_EARLIER.includes(o)) ?
              `travel time needs confirmation (the earlier answer ${(person.unknown_modes || []).filter(o => HB_TRANSPORT_EARLIER.includes(o)).join(', ')} is not measured; confirm with the applicant how they will travel)` :
              'travel time needs confirmation'));
        const warning = person.warning ? (measured ? '; long journey, discuss with the applicant' :
          '; possible long journey, confirm before discussing') : '';
        return `Person ${person.index}: ${detail}${warning}`;
      });
      (f.secondary_display || []).forEach(person => lines.push(`Person ${person.index}, other destination: ${
        person.status === 'MEASURED' ? routeMinutes(person.best_known_minutes) + ' minutes (rounded) by ' + (person.best_known_modes || []).join(' / ') :
          'travel time needs confirmation'} (not counted twice)`));
      if (f.majority) lines.unshift(`Closest ${f.majority.size} people: average ${f.majority.average_display} minutes (rounded display)`);
      // any measured journey time on this page comes from the journey provider, so its name sits beside the figures themselves
      const shown = !!f.majority || (f.people || []).some(person => person.fastest_minutes != null) || (f.secondary_display || []).some(person => person.status === 'MEASURED');
      return `${grounded(x)}<details class="small"><summary>Journey estimates for each person</summary>${lines.map(esc).join('<br>')}${shown ? '<br>' + GMAPS_ATTR : ''}</details>`;
    };
    // Live travel view only. The AI's checked report on this option's journeys (the sentence is written by the server from the
    // fields it verified, never the model's own words), then straight after it the Google Maps sources those
    // journeys came from (the provider's own titles and links, unaltered) and the provider's walking notice where it applies.
    const grounded = x => {
      const src = x.travel_sources;
      if (!src) return '';
      const ai = x.ai_travel;
      const links = (src.links || []).filter(s => /^https:\/\//.test(s.url || '')).map(s =>
        `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title)}</a>`);
      const measured = x.travel && x.travel.facts && ((x.travel.facts.people || []).some(person => person.fastest_minutes != null) ||
        (x.travel.facts.secondary_display || []).some(person => person.status === 'MEASURED'));
      if (!ai && !links.length && !measured) return '';
      return `<div class="small hb-grounded" data-role="hb-grounded">
        ${ai ? `<p data-role="hb-travel-ai-view"><b>AI assessment${ai.of > 1 ? ` (its choice ${ai.preference} of ${ai.of})` : ''}.</b> Travel: ${esc(ai.travel_rating_plain)}.
          ${(ai.strengths || []).length ? `<br>Strengths: ${ai.strengths.map(esc).join(' ')}` : ''}${(ai.weaknesses || []).length ? `<br>Weaknesses: ${ai.weaknesses.map(esc).join(' ')}` : ''}
          <br>Advice: ${esc(ai.advice_plain)}</p>
          <p data-role="hb-travel-ai-note"><b>Journey figures the AI reported.</b> ${esc(ai.note)}<br>
          Each person's own limit (${ai.stated_limit_minutes} minutes): ${ai.within_own_limit} within, ${ai.over_own_limit} over, ${ai.not_known} not known${ai.no_destination ? `, ${ai.no_destination} with no destination yet` : ''}.
          Group result (closest majority against the doubled limit of ${ai.doubled_limit_minutes} minutes): ${esc(ai.group_plain)}.</p>` : ''}
        <p data-role="hb-travel-sources">Sources: ${GMAPS_TEXT}${links.length ? ' · ' + links.join(' · ') : ''}</p>
        ${src.walking_warning ? `<p class="muted" data-role="hb-walk-warning">${esc(src.walking_warning)}</p>` : ''}</div>`;
    };
    const candRows = (list, label) => list && list.length ? `<h4 class="small">${label}</h4><div class="table-wrap"><table class="tbl"><thead><tr><th>Apartment</th><th>Type</th><th>Result</th><th>Offer (dates · whole-apartment rent)</th><th>To check / notes</th></tr></thead><tbody>
      ${list.map(x => `<tr><td>${esc(x.address)}<br><span class="small muted">${esc(x.suburb)}</span></td><td class="mono">${esc(x.type_code)}</td><td>${esc(x.class_plain)}${x.pareto_rank ? `<br><span class="small muted">beaten on every measured point by ${x.pareto_rank} listed option(s)</span>` : ''}</td>
        <td class="small">${esc(offer(x.offer))}${x.cheapest_offer && x.offer && x.cheapest_offer.total_weekly_rent !== x.offer.total_weekly_rent ? `<br><span class="muted">cheapest accepted dates: ${esc(offer(x.cheapest_offer))}</span>` : ''}</td>
        <td class="small">${[...(x.reasons || []), ...(x.notes || [])].map(esc).join('<br>') || '<span class="muted">—</span>'}${journeys(x)}</td></tr>`).join('')}</tbody></table></div>` : '';
    // the options are never "ready" from the rules check alone: the AI review decides, and until it has completed they are held
    const reviewLine = (key, cur) => {
      const r = cur.review || {};
      if (r.state === 'reviewed') return `<p class="small"><b>AI review complete.</b> ${esc(typeof r.detail === 'string' ? r.detail : (r.detail && r.detail.plain) || '')}</p>`;
      if (r.state === 'held') return `<p class="msg small" data-role="hb-review-held"><b>Held.</b> ${esc(r.plain)} <button class="btn btn--sm" type="button" data-action="hb-review-retry" data-scenario="${key}">Retry AI review</button></p>`;
      if (r.state === 'queued') return `<p class="small muted">Waiting for the AI review${r.queued_at ? ` (queued ${fmtDT(r.queued_at)})` : ''}; the options are held until it completes.</p>`;
      if (r.state === 'retrying') return `<p class="small muted">${esc(r.plain)}</p>`;
      return `<p class="small muted">${esc(r.plain || 'The AI review has not run yet; the options are held until it completes.')}</p>`;
    };
    const travelView = r => {
      if (!r) return '';
      if (!r.ok) return `<p class="msg small">${esc(r.plain)}</p>`;
      const k = r.check || {};
      const missed = (k.failures || 0) + (k.not_requested || 0);
      const counts = r.summary && r.summary.counts ? Object.entries(r.summary.counts).map(([n, v]) => `${v} ${n.replace(/_/g, ' ')}`).join(', ') : '';
      // the AI's assessment for this check (never saved): each option's assessment is shown in its own row, followed by its sources
      const ai = r.ai_reply || {};
      const notes = new Map((ai.state === 'ready' ? ai.options || [] : []).map(o => [o.apartment_id, o]));
      [...(r.listed || []), ...(r.alternatives || []), ...(r.unresolved || [])].forEach(x => { x.ai_travel = notes.get(x.apartment_id) || null; });
      const gate = r.ai_gate || {};
      return `<div data-role="hb-travel-result"><h4>Options with live travel times</h4>
        <p class="small">${GMAPS_ATTR}<br>Journey estimates. ${esc(k.basis || '')}. Checked ${fmtDT(k.checked_at)}.</p>
        ${gate.plain ? `<p class="${gate.held ? 'msg ' : ''}small" data-role="hb-travel-gate" data-state="${esc(gate.state || '')}"><b>${gate.held ? 'Held.' : 'AI-assessed.'}</b> ${esc(gate.plain)}</p>` : ''}
        ${ai.plain ? `<p class="small${ai.state === 'ready' ? '' : ' muted'}" data-role="hb-travel-ai" data-state="${esc(ai.state || '')}">${esc(ai.plain)}</p>` : ''}
        <p class="small muted">The apartments are chosen on dates, layout, price and features first. Travel is checked last, for the shortlisted apartments only; one that fails is replaced by the next and the replacement is checked. Never more than three apartments are shown here in all.</p>
        <p class="small muted">${esc(r.plain)}${counts ? ` All apartments, by result: ${esc(counts)}.` : ''}${r.summary && r.summary.travel_not_checked ? ` ${r.summary.travel_not_checked} of these were not shortlisted, so travel was not checked for them.` : ''}</p>
        ${missed ? `<p class="msg small">${missed} journey${missed === 1 ? '' : 's'} could not be checked this time; where a time is missing the option reads "Travel time needs confirmation".</p>` : ''}
        ${!k.requests ? '<p class="small muted">No journeys were needed: no one in the party has a place of work or study recorded yet.</p>' : ''}
        ${candRows(r.listed, 'Options to review (up to three)')}
        ${candRows(r.alternatives, 'Alternatives the party did not ask for (different area or one bathroom)')}
        ${candRows(r.unresolved, 'Could not be checked automatically — check by hand')}
        ${r.unresolved_not_shown ? `<p class="small muted">${r.unresolved_not_shown} more could not be checked automatically; they are in the list of other apartments below.</p>` : ''}
        ${!(r.listed || []).length && !(r.alternatives || []).length && !(r.unresolved || []).length ? '<p class="small">With these journey times, no apartment is within the party\'s travel limit.</p>' : ''}
        ${r.why_not && r.why_not.length ? `<details class="small"><summary>Why the other ${r.why_not.length} apartment(s) are not listed with these journey times</summary><ul>${r.why_not.map(x => `<li>${esc(x.address)} (${esc(x.type_code)}): ${esc(x.class_plain)}${[...x.reasons, ...(x.notes || [])].length ? ' — ' + [...x.reasons, ...(x.notes || [])].map(esc).join('; ') : ''}</li>`).join('')}</ul></details>` : ''}</div>`;
    };
    const travelBlock = (key, cur) => {
      if (!liveTravel.enabled || !cur || cur.stale) return '';
      const kept = travelKept(key, cur);
      return `<div class="card card--tight" data-role="hb-travel" data-scenario="${key}" data-shortlist="${cur.id}">
        <div class="actions"><button class="btn btn--sm" type="button" data-action="hb-travel" data-scenario="${key}">${kept && kept.reply ? 'Check travel times again' : 'Check travel times now'}</button></div>
        <p class="small muted">${esc(liveTravel.plain || '')}</p>
        <div data-role="hb-travel-out" aria-live="polite">${kept && kept.reply ? travelView(kept.reply) : ''}</div></div>`;
    };
    const scenario = (key, sc) => {
      const cur = sc.current;
      return `<div class="card card--tight" data-scenario="${key}"><h3>${esc(sc.label)}</h3>
        <p class="small"><b>${esc(sc.job.plain)}</b>${cur ? ` · ${esc(cur.status_plain)}` : ''}</p>
        ${cur ? reviewLine(key, cur) : ''}
        ${cur && cur.route_evidence ? `<p class="small">Journey estimates: <a href="https://maps.google.com/" target="_blank" rel="noopener noreferrer">Google Maps</a>. ${esc(cur.route_evidence.basis)}. Measured ${fmtDT(cur.route_evidence.measured_at)}; valid until ${fmtDT(cur.route_evidence.expires_at)}.</p>` : ''}
        ${cur && cur.stale ? `<p class="msg small"><b>Out of date.</b> ${esc(cur.stale.replace(/^Out of date: /, ''))} The options below are kept for reference only until the fresh run replaces them.</p>` : ''}
        ${travelBlock(key, cur)}
        ${cur ? `<p class="small muted">Prepared ${fmtDT(cur.created_at)} for business date ${cur.today}${cur.summary && cur.summary.counts ? ` · ${Object.entries(cur.summary.counts).map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ')}` : ''}</p>
          ${candRows(cur.listed, 'Options to review (up to three)')}
          ${candRows(cur.alternatives, 'Alternatives the party did not ask for (different area or one bathroom)')}
          ${candRows(cur.unresolved, 'Could not be checked automatically — check by hand')}
          ${cur.unresolved_not_shown ? `<p class="small muted">${cur.unresolved_not_shown} more could not be checked automatically; they are in the list of other apartments below.</p>` : ''}
          ${cur.why_not && cur.why_not.length ? `<details class="small"><summary>Why the other ${cur.why_not.length} apartment(s) are not listed</summary><ul>${cur.why_not.map(x => `<li>${esc(x.address)} (${esc(x.type_code)}): ${esc(x.class_plain)}${x.reasons.length ? ' — ' + x.reasons.map(esc).join('; ') : ''}</li>`).join('')}</ul></details>` : ''}` : ''}
        ${cur && !cur.stale && declineOptions(cur, key) ? `<details class="small mt"><summary>Record that the applicant declined one of these options</summary>
          <p class="small muted">Recorded against this list exactly as shown here (${esc(sc.label.toLowerCase())}, prepared ${fmtDT(cur.created_at)}). If the list has been replaced since this page loaded, nothing is recorded and the page refreshes.${liveTravel.enabled ? ' Live journey times are never saved: a decline of an apartment from the live travel view is recorded with the saved details for that apartment, where travel time still reads as needing confirmation.' : ''}</p>
          <div class="field"><label>Apartment<select name="hb-rej-apt-${key}"><option value="">Choose the apartment…</option>${declineOptions(cur, key)}</select></label></div>
          <div class="field"><label>Scope<select name="hb-rej-scope-${key}"><option value="apartment">This apartment only</option><option value="building">The whole building</option></select></label></div>
          <div class="field"><label>Applicant's reason (optional)<input name="hb-rej-reason-${key}" maxlength="1000"></label></div>
          <div class="actions"><button class="btn btn--sm" type="button" data-action="hb-reject" data-scenario="${key}" data-shortlist="${cur.id}">Record decline from this list</button></div></details>` : ''}
        <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="hb-rerun" data-scenario="${key}">Run again</button></div></div>`;
    };
    const declineOptions = (cur, key) => {
      const all = [...cur.listed, ...cur.alternatives, ...cur.unresolved];
      const saved = new Set(all.map(x => x.apartment_id));
      // an apartment offered from the live travel view can be declined too; the decline is still recorded against the saved list
      const live = key && liveTravel.enabled ? travelKept(key, cur) : null;
      if (live && live.reply && live.reply.ok) all.push(...live.reply.listed, ...live.reply.alternatives, ...live.reply.unresolved);
      const seen = new Set();
      return all.filter(x => !seen.has(x.apartment_id) && seen.add(x.apartment_id)).map(x => `<option value="${x.apartment_id}">${esc(x.address)}${saved.has(x.apartment_id) ? '' : ' (from the live travel view)'}</option>`).join('');
    };
    const planning = ov.planning;
    const rejections = ov.rejections || [];
    box.innerHTML = `<div class="card card--tight"><h3>Apartment options · rules check</h3>
      <p class="small muted">${esc(ov.travel_note)} Options here are a rules check for staff to review; nothing is sent to the applicant from this page.</p>
      <p class="msg" data-role="hb-msg"></p></div>
      ${Object.entries(ov.scenarios).map(([k, sc]) => scenario(k, sc)).join('')}
      ${e.party_mode === 'more_declined' ? `<div class="card card--tight"><h3>Planning budget for the final size</h3>
        <p class="small">${planning.applies ? `Confirmed: <b>${money(planning.budget_total)}/week</b> for ${planning.target_size} people, by ${esc(planning.confirmed_by)} on ${fmtDT(planning.confirmed_at)}.` : esc(planning.note || '')}</p>
        <div class="field"><label>Whole-apartment budget per week for ${e.headcount_target} people (agreed with the applicant)<input name="hb-planning" type="number" min="1" step="1" value="${planning.budget_total || ''}"></label></div>
        <div class="actions"><button class="btn btn--sm" type="button" data-action="hb-planning">Confirm planning budget</button></div>
        <p class="small muted">Changing the party's size or stated budget means the planning budget must be confirmed again.</p></div>` : ''}
      <div class="card card--tight"><h3>Declined by the applicant</h3>
        ${rejections.length ? `<ul class="small">${rejections.map(r => `<li><b>${r.scope === 'building' ? 'Whole building of ' : ''}${esc((r.offered_basis.apartment || {}).address || ('apartment ' + r.apartment_id))}</b> — ${r.status === 'active' ? 'declined' : 'reopened'} · recorded ${fmtDT(r.recorded_at)} by ${esc(r.recorded_by)}${r.customer_reason ? ` · "${esc(r.customer_reason)}"` : ''}
          <ul>${r.events.map(ev => `<li>${fmtDT(ev.at)} · ${esc(ev.staff)} · ${({ recorded: 'declined', reopened_customer: 'applicant asked to reconsider', reoffer_allowed: 'staff allowed one apartment to be offered again after a change' })[ev.event] || esc(ev.event)}${ev.note ? ` — ${esc(ev.note)}` : ''}${ev.change_basis && ev.change_basis.length ? ` (changed: ${ev.change_basis.map(d => esc(d.field.replace('requirements.', ''))).join(', ')})` : ''}</li>`).join('')}</ul>
          ${r.status === 'active' ? `<div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="hb-reopen" data-rid="${r.id}">Applicant asked to reconsider</button><button class="btn btn--ghost btn--sm" type="button" data-action="hb-reoffer" data-rid="${r.id}" data-aid="${r.apartment_id}" data-scope="${r.scope}">Allow re-offer after a change</button></div>` : ''}</li>`).join('')}</ul>` : '<p class="small muted">Nothing declined so far.</p>'}
        <p class="small muted">To record a decline, use "Record that the applicant declined one of these options" under the list the apartment was offered from, so the record says exactly which options the applicant saw. A decline removes the apartment (or building) from the options. It can be reopened if the applicant asks, or — for one apartment only — if its rent, dates or bedding have changed since; every step stays on record.</p></div>
      ${ov.versions && ov.versions.length ? `<details class="small"><summary>Requirement versions (${ov.versions.length})</summary><ul>${ov.versions.map(v => `<li>${fmtDT(v.created_at)} · ${esc(v.scenario_label)} · v${v.version} · ${esc(v.created_by)} — ${esc(v.change_note || '')}</li>`).join('')}</ul></details>` : ''}`;
    const m = box.querySelector('[data-role="hb-msg"]');
    const runTravel = async (key, asked) => {
      const holder = box.querySelector(`[data-role="hb-travel"][data-scenario="${key}"]`);
      const cur = ov.scenarios[key] && ov.scenarios[key].current;
      if (!holder || !cur) return;
      const out = holder.querySelector('[data-role="hb-travel-out"]'), btn = holder.querySelector('button[data-action="hb-travel"]');
      const kept = hbTravelShown.get(travelKey(key, cur)) || { at: 0, reply: null, busy: false };
      if (kept.busy) return;
      kept.busy = true; hbTravelShown.set(travelKey(key, cur), kept);
      btn.disabled = true; out.innerHTML = '<p class="small muted">Checking travel times… this can take up to a minute.</p>';
      try {
        kept.reply = await api.post(`/api/homebot/enquiries/${id}/options/travel-check`, { scenario: key });
        kept.at = Date.now();
        if (kept.reply.state === 'replaced' || kept.reply.state === 'out_of_date') { kept.reply = null; msg(m, 'The options changed, so travel was not checked. The page is refreshing.', false); setTimeout(render, 1200); return; }
        // dropped after 30 minutes even if this page is never opened or redrawn again
        const shownAt = kept.at;
        setTimeout(() => {
          if (kept.at !== shownAt) return;
          kept.reply = null;
          if (out.isConnected) { out.innerHTML = '<p class="small muted">The journey times shown earlier have been cleared. Check travel times again to see current ones.</p>'; btn.textContent = 'Check travel times now'; }
        }, HB_TRAVEL_KEEP_MS);
        if (!box.isConnected) return;
        out.innerHTML = travelView(kept.reply);
        btn.textContent = 'Check travel times again';
        // the decline list also offers the apartments shown with travel times (only that list is touched; nothing typed elsewhere is lost)
        const pick = box.querySelector(`[name="hb-rej-apt-${key}"]`);
        if (pick && kept.reply.ok) { const chosen = pick.value; pick.innerHTML = '<option value="">Choose the apartment…</option>' + declineOptions(cur, key); pick.value = chosen; }
      } catch (ex) {
        kept.reply = null;
        if (box.isConnected) out.innerHTML = `<p class="msg err small">${esc(asked ? ex.message : 'Travel times could not be checked just now. Use the button to try again.')}</p>`;
      } finally { kept.busy = false; if (btn.isConnected) btn.disabled = false; }
    };
    // when the options open, check travel once for each list (not again on every redraw; the button checks again on request)
    if (liveTravel.auto) Object.entries(ov.scenarios).forEach(([key, sc]) => {
      if (sc.current && !sc.current.stale && !hbTravelShown.has(travelKey(key, sc.current))) runTravel(key, false);
    });
    box.addEventListener('click', async ev => {
      const b = ev.target.closest('button[data-action]'); if (!b) return;
      const a = b.dataset.action;
      try {
        if (a === 'hb-review-retry') { const r = await api.post(`/api/homebot/enquiries/${id}/options/review-retry`, { scenario: b.dataset.scenario }); msg(m, r.outcome === 'enqueued' || r.outcome === 'already_queued' ? 'The AI review has been asked for again; the options stay held until it completes.' : 'The AI review could not be queued just now; the options stay held. Try again shortly.', r.outcome === 'enqueued' || r.outcome === 'already_queued'); setTimeout(render, 1500); }
        if (a === 'hb-travel') { await runTravel(b.dataset.scenario, true); return; }
        if (a === 'hb-rerun') { await api.post(`/api/homebot/enquiries/${id}/options/rerun`, { scenario: b.dataset.scenario }); msg(m, 'A fresh run has been queued; this page updates when it is ready.'); setTimeout(render, 1500); }
        if (a === 'hb-planning') { await api.post(`/api/homebot/enquiries/${id}/planning`, { budget_total: box.querySelector('[name="hb-planning"]').value }); msg(m, 'Planning budget confirmed; provisional options for the final size are being prepared.'); setTimeout(render, 1500); }
        if (a === 'hb-reject') {
          const key = b.dataset.scenario, sid = +b.dataset.shortlist;
          const aid = box.querySelector(`[name="hb-rej-apt-${key}"]`).value; if (!aid) { msg(m, 'Choose the apartment first.', false); return; }
          // stale page: the list shown here must still be the current one for this scenario, otherwise nothing is recorded
          const now = await api.get(`/api/homebot/enquiries/${id}/options`);
          const live = now.scenarios && now.scenarios[key] && now.scenarios[key].current;
          if (!live || live.id !== sid || live.stale) { msg(m, 'The options shown here have been replaced since this page loaded, so the decline was not recorded. The page has refreshed; check the apartment is still among the options and record it again.', false); setTimeout(render, 1200); return; }
          b.disabled = true;
          await api.post(`/api/homebot/enquiries/${id}/rejections`, { apartment_id: +aid, shortlist_id: sid, scenario: key, scope: box.querySelector(`[name="hb-rej-scope-${key}"]`).value, customer_reason: box.querySelector(`[name="hb-rej-reason-${key}"]`).value });
          msg(m, 'Decline recorded against the list shown; the options are being prepared again without it.'); setTimeout(render, 1500);
        }
        if (a === 'hb-reopen') { const note = prompt('Why is this being reopened? (what the applicant said)'); if (!note) return; await api.post(`/api/homebot/rejections/${b.dataset.rid}/reopen`, { note }); msg(m, 'Reopened at the applicant\'s request.'); setTimeout(render, 1500); }
        if (a === 'hb-reoffer') {
          let aid = b.dataset.aid;
          if (b.dataset.scope === 'building') { aid = prompt('Which apartment in that building has changed? Enter its number from the options list (the address shows it as Unit n / …).', aid); if (!aid) return; }
          const d = await api.get(`/api/homebot/rejections/${b.dataset.rid}/material-change?apartment_id=${encodeURIComponent(aid)}`);
          if (!d.changes.length) { msg(m, 'Nothing material has changed for that apartment since it was declined (rent, dates, bedding or the party\'s requirements), so it cannot be re-offered on that basis.', false); return; }
          const note = prompt(`Changed since the decline: ${d.changes.map(x => `${x.field.replace('requirements.', '')} ${x.was ?? '—'} → ${x.now ?? '—'}`).join('; ')}. Note for the record:`);
          if (!note) return;
          await api.post(`/api/homebot/rejections/${b.dataset.rid}/reoffer`, { apartment_id: +aid, note }); msg(m, 'Re-offer allowed for that apartment only; the options are being prepared again.'); setTimeout(render, 1500);
        }
      } catch (ex) { msg(m, ex.message, false); }
    });
  }

  const Q = state_q => `<span class="q">${state_q}</span>`;
  const coupleRefOptions = (e, sel) => [['lead', `${e.first_name} (enquirer)`], ...e.party_members.map((m, i) => [`m${i + 1}`, m.name || `Person ${i + 2}`])].map(([v, t]) => `<option value="${v}" ${v === sel ? 'selected' : ''}>${esc(t)}</option>`).join('');

  // GP14 (MateBot): record that this person declined an apartment / a whole building; history, reopening and the one re-offer
  // permission after a material change. Everything printed here is escaped; actions call the server, never the matcher directly.
  async function matebotRejections(box, id) {
    const data = await api.get(`/api/enquiries/${id}/apartment-rejections`);
    const items = data.items || [], offered = data.offered || [];
    const seen = new Set();
    const options = offered.filter(o => !seen.has(o.apartment_id) && seen.add(o.apartment_id))
      .map(o => `<option value="${o.apartment_id}">${esc(o.address)} (${esc(o.type_code || '')}${o.is_current ? ', current for Group ' + o.group_id : ', listed for Group ' + o.group_id})</option>`).join('');
    const EVENT = { recorded: 'declined', reopened_by_customer: 'the person asked to reconsider', material_change_reoffer: 'staff allowed one apartment to be offered again after a change' };
    const item = r => `<li class="rej-item"><b>${r.scope === 'building' ? 'Whole building of ' : ''}${esc(r.address || ('apartment ' + r.apartment_id))}</b>${r.suburb ? ` <span class="small muted">${esc(r.suburb)}</span>` : ''}
        — ${r.status === 'active' ? 'declined' : 'reopened'} · recorded by ${esc(r.recorded_by)} on ${fmtDT(r.recorded_at)}${r.customer_reason ? `<br><span class="small">Reason given: ${esc(r.customer_reason)}</span>` : ''}
        ${r.reoffer_permitted_apartments.length && r.status === 'active' ? `<br><span class="small">Re-offer permitted for apartment ${r.reoffer_permitted_apartments.map(esc).join(', ')} — pending the next matching run's normal checks; nothing is offered until that run lists it.</span>` : ''}
        <ul class="small muted">${r.events.map(ev => `<li>${fmtDT(ev.at)} · ${esc(ev.staff)} · ${EVENT[ev.event] || esc(ev.event)}${ev.note ? ` — ${esc(ev.note)}` : ''}</li>`).join('')}</ul>
        ${r.status === 'active' ? `<div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="mb-reopen" data-rid="${r.id}">Person asked to reconsider</button>
          <button class="btn btn--ghost btn--sm" type="button" data-action="mb-reoffer" data-rid="${r.id}" data-aid="${r.apartment_id || ''}" data-scope="${r.scope}">Allow re-offer after a change</button></div>` : ''}</li>`;
    box.innerHTML = `<h3>Apartments this person declined</h3>
      <p class="msg" data-role="mb-rej-msg"></p>
      ${items.length ? `<ul class="small rej-list">${items.map(item).join('')}</ul>` : '<p class="small muted">Nothing recorded. A decline only ever comes from the person (or someone speaking for them); staff record it here.</p>'}
      ${options ? `<details class="small mt"><summary>Record that this person declined one of the apartments listed for them</summary>
        <p class="small muted">The decline removes that apartment (or the whole building) from this person's options in every group they are in; nothing else about the matching changes. If it was the current apartment of a group, the group moves to its next listed apartment that nobody has declined, or waits with no current offer.</p>
        <div class="field"><label>Apartment<select name="mb-rej-apt"><option value="">Choose the apartment…</option>${options}</select></label></div>
        <div class="field"><label>Scope<select name="mb-rej-scope"><option value="apartment">This apartment only</option><option value="building">The whole building</option></select></label></div>
        <div class="field"><label>The person's reason (optional, their words)<input name="mb-rej-reason" maxlength="500"></label></div>
        <div class="actions"><button class="btn btn--sm" type="button" data-action="mb-reject">Record the decline</button></div></details>`
      : '<p class="small muted">A decline can be recorded once this person has a group with listed apartments.</p>'}`;
    const m = box.querySelector('[data-role="mb-rej-msg"]');
    if (box.rejClick) box.removeEventListener('click', box.rejClick);
    box.rejClick = async ev => {
      const b = ev.target.closest('button[data-action]'); if (!b) return;
      ev.stopPropagation();
      const a = b.dataset.action;
      try {
        if (a === 'mb-reject') {
          const aid = box.querySelector('[name="mb-rej-apt"]').value; if (!aid) { msg(m, 'Choose the apartment first.', false); return; }
          const o = offered.find(x => String(x.apartment_id) === aid) || {};
          b.disabled = true;
          await api.post(`/api/enquiries/${id}/apartment-rejections`, { apartment_id: +aid, scope: box.querySelector('[name="mb-rej-scope"]').value, customer_reason: box.querySelector('[name="mb-rej-reason"]').value, group_id: o.group_id || null });
          msg(m, 'Decline recorded.'); setTimeout(render, 900);
        }
        if (a === 'mb-reopen') { const note = prompt('What did the person say? (kept with the record)'); if (!note) return; await api.post(`/api/apartment-rejections/${b.dataset.rid}/reopen`, { note }); msg(m, 'Reopened at the person\'s request; the apartment is an option again from the next matching run.'); setTimeout(render, 900); }
        if (a === 'mb-reoffer') {
          let aid = b.dataset.aid;
          if (b.dataset.scope === 'building' || !aid) { aid = prompt('Which apartment in that building has changed? Enter its number (shown in the apartment list).', aid); if (!aid) return; }
          const d = await api.get(`/api/apartment-rejections/${b.dataset.rid}/material-change?apartment_id=${encodeURIComponent(aid)}`);
          if (!d.any) { msg(m, 'Nothing material has changed for that apartment or this person\'s requirements since the decline, so it cannot be offered again on that basis.', false); return; }
          const lines = [...Object.entries(d.changes || {}).map(([k, v]) => `${k}: ${v[0] ?? '—'} → ${v[1] ?? '—'}`), ...Object.entries(d.enquiry_changes || {}).map(([k, v]) => `your requirement ${k}: ${v[0] ?? '—'} → ${v[1] ?? '—'}`)];
          const note = prompt(`Changed since the decline: ${lines.join('; ')}. Note for the record:`); if (!note) return;
          await api.post(`/api/apartment-rejections/${b.dataset.rid}/reoffer`, { apartment_id: +aid, note });
          msg(m, 'Re-offer permitted for that apartment only. Nothing is offered yet: the next matching run checks it like any other option and lists it only if it still passes.'); setTimeout(render, 900);
        }
      } catch (ex) { msg(m, ex.message, false); b.disabled = false; }
    };
    box.addEventListener('click', box.rejClick);
  }

  async function enquiryDetail(p, id) {
    const e = await api.get(`/api/enquiries/${id}`);
    setDataset(e.is_test ? 'test' : 'real');
    const qt = state.ref.question_types;
    // GAP-T4c v2: the practical facts the AI review was given from the person's own free text (plain labels, typed values, the
    // person's own words as the supporting quote) and the 'unreviewed note' marker. Staff/audit view only; nothing here is a rule.
    const notesValue = v => {
      if (!v || typeof v !== 'object') return '';
      const parts = [];
      if (v.scope) parts.push({ interior: 'inside the home', whole_home: 'the whole home', any_smoker: 'no smokers at all' }[v.scope] || '');
      if (v.quiet_from || v.quiet_until) parts.push(`quiet ${v.quiet_from || '…'} – ${v.quiet_until || '…'}`);
      if (v.pattern) parts.push(v.pattern + ' hours');
      if (v.from || v.until) parts.push(`${v.from || '…'} – ${v.until || '…'}`);
      if (v.frequency) parts.push(v.frequency + ' guests');
      if (v.comparator && v.date) parts.push(`${{ on_or_before: 'on or before', on_or_after: 'on or after', exactly: 'exactly' }[v.comparator] || v.comparator} ${fmtD(v.date)}`);
      if (v.mode) parts.push(v.mode === 'any' ? 'public transport' : v.mode);
      if (v.field && v.answer) parts.push(`${v.field}: ${v.answer}`);
      return parts.filter(Boolean).join(', ');
    };
    const notesReviewHtml = nr => {
      if (!nr) return '<p class="small muted">Not available for this record.</p>';
      const head = `<p class="small">${nr.status === 'ok' ? '' : nr.status === 'none' ? '' : '<span class="badge badge--warn">Not yet read</span> '}${esc(nr.status_label || '')}${nr.updated_at ? ` <span class="muted">(${fmtDT(nr.updated_at)})</span>` : ''}</p>`;
      const marker = nr.unreviewed_note ? `<p class="small warn-text"><span class="badge badge--warn">Unreviewed note</span> Part of what this person wrote could not be read into the review${nr.unresolved_count ? ` (${nr.unresolved_count} item${nr.unresolved_count === 1 ? '' : 's'})` : ''}. Read the answers above yourself before introducing.</p>` : '';
      const takeover = nr.lead_answers_unverified_at ? `<p class="small warn-text"><span class="badge badge--warn">Former enquirer's answers</span> The study/work, lifestyle and free-text answers on this record were written by the person who filled in the form before the enquiry changed hands (${esc(fmtDT(nr.lead_answers_unverified_at))}). They are left out of every review, brief and introduction until the team confirms them with the current enquirer. Speak with them, correct any answer that is not theirs (edit the record first if needed), then choose one:</p>
        <p class="small"><button class="btn btn--small" type="button" data-action="confirm-lead-answers">These answers are the current enquirer's</button> <button class="btn btn--small btn--ghost" type="button" data-action="unrecord-lead-answers">Not theirs - record as not given</button></p>
        <p class="small muted">"Not given" blanks the study/work, lifestyle and free-text answers on this record (the form answers made for the whole party stay). This is a data check, not a consent step: the sharing consent given on the original form is unchanged.</p>` : '';
      const items = (nr.items || []);
      const list = items.length ? `<ul class="small">${items.map(it => `<li><strong>${esc(it.label)}</strong>${it.strength && it.strength !== 'stated' ? ` · ${esc(it.strength)}` : ''}${notesValue(it.value) ? ` · ${esc(notesValue(it.value))}` : ''}${it.in_review ? '' : it.polarity !== 'affirmed' ? ` · <span class="muted">not a current need (${esc(it.polarity)}); not given to the review</span>` : ' · <span class="muted">about someone else; not given to the review</span>'}<br><span class="muted">Their words (${esc(it.field === 'else_text' ? 'anything else' : it.field)}): “${esc(it.quote || '')}”</span></li>`).join('')}</ul>`
        : (nr.status === 'ok' ? '<p class="small muted">Nothing practical was read from the notes.</p>' : '');
      return head + takeover + list + marker + (items.length ? `<p class="small muted">Self-reported and unverified: these are discussion points for the review and briefs, never a filter or a weight.</p>` : '');
    };
    const kv = (label, val, q) => `<dt>${label}${q ? ` <span class="q">${q} · ${qt[q] || ''}</span>` : ''}</dt><dd>${val == null || val === '' || (Array.isArray(val) && !val.length) ? '<span class="muted">—</span>' : esc(Array.isArray(val) ? val.join(', ') : val)}</dd>`;
    p.innerHTML = `
      <div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-role="back">← Enquiries</button><h1>${esc(e.first_name)} ${esc(e.last_name)} ${e.is_test ? '<span class="badge badge--test">TEST</span>' : ''}</h1>${badge(e.status)} ${barredBadge(e)} ${adultsBadge(e)}<span class="spacer"></span>
        <span class="small muted">MateBot-${String(e.id).padStart(5, '0')} · ${esc(e.channel)} · enquired ${fmtDT(e.created_at)} · last contact ${fmtDT(e.last_contact_at)}</span></div>
      <div class="detail detail--2">
        <div>
          <div class="card card--tight"><h3>Status and staff fields</h3>
            <div class="editgrid">
              <div class="field"><label>Status<select name="status">${state.ref.statuses.map(s => `<option ${e.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
              <div class="field"><label>Campaign<input name="campaign" value="${esc(e.campaign || '')}"></label></div>
              <div class="field"><label>Staff member<select name="staff_member"><option value="">—</option>${state.ref.staff_list.map(s => `<option ${e.staff_member === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
            </div>
            <div class="field mt"><label>Staff notes<textarea name="staff_notes" rows="2">${esc(e.staff_notes || '')}</textarea></label></div>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="save-staff">Save</button>
              <button class="btn btn--ghost btn--sm" type="button" data-action="contact" title="Resets the 12-month retention clock">Log contact</button>
              <button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="delete">Delete enquiry</button></div>
            <p class="msg" data-role="msg1"></p>
          </div>
          <div class="card card--tight mt"><h3>18 or older at move-in (everyone in the group)</h3>
            <p class="small">${e.adults_status === 'confirmed' ? '' : '<span class="badge badge--warn">To confirm</span> '}${adultsText(e)}</p>
            <p class="small muted">Only a person can make this confirmation: the enquirer on the form, or a staff member who has checked with them. Nothing here is worked out from other answers.</p>
            <div class="actions">${e.adults_status === 'confirmed'
              ? '<button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="adults-withdraw">Withdraw confirmation</button>'
              : '<button class="btn btn--sm" type="button" data-action="adults-confirm">Record: confirmed with the enquirer</button>'}</div>
            <p class="msg" data-role="msgadults"></p>
          </div>
          <div class="card card--tight mt"><h3>Bring a mate</h3>
            ${e.linked_friends.length ? `<p class="small">${e.linked_root_id === e.id
                ? `<b>${esc(e.first_name)} is bringing ${e.linked_friends.length === 1 ? 'a friend' : e.linked_friends.length + ' friends'}:</b> ${e.linked_friends.map(f => `<a href="#/staff/enquiries/${f.id}">${esc(f.first_name)} ${esc(f.last_name)}</a> (${esc(f.status)})`).join(', ')}. They are always kept in the same group.`
                : `<b>Joined through ${esc((e.linked_friends.find(f => f.role === 'enquirer') || {}).first_name || 'another enquirer')}'s link.</b> Kept together with ${e.linked_friends.map(f => `<a href="#/staff/enquiries/${f.id}">${esc(f.first_name)} ${esc(f.last_name)}</a>${f.role === 'enquirer' ? ' (sent the link)' : ''}`).join(', ')}.`}</p>`
              : `<p class="small muted">No friends linked yet. Friends who use ${esc(e.first_name)}'s share link fill in their own form and are always kept in the same group.</p>`}
            ${e.mate_note ? `<p class="small muted">${esc(e.mate_note)}</p>` : ''}
            ${e.mate_requests.length ? `<p class="msg"><span class="badge badge--flag">Waiting for approval</span> ${e.mate_requests.map(r => `${esc(r.note || '')} <a href="#/staff/groups/${r.group_id}">Open Group ${r.group_id}</a>`).join(' ')}</p>` : ''}
            ${['Leased', 'Withdrawn'].includes(e.status) || e.barred_at ? '' : `<div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="mate-link">${e.mate_link ? 'Show share link' : 'Create a share link to give them'}</button></div><p class="small mono" data-role="mateLinkOut" hidden></p>`}
            <p class="msg" data-role="msgmate"></p>
          </div>
          <div class="card card--tight mt ${e.barred_at ? 'card--barred' : ''}"><h3>Bar from matching</h3>
            ${(e.barred_people || []).length ? `<p class="msg warn"><b>Barred from this form:</b> ${e.barred_people.map(b => `${esc(b.name)}${b.was_lead ? ' (filled in the form)' : ''}, ${fmtDT(b.barred_at)} by ${esc(b.barred_by || 'staff')}: ${esc(b.reason)}`).join('; ')}. The rest of the party chose to carry on without them.</p>` : ''}
            ${e.barred_at ? `<p class="msg err"><b>${esc(e.barred_person || e.first_name)} is barred from matching</b> since ${fmtDT(e.barred_at)} by ${esc(e.barred_by || 'staff')}: ${esc(e.barred_reason || '')}</p>
              <p class="small muted">${e.party && e.party !== 'Alone' ? `The ${e.party_size - 1 === 1 ? 'other person' : 'other ' + (e.party_size - 1) + ' people'} on this form ${e.party_size - 1 === 1 ? 'is' : 'are'} <b>not</b> barred: they chose to leave with ${esc(e.barred_person || e.first_name)}, so this form is out of matching while the bar stands. If they change their mind, lift the bar and bar ${esc(e.barred_person || e.first_name)} again choosing “they stay”. ` : ''}While barred, ${esc(e.barred_person || e.first_name)} is left out of every matching run and every replacement. Lifting the bar includes ${e.party && e.party !== 'Alone' ? 'the form' : 'them'} in the next matching run again; earlier rejections still stand.</p>
              <div class="actions"><button class="btn btn--sm" type="button" data-action="lift-bar">Lift bar</button></div>`
            : `<p class="small muted">A rejected person is only kept apart from the members who rejected them; they can still be grouped with others. Barring goes further: the barred person is left out of every run and every replacement until the bar is lifted. Barring one person never bars the people they came with, on the same form or through a share link: they choose for themselves whether to leave with the barred person or carry on. A reason is required and is kept in the audit log.${e.groups.some(g => g.status === 'Proposed' && g.is_primary) ? ' Their proposed group is kept: they are removed and the AI-chosen replacement is applied at once; if none passes every rule the group waits for one.' : ''}${e.groups.some(g => ['Introduced', 'Group confirmed', 'Awaiting replacement'].includes(g.status)) ? ' Their introduced or confirmed group is kept: they are removed, the group waits for a replacement and the AI\'s ranked find is flagged for you to accept or decline.' : ''}</p>
              ${e.party && e.party !== 'Alone' && e.party_members.length ? `<div class="field"><label>Who is barred?<select name="bar_person"><option value="lead">${esc(e.first_name)} ${esc(e.last_name)} (filled in the form)</option>${e.party_members.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join('')}</select></label></div>
              <fieldset class="field"><legend>The ${e.party_members.length === 1 ? 'other person' : 'other people'} on this form (${e.party_members.map(m => esc(m.name)).join(', ')}, ${esc(e.first_name)}) chose to:</legend>
                <label class="pill"><input type="radio" name="bar_party_choice" value="stay"> Carry on without the barred person (they are taken off this form; if the form-filler is barred, the first other person takes over the enquiry)</label>
                <label class="pill"><input type="radio" name="bar_party_choice" value="leave"> Leave with the barred person (the form is out of matching, but only the barred person is barred)</label></fieldset>` : ''}
              ${e.linked_friends.length && e.groups.some(g => ['Proposed', 'Introduced', 'Group confirmed', 'Awaiting replacement'].includes(g.status)) ? `<fieldset class="field"><legend>The friends linked to ${esc(e.first_name)} (${e.linked_friends.map(f => esc(f.first_name)).join(', ')}) chose to:</legend>
                <label class="pill"><input type="radio" name="bar_friends_choice" value="stay"> Stay in their group without ${esc(e.first_name)}</label>
                <label class="pill"><input type="radio" name="bar_friends_choice" value="leave"> Leave the group with ${esc(e.first_name)} and be grouped again together (they are not barred)</label></fieldset>` : ''}
              <div class="field"><label>Reason (required)<input name="bar_reason" placeholder="e.g. abusive to staff; duplicate enquiry; asked not to be matched"></label></div>
              <div class="actions"><button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="bar">Bar from matching</button></div>`}
            <p class="msg" data-role="msgbar"></p>
          </div>
          <div class="card card--tight mt"><h3>Edit answers</h3>
            <div class="editgrid">
              ${[['first_name', 'First name', 'text'], ['last_name', 'Last name', 'text'], ['email', 'Email', 'email'], ['phone', 'Mobile', 'text'], ['move_in', 'Move-in', 'date'], ['move_out', 'Move-out', 'date']]
                .map(([k, l, t]) => `<div class="field"><label>${l}<input name="${k}" type="${t}" value="${esc(e[k] || '')}"></label></div>`).join('')}
              <div class="field"><label>Country of birth ${e.birth_country_confirmed ? '' : '<span class="badge badge--warn">confirm</span>'}<input name="birth_country" value="${esc(e.birth_country || '')}" list="countries-dl"></label><span class="small muted">Typed: “${esc(e.birth_country_raw)}”</span></div>
              <div class="field"><label>Budget option<select name="budget_option">${state.ref.price_guide[e.budget_guide].map(o => `<option value="${o.id}" ${e.budget_option === o.id ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select></label></div>
              <div class="field"><label>Max housemates<select name="mate_max">${[1, 2, 3, 4, 5, 6].map(n => `<option ${e.mate_max === n ? 'selected' : ''}>${n}</option>`).join('')}</select></label></div>
              <div class="field"><label>Bunk bed OK<select name="bunk_ok"><option value="1" ${e.bunk_ok ? 'selected' : ''}>Yes</option><option value="0" ${!e.bunk_ok ? 'selected' : ''}>No</option></select></label></div>
              <div class="field"><label>Smokes<select name="smoke">${['No', 'Yes, outside only (balcony or courtyard)'].map(s => `<option ${e.smoke === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
              <div class="field"><label>OK with outside smoker<select name="smoke_ok">${['Yes', 'No'].map(s => `<option ${e.smoke_ok === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
            </div>
            <datalist id="countries-dl">${state.meta.countries.map(c => `<option value="${esc(c)}">`).join('')}</datalist>
            <p class="sublabel">Features wanted <span class="small muted">(3.11, wishes only: never a filter)</span></p>
            <div class="chips" data-role="editFeatures">${(state.ref.features || []).map(f => `<label class="chip"><input type="checkbox" name="edit_features" value="${esc(f.label)}" ${(e.features || []).includes(f.label) ? 'checked' : ''}>${esc(f.label)}</label>`).join('')}</div>
            <p class="sublabel">Matter most <span class="small muted">(3.12, up to ${state.ref.features_max_top || 3} of the ticked features)</span></p>
            <div class="chips" data-role="editFeaturesTop">${(state.ref.features || []).map(f => `<label class="chip" ${(e.features || []).includes(f.label) ? '' : 'hidden'}><input type="checkbox" name="edit_features_top" value="${esc(f.label)}" ${(e.features_top || []).includes(f.label) ? 'checked' : ''}>${esc(f.label)}</label>`).join('')}</div>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="save-answers">Save answers</button>${e.birth_country_confirmed ? '' : '<button class="btn btn--ghost btn--sm" type="button" data-action="confirm-country">Confirm country as shown</button>'}</div>
            <h4 class="mt">Introduction details</h4>
            <p class="small muted">Shared in introductions to proposed housemates; not used for matching. Optional: record only what the person told you, as they said it. Saving changes nothing about the group or the adults declaration, but any introduction already drafted is marked for re-drafting.</p>
            ${e.intro_profile_status === 'incomplete' ? `<p class="small warn-text">Not yet given by: ${esc(e.intro_missing.join(', '))}. Ask before introducing; MateBot AI never fills this in.</p>` : ''}
            <div class="editgrid" data-role="introEdit">${introEdit('lead_', e)}</div>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="save-intro">Save introduction details</button></div>
            ${e.party_members.length ? `<h4 class="mt">Other people in the party</h4>
            ${e.party_members.map(m => `<div class="field member-row" data-member="${m.id}">
              <label>${esc(m.name)}: country of birth ${m.birth_country_raw && !m.birth_country_confirmed ? '<span class="badge badge--warn">confirm</span>' : ''}<input name="member_country" value="${esc(m.birth_country || '')}" list="countries-dl"></label>
              <span class="small muted">Typed: “${esc(m.birth_country_raw || '—')}”</span>
              <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="confirm-member" data-member="${m.id}">${m.birth_country_confirmed ? 'Save country' : 'Confirm country'}</button></div>
              <p class="sublabel">Introduction details for ${esc(m.name)} <span class="small muted">(their own answer; not used for matching)</span></p>
              <div class="editgrid">${introEdit(`m${m.id}_`, m)}</div>
              <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="save-member-intro" data-member="${m.id}">Save introduction details</button></div>
            </div>`).join('')}` : ''}
            ${e.party === 'With friends' ? `<h4 class="mt">Couple in the group</h4>
            <div class="field"><label>Is anyone in the group a couple who will share one bed? <select name="couples_answer"><option value="" ${!e.couples_answer ? 'selected' : ''}>Not asked yet</option><option ${e.couples_answer === 'No' ? 'selected' : ''}>No</option><option ${e.couples_answer === 'Yes' ? 'selected' : ''}>Yes</option></select></label>
              <div class="couple-rows mt" data-role="coupleEdit">${(e.couples_answer === 'Yes' && (e.couples || []).length ? e.couples.map(c => c.refs) : [['', '']]).map((pr, i) => `<div class="couple-row" data-cindex="${i}"><span class="party-row__title">Couple ${i + 1}</span>${['a', 'b'].map((k, j) => `<label class="party-cell"><small>${j ? 'Second' : 'First'} person</small><select data-cp="${k}"><option value="">Select…</option>${coupleRefOptions(e, pr[j])}</select></label>`).join('')}</div>`).join('')}</div>
              <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="add-couple">Add another couple</button><button class="btn btn--sm" type="button" data-action="save-couples">Save couple answer</button></div>
              <p class="small muted">Only the enquirer's answer creates a couple; MateBot AI never assumes one. Record what they told you by phone.</p></div>` : ''}
            <p class="msg" data-role="msg2"></p>
          </div>
        </div>
        <div>
          <div class="card card--tight"><h3>All answers</h3>
            <dl class="kv">
              ${kv('Contact', `${e.phone_code || ''} ${e.phone || ''} · ${e.contact_method || ''}`, '1.4')}
              ${kv('Country of birth', e.birth_country || e.birth_country_raw, '1.8')}${kv('Lives in', e.live_country || e.live_country_raw, '1.9')}${kv('Languages', e.languages, '1.10')}
              ${kv('For introductions only (sex, current age)', [`${e.first_name}: ${introText(e)}`, ...e.party_members.map(m => `${m.name}: ${introText(m)}`)].concat(e.intro_profile_status === 'incomplete' ? ['Not given by everyone: ask before introducing'] : []), '1.6')}
              ${kv('City', e.city, '2.1')}${kv('Move in', `${fmtD(e.move_in)} (${e.move_in_flex})`, '2.2')}${kv('Move out', `${fmtD(e.move_out)} (${e.move_out_flex})`, '2.4')}
              ${kv('Budget', `${e.budget_option} · max ${money(e.budget_max)}/wk (${e.budget_guide} guide)`, '3.1')}${kv('Apartment sizes', e.apt_sizes, '3.2')}${kv('Housemates ideal / max', `${e.mate_ideal} / ${e.mate_max}`, '3.3')}
              ${kv('Room arrangements', e.rooms, '3.5')}${kv('Bunk bed OK', e.bunk_ok ? 'Yes' : 'No', '3.6')}${kv('First choice', e.room_first, '3.7')}${kv('Enquiring as', e.party + (e.party_size > 1 ? ` (${e.party_size} people)` : ''), '3.8')}
              ${e.party === 'With friends' ? kv('Couple in the group', e.couples_answer === 'Yes' ? `Yes: ${(e.couples || []).map(c => c.names.join(' & ')).join('; ')}` : e.couples_answer === 'No' ? 'No' : 'Not asked (saved before the couples question existed): treated as no couple until staff confirm', '3.10') : ''}
              ${e.party_members.length ? kv('Other people', e.party_members.map(m => `${m.name} <${m.email}> · born in ${m.birth_country || (m.birth_country_raw ? `“${m.birth_country_raw}”` : 'country not recorded')}${m.birth_country_raw && !m.birth_country_confirmed ? ' (confirm below)' : ''}`), '3.9') : ''}
              ${kv('Doing', e.doing, '4.1')}${kv('Study', [e.school, e.school_other, e.course, e.level].filter(Boolean).join(' · '), '4.2')}${kv('Course dates', e.course_start ? `${fmtD(e.course_start)} → ${fmtD(e.course_end)}` : '', '4.4')}${kv('Work', e.work, '4.5')}${kv('Preferred areas', e.areas, '4.6')}${kv('Travel choices', (e.travel_modes || []).length ? e.travel_modes : 'Not answered', '4.7')}${kv('Max travel', e.travel, '4.7')}
              ${kv('Features wanted (wishes, not filters)', (e.features || []).length ? e.features : 'None ticked', '3.11')}${kv('Matter most', (e.features_top || []).length ? e.features_top : 'None chosen', '3.12')}
              ${kv('Smokes', e.smoke, '5.5')}${kv('OK with outside smoker', e.smoke_ok, '5.6')}${kv('Routine / tidy / social / guests / diet', [e.routine, e.tidy, e.social, e.guests, e.diet].map(x => x || '—').join(' / '), '5.7')}
              ${kv('Anything else', e.else_text, '5.12')}${kv('Introduction', e.intro, '5.13')}
              ${kv('Heard about us', e.source, '6.1')}${kv('Consents', `store ${e.consent_store ? '✓' : '✗'} · share ${e.consent_share ? '✓' : '✗'} · marketing ${e.consent_marketing ? '✓' : '✗'}`, '6.2')}
              ${kv('Everyone 18+ at move-in', e.adults_status === 'confirmed' ? `Yes: ${adultsText(e)}` : e.adults_status === 'renew' ? 'To renew (see the card on the left)' : 'Not yet declared (see the card on the left)', '6.4')}
            </dl>
          </div>
          <div class="card card--tight mt"><h3>What the review read from this person's notes</h3>${notesReviewHtml(e.notes_review)}</div>
          <div class="card card--tight mt"><h3>Groups</h3>
            ${e.groups.length ? `<ul class="small">${e.groups.map(g => `<li><a href="#/staff/groups/${g.id}">Group ${g.id}</a> ${g.status === 'Proposed' && !g.is_primary ? '<span class="badge badge--alt">alternative</span>' : ''} · ${esc(g.address)} · ${badge(g.status)} · share ${money(g.weekly_share)}/wk</li>`).join('')}</ul><p class="small muted">Only one proposed group is offered to a person at a time; alternatives are for staff reference.</p>` : '<p class="small muted">Not in any proposed group yet.</p>'}
          </div>
          <div class="card card--tight mt" data-role="mb-rejections"><h3>Apartments this person declined</h3><p class="small muted">Loading…</p></div>
        </div>
      </div>`;
    p.querySelector('[data-role="back"]').addEventListener('click', () => navigate('enquiries'));
    matebotRejections(p.querySelector('[data-role="mb-rejections"]'), id).catch(ex => { p.querySelector('[data-role="mb-rejections"]').innerHTML = `<h3>Apartments this person declined</h3><p class="msg err">${esc(ex.message)}</p>`; });
    const m1 = p.querySelector('[data-role="msg1"]'), m2 = p.querySelector('[data-role="msg2"]');
    // One click handler per pane: a re-render from inside an action (bar, lift bar, 18+ confirmation) must not stack a second
    // listener, otherwise every later "click again to confirm" button would fire twice and skip its own confirmation step.
    if (p.enquiryClick) p.removeEventListener('click', p.enquiryClick);
    p.enquiryClick = async ev => {
      const b = ev.target.closest('[data-action]'); if (!b) return;
      try {
        if (b.dataset.action === 'save-staff') { const patch = {}; ['status', 'campaign', 'staff_member', 'staff_notes'].forEach(k => patch[k] = p.querySelector(`[name="${k}"]`).value); await api.patch(`/api/enquiries/${id}`, patch); msg(m1, 'Saved.'); }
        if (b.dataset.action === 'contact') { await api.post(`/api/enquiries/${id}/contact`, { note: 'Staff logged contact' }); msg(m1, 'Contact logged; retention clock reset.'); }
        // GP9(e) takeover: the individual answers on the record were the former enquirer's; staff either confirm them as the current
        // enquirer's own or record them as not given. Only this explicit choice lifts the flag (editing the text alone does not).
        if (b.dataset.action === 'confirm-lead-answers') { if (!confirm("Confirm: the study/work, lifestyle and free-text answers on this record are the current enquirer's own?")) return; await api.patch(`/api/enquiries/${id}`, { lead_answers_confirmed: true }); await enquiryDetail(p, id); msg(p.querySelector('[data-role="msg1"]'), 'Confirmed. The notes are queued to be read for the review.'); }
        if (b.dataset.action === 'unrecord-lead-answers') { if (!confirm('Record the study/work, lifestyle and free-text answers as not given for the current enquirer? The party-wide answers stay.')) return; const blank = { lead_answers_confirmed: true }; ['doing', 'school', 'school_other', 'course', 'level', 'work', 'routine', 'tidy', 'social', 'guests', 'diet', 'else_text', 'intro'].forEach(k => blank[k] = ''); await api.patch(`/api/enquiries/${id}`, blank); await enquiryDetail(p, id); msg(p.querySelector('[data-role="msg1"]'), 'Recorded as not given.'); }
        if (b.dataset.action === 'mate-link') {
          const out = p.querySelector('[data-role="mateLinkOut"]'); const mm = p.querySelector('[data-role="msgmate"]');
          try { const r = await api.post(`/api/enquiries/${id}/mate-link`, {}); const url = `${publicBase()}#/mate?t=${encodeURIComponent(r.token)}`; out.hidden = false; out.textContent = url; try { await navigator.clipboard.writeText(url); msg(mm, 'Share link copied. Give it to the enquirer to pass on to their friends.'); } catch { msg(mm, 'Share link shown above; copy it for the enquirer.'); } }
          catch (ex) { msg(mm, ex.message, false); }
          return;
        }
        if (b.dataset.action === 'bar') {
          const mb = p.querySelector('[data-role="msgbar"]'); const reason = p.querySelector('[name="bar_reason"]').value.trim();
          if (!reason) return msg(mb, 'A reason is required to bar someone from matching.', false);
          const pick = n => (p.querySelector(`[name="${n}"]:checked`) || {}).value;
          const body = { reason, person: (p.querySelector('[name="bar_person"]') || {}).value || 'lead', party_choice: pick('bar_party_choice'), friends_choice: pick('bar_friends_choice') };
          if (p.querySelector('[name="bar_party_choice"]') && !body.party_choice) return msg(mb, 'Speak with the people who enquired with them first, then say whether they leave with the barred person or carry on without them.', false);
          if (p.querySelector('[name="bar_friends_choice"]') && !body.friends_choice) return msg(mb, 'Speak with their linked friends first, then say whether they stay in the group or leave with them.', false);
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to bar from matching'; return; }
          try { const r = await api.post(`/api/enquiries/${id}/bar`, body); await enquiryDetail(p, id); msg(p.querySelector('[data-role="msgbar"]'), r.message); } catch (ex) { msg(mb, ex.message, false); }
          return;
        }
        if (b.dataset.action === 'adults-confirm' || b.dataset.action === 'adults-withdraw') {
          const ma = p.querySelector('[data-role="msgadults"]'); const confirm = b.dataset.action === 'adults-confirm';
          if (!confirm && !b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to withdraw'; return; }
          // strictly a JSON boolean, exactly as the server requires; the server records who, when, for which move-in date and which group
          try { await api.patch(`/api/enquiries/${id}`, { adults_confirmed: confirm }); await enquiryDetail(p, id); msg(p.querySelector('[data-role="msgadults"]'), confirm ? 'Recorded: everyone in the group confirmed as 18 or older at move-in.' : 'Confirmation withdrawn; it is flagged to be checked again.'); }
          catch (ex) { msg(ma, ex.data && ex.data.errors ? Object.values(ex.data.errors).join(' ') : ex.message, false); }
          return;
        }
        if (b.dataset.action === 'lift-bar') { const mb = p.querySelector('[data-role="msgbar"]'); try { const r = await api.post(`/api/enquiries/${id}/lift-bar`, {}); await enquiryDetail(p, id); msg(p.querySelector('[data-role="msgbar"]'), r.message); } catch (ex) { msg(mb, ex.message, false); } return; }
        if (b.dataset.action === 'delete') { if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to permanently delete'; return; } await api.del(`/api/enquiries/${id}`); navigate('enquiries'); }
        if (b.dataset.action === 'add-couple') { const w = p.querySelector('[data-role="coupleEdit"]'); const i = w.children.length; const d = document.createElement('div'); d.className = 'couple-row'; d.dataset.cindex = i; d.innerHTML = `<span class="party-row__title">Couple ${i + 1}</span>${['a', 'b'].map((k, j) => `<label class="party-cell"><small>${j ? 'Second' : 'First'} person</small><select data-cp="${k}"><option value="">Select…</option>${coupleRefOptions(e, '')}</select></label>`).join('')}`; w.append(d); return; }
        if (b.dataset.action === 'save-couples') {
          const m2 = p.querySelector('[data-role="msg2"]'); const ans = p.querySelector('[name="couples_answer"]').value;
          if (!ans) return msg(m2, 'Choose No or Yes.', false);
          const pairs = [...p.querySelectorAll('[data-role="coupleEdit"] .couple-row')].map(r => [r.querySelector('[data-cp="a"]').value, r.querySelector('[data-cp="b"]').value]).filter(x => x[0] || x[1]);
          try { await api.patch(`/api/enquiries/${id}`, { couples_answer: ans, couples_json: ans === 'Yes' ? pairs : [] }); msg(m2, 'Couple answer saved. It applies from the next matching run.'); setTimeout(render, 800); }
          catch (ex) { msg(m2, ex.message, false); }
          return;
        }
        if (b.dataset.action === 'save-answers') {
          const patch = {}; ['first_name', 'last_name', 'email', 'phone', 'move_in', 'move_out', 'birth_country', 'budget_option', 'mate_max', 'bunk_ok', 'smoke', 'smoke_ok'].forEach(k => patch[k] = p.querySelector(`[name="${k}"]`).value);
          patch.features = [...p.querySelectorAll('input[name="edit_features"]:checked')].map(i => i.value);
          patch.features_top = [...p.querySelectorAll('input[name="edit_features_top"]:checked')].map(i => i.value).filter(v => patch.features.includes(v));
          try { await api.patch(`/api/enquiries/${id}`, patch); msg(m2, 'Answers saved.'); setTimeout(render, 600); }
          catch (err) { msg(m2, (err && err.message) || 'Could not save: please check the answers.', false); }
        }
        if (b.dataset.action === 'save-intro') {
          try { await api.patch(`/api/enquiries/${id}`, introPatch(p, 'lead_')); msg(m2, 'Introduction details saved.'); setTimeout(render, 600); }
          catch (err) { msg(m2, (err && err.message) || 'Could not save the introduction details.', false); }
          return;
        }
        if (b.dataset.action === 'save-member-intro') {
          const rowEl = p.querySelector(`.member-row[data-member="${b.dataset.member}"]`);
          try { await api.patch(`/api/enquiries/${id}/members/${b.dataset.member}`, introPatch(rowEl, `m${b.dataset.member}_`)); msg(m2, 'Introduction details saved.'); setTimeout(render, 600); }
          catch (err) { msg(m2, (err && err.message) || 'Could not save the introduction details.', false); }
          return;
        }
        if (b.dataset.action === 'confirm-member') { const rowEl = p.querySelector(`.member-row[data-member="${b.dataset.member}"]`); await api.patch(`/api/enquiries/${id}/members/${b.dataset.member}`, { birth_country: rowEl.querySelector('[name="member_country"]').value, birth_country_confirmed: 1 }); msg(m2, 'Party member country confirmed.'); setTimeout(render, 600); }
        if (b.dataset.action === 'confirm-country') { await api.patch(`/api/enquiries/${id}`, { birth_country: p.querySelector('[name="birth_country"]').value, birth_country_confirmed: 1 }); msg(m2, 'Country confirmed.'); setTimeout(render, 600); }
      } catch (ex) { msg(ex.data && ex.data.errors ? m2 : m1, ex.data && ex.data.errors ? Object.values(ex.data.errors).join(' ') : ex.message, false); }
    };
    p.addEventListener('click', p.enquiryClick);
    // 3.11 → 3.12 in the edit card: the "matter most" chips follow the ticked features; an unticked feature drops out of the three
    if (p.enquiryChange) p.removeEventListener('change', p.enquiryChange);
    p.enquiryChange = ev => {
      const i = ev.target; if (!i || i.name !== 'edit_features') return;
      const top = p.querySelector(`input[name="edit_features_top"][value="${CSS.escape(i.value)}"]`); if (!top) return;
      top.closest('.chip').hidden = !i.checked; if (!i.checked) top.checked = false;
    };
    p.addEventListener('change', p.enquiryChange);
  }

  // ---- Apartments ----
  views.apartments = async p => {
    if (state.param === 'new') return apartmentForm(p, null);
    if (state.param) return apartmentDetail(p, +state.param);
    const f = state.cache.aptFilter || {};
    const r = await api.get('/api/apartments?' + new URLSearchParams({ ...f, dataset: state.dataset }));
    const dq = await api.get('/api/apartments-data-quality');
    const setName = state.dataset === 'test' ? 'test' : 'real';
    p.innerHTML = `
      <div class="pane-head"><h1>Apartments</h1><span class="spacer"></span>
        <button class="btn btn--sm" type="button" data-action="add">Add apartment</button><button class="btn btn--ghost btn--sm" type="button" data-action="export">Export list (.xlsx)</button>
        <p>${r.total} apartments. “Groups” counts the ${setName} groups that list each apartment. “Features” counts how many of the ${(state.ref.features || []).length || 20} features are recorded (yes or no) for each apartment; the export carries the same columns so they can be filled in Excel and loaded again under Settings.</p></div>
      <form class="toolbar" data-role="filters"><input type="search" name="q" placeholder="Search address" value="${esc(f.q || '')}">
        <select name="type_code"><option value="">Any type</option>${Object.keys(state.ref.apt_types).map(t => `<option ${f.type_code === t ? 'selected' : ''}>${t}</option>`).join('')}</select>
        <label class="small"><input type="checkbox" name="available_only" value="true" ${f.available_only ? 'checked' : ''}> With a date only</label><button class="btn btn--sm" type="submit">Filter</button></form>
      <p class="msg" data-role="msg"></p>
      <div class="table-wrap"><table class="tbl"><thead><tr><th>Address</th><th>Type · min lease · bills</th><th>Available</th><th>Rent/wk 12-mo · 6-mo</th><th>Bedrooms</th><th class="nw">Sleeps</th><th class="nw">Features</th><th>Groups (${setName})</th></tr></thead><tbody>
        ${r.items.map(a => `<tr data-id="${a.id}"><td class="wrap-cell"><b>${esc(a.address)}</b><br><span class="small muted">${esc(a.suburb)}</span></td><td><b>${a.type_code}</b><br><span class="small muted">${a.min_lease_months} mo min · bills ${a.bills_included ? 'incl.' : 'excl.'}</span></td>
          <td class="mono">${a.is_leased ? `<span class="badge badge--leased">leased ${fmtD(a.leased_at)}</span>` : a.available_from ? fmtD(a.available_from) : `<span class="badge badge--warn">no date: “${esc(a.available_raw)}”</span>`}</td><td class="mono">${money(a.rent_12m)}<br><span class="small muted">${money(a.rent_6m)} 6-mo</span></td>
          <td class="wrap-cell small">${[a.bedroom1, a.bedroom2, a.bedroom3].filter(Boolean).map((b, i) => `Bed ${i + 1}: ${esc(b)}`).join('<br>')}</td><td class="mono nw">${a.capacity_min}–${a.capacity_max}${a.a4b_ok ? '' : ' <span class="badge badge--warn" title="More beds than this apartment type allows">too many beds</span>'}</td>
          <td class="mono nw">${a.features_recorded ? `${a.features_recorded}/${Object.keys(a.features || {}).length}` : '<span class="small muted">not yet recorded</span>'}</td>
          <td class="mono"><b class="num-big">${a.proposed_groups}</b>${a.is_leased ? ' <span class="badge badge--leased">leased</span>' : ''}</td></tr>`).join('')}
      </tbody></table></div>
      <details class="more mt"><summary>Apartment list checks</summary><div class="card card--tight dq mt">${dqHtml(dq)}</div></details>`;
    p.querySelector('tbody').addEventListener('click', e => { const tr = e.target.closest('tr[data-id]'); if (tr) navigate('apartments', tr.dataset.id); });
    const form = p.querySelector('[data-role="filters"]'); form.addEventListener('submit', e => { e.preventDefault(); const o = {}; new FormData(form).forEach((v, k) => { if (v) o[k] = v; }); state.cache.aptFilter = o; render(); });
    p.querySelector('[data-action="add"]').addEventListener('click', () => navigate('apartments', 'new'));
    p.querySelector('[data-action="export"]').addEventListener('click', async ev => { const b = ev.currentTarget; b.disabled = true; try { await api.download('/api/apartments/export.xlsx'); } catch (ex) { msg(p.querySelector('[data-role="msg"]'), ex.message, false); } b.disabled = false; });
  };

  function dqHtml(dq) {
    const fixed = ['604, 606 and 607/10 St Andrews Place sleep 5', '904/543 Elizabeth St Bedroom 2 is “1 king or 2 singles”'];
    return `<ul>
      <li><b>Count:</b> ${dq.apartment_count} apartments${dq.a1_matches ? ' (the full launch list)' : ` (the launch list has ${dq.a1_expected})`}. By type: ${Object.entries(dq.by_type).map(([k, v]) => `${k} ${v}`).join(', ')}.</li>
      <li><b>Not yet available:</b> ${dq.non_date_rows_live.map(x => `${esc(x.address)} (“${esc(x.text)}”)`).join('; ') || 'none'}. Not matched until staff enter a date.</li>
      <li><b>Minimum lease:</b> ${Object.entries(dq.min_lease_values || {}).map(([m, n]) => `${n} apartment(s) at ${Number(m)} months`).join(', ')}; you can change it on each apartment. New apartments start at ${dq.min_lease_default} months.</li>
      <li><b>Bathrooms:</b> taken from the apartment type.</li>
      <li><b>Features:</b> ${dq.features_complete || 0} apartment(s) with all features recorded; ${dq.features_unknown_cells || 0} feature answer(s) still unknown. Unknown is never treated as no. Record them on each apartment, or export the list, fill the Yes/No columns in Excel and load it under Settings.</li>
      <li><b>Beds vs apartment type:</b> ${dq.capacity_issues_live.map(x => `${esc(x.address)} (${x.type}${x.max_capacity ? `: sleeps up to ${x.max_capacity}, limit ${x.limit}` : `: ${esc(x.note)}`})`).join('; ') || `all within limits. Agreed list corrections applied: ${fixed.join('; ')}`}.</li>
      <li><b>Price guide on the form vs list:</b> ${dq.price_guide_differences.filter(d => d.note !== 'rounding only').map(d => `${d.guide} ${esc(d.option)}: guide $${d.bill[0]}–${d.bill[1]} vs list ${d.list ? `$${d.list[0]}–${d.list[1]}` : 'n/a'}${d.note && d.note !== 'differs' ? ` (${esc(d.note)})` : ''}`).join('; ') || 'no differences beyond $1 rounding'}. ${dq.price_guide_differences.filter(d => d.note === 'rounding only').length} option(s) differ by $1 rounding only. The form uses the agreed guide figures.</li>
    </ul>`;
  }

  function accessFactCardHtml(af) {
    // A13(a) recorded step-free fact (T3(d) C3): a plain record with its provenance. Never inferred from a lift, an address or
    // another apartment; unknown stays unknown. Recording a change queues a fresh AI review for the groups offering this apartment.
    const cur = (af && af.current) || {}, hist = (af && af.history) || [];
    const v = cur.value === 1 ? 'yes' : cur.value === 0 ? 'no' : 'unknown';
    // the record's closed status word, in plain words for staff (the raw word is a storage key, not staff wording)
    const STATUS_WORDS = { confirmed: 'confirmed step-free', not_step_free: 'confirmed not step-free', unknown: 'not confirmed',
      excluded_from_confirmation: 'not covered by the confirmation (unknown)' };
    const statusWords = (st) => STATUS_WORDS[st] || (st ? 'recorded' : '');
    const badge = cur.value === 1 ? 'badge--ok' : cur.value === 0 ? 'badge--warn' : '';
    return `<h3>Step-free access (recorded fact)</h3>
      <p class="small"><span class="badge ${badge}">${esc(cur.label || 'Not recorded (unknown)')}</span></p>
      ${cur.record_id ? `<p class="small muted">Source: ${esc(cur.source || '—')}${cur.scope ? ` · covers: ${esc(cur.scope)}` : ''} · recorded by ${esc(cur.recorded_by || '—')} on ${fmtDT(cur.recorded_at)}${cur.note ? `<br>Staff note: ${esc(cur.note)}` : ''}</p>` : '<p class="small muted">Nothing recorded yet. A lift is not step-free access; nothing is assumed from the address or from another apartment.</p>'}
      <div class="editgrid">
        <div class="field"><label>Completely step-free route from the street into the apartment<select data-role="afValue">${['unknown', 'yes', 'no'].map(x => `<option value="${x}" ${v === x ? 'selected' : ''}>${x === 'unknown' ? 'Unknown / not confirmed' : x === 'yes' ? 'Yes - confirmed step-free' : 'No - not step-free'}</option>`).join('')}</select></label></div>
        <div class="field"><label>Who confirmed it and when<input data-role="afSource" maxlength="200" placeholder="e.g. Bill, 10 November 2026 / site check by Anna" value="${esc(cur.source || '')}"></label></div>
        <div class="field"><label>What the confirmation covered<input data-role="afScope" maxlength="200" placeholder="e.g. street to apartment door" value="${esc(cur.scope || '')}"></label></div>
      </div>
      <div class="actions"><button class="btn btn--sm" type="button" data-action="record-access">Record</button><span class="small muted">Any change queues a fresh AI review for the groups offering this apartment; nothing is approved by hand.</span></div>
      <p class="msg" data-role="afmsg"></p>
      ${hist.length > 1 ? `<details class="small"><summary>History (${hist.length})</summary><ul class="small muted">${hist.map(h => `<li>${fmtDT(h.recorded_at)} · ${h.value === 1 ? 'yes' : h.value === 0 ? 'no' : 'unknown'} (${esc(statusWords(h.status))}) · ${esc(h.source || '—')} · by ${esc(h.recorded_by || '—')}${h.superseded_at ? '' : ' · current'}</li>`).join('')}</ul></details>` : ''}`;
  }

  async function apartmentDetail(p, id) {
    const a = await api.get(`/api/apartments/${id}?dataset=${state.dataset}`);
    let af = null; try { af = await api.get(`/api/apartments/${id}/access-facts`); } catch (ex) { af = { current: {}, history: [], error: ex.message }; }
    p.innerHTML = `
      <div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-role="back">← Apartments</button><h1>${esc(a.address)}</h1><span class="badge">${a.type_label}</span>${a.is_leased ? `<span class="badge badge--leased">Leased ${fmtD(a.leased_at)}${a.leased_group_id ? ` · Group ${a.leased_group_id}` : ''}</span>` : (a.is_available ? '<span class="badge badge--ok">available</span>' : '<span class="badge">no date yet</span>')}${a.a4b_ok ? '' : '<span class="badge badge--warn">more beds than this type allows</span>'}<span class="spacer"></span>
        <span class="small muted">Original text: “${esc(a.address_raw)}” · ${a.bathrooms} bathroom(s) · monthly ${money(a.monthly_12m)} / ${money(a.monthly_6m)}</span></div>
      <div class="detail detail--2">
        <div>
          <div class="card card--tight"><h3>Lease status</h3>
            ${a.is_leased ? `<p class="small">Marked Leased on ${fmtDT(a.leased_at)}${a.leased_by ? ` by ${esc(a.leased_by)}` : ''}${a.leased_group_id ? ` for <a href="#/staff/groups/${a.leased_group_id}">Group ${a.leased_group_id}</a>` : ' (lease recorded outside MateBot AI)'}. It is not matched until relisted.</p>
              ${(a.rerank_pending_group_ids || []).length ? `<p class="small warn-text">The lease is recorded, but ${a.rerank_pending_group_ids.length} other group(s) still need to be moved to their next best apartment. Use “Finish updating other groups”; the lease itself is not repeated.</p>` : ''}
              <div class="actions"><button class="btn btn--sm" type="button" data-action="finish-lease">Finish updating other groups</button><label class="small">New date available <input type="date" data-role="relistDate" value="${esc(a.available_from || '')}"></label><button class="btn btn--sm" type="button" data-action="relist">Relist apartment</button></div>`
            : `<p class="small muted">Apartments are never held for groups. An apartment counts as taken only when reservations marks it Leased here or on the group. MateBot AI then moves every other group to its next best available apartment straight away and flags them.</p>
              <div class="actions"><select data-role="leaseGroup"><option value="">Leased outside MateBot AI (no group)</option>${a.groups.filter(g => g.status !== 'Dissolved' && g.status !== 'Leased').map(g => `<option value="${g.id}">Group ${g.id} · ${esc(g.status)} · ${(g.members || []).map(m => esc(m.first_name)).join(', ')}</option>`).join('')}</select><button class="btn btn--sm btn--danger" type="button" data-action="lease">Mark Leased</button></div>`}
            <p class="msg" data-role="leasemsg"></p>
          </div>
          <div class="card card--tight mt"><h3>Matching for this apartment</h3>
            <p class="small muted">Checks every rule first; the AI then rates every passing group and this apartment is added to each group's ranked list. For all apartments at once use “Run matching now” on Groups. Showing <b>${state.dataset === 'test' ? 'test data' : 'real enquiries'}</b>.</p>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="run">Find groups (rules + AI)</button><button class="btn btn--ghost btn--sm" type="button" data-action="run-rules">Rules only</button></div>
            <p class="msg" data-role="runmsg"></p>
            ${a.runs.length ? `<p class="small muted">Last run ${fmtDT(a.runs[0].finished_at)} by ${esc(a.runs[0].staff_member)}: ${a.runs[0].candidates_considered} candidate groups checked, ${a.runs[0].groups_passing} passed every rule, ${a.runs[0].ai_used ? 'AI ranked them' : 'ranked by rules only' + (a.runs[0].ai_error ? ` (${esc(a.runs[0].ai_error)})` : '')}.</p>` : ''}
          </div>
          <h3 class="mt">${state.dataset === 'test' ? 'Test' : 'Real'} groups suited to this apartment (${a.groups.length})</h3>
          <div class="groups mt" data-role="groups">${a.groups.map(g => groupCard(g, { compact: true })).join('') || '<p class="small muted">No group lists this apartment yet.</p>'}</div>
        </div>
        <div>
          <div class="card card--tight" data-role="editcard">${apartmentFormHtml(a)}
            <div class="actions"><button class="btn btn--sm" type="button" data-action="save">Save changes</button><button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="delete">Delete permanently</button></div>
            <p class="msg" data-role="msg"></p></div>
          <div class="card card--tight mt" data-role="accesscard">${accessFactCardHtml(af)}${af && af.error ? `<p class="small warn-text">${esc(af.error)}</p>` : ''}</div>
        </div>
      </div>`;
    p.querySelector('[data-role="back"]').addEventListener('click', () => navigate('apartments'));
    const m = p.querySelector('[data-role="msg"]'), rm = p.querySelector('[data-role="runmsg"]');
    p.addEventListener('click', async ev => {
      const b = ev.target.closest('[data-action]'); if (!b) return;
      try {
        if (b.dataset.action === 'save') { await api.patch(`/api/apartments/${id}`, readAptForm(p)); msg(m, 'Saved.'); setTimeout(render, 500); }
        if (b.dataset.action === 'delete') { if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to delete permanently'; return; } await api.del(`/api/apartments/${id}`); navigate('apartments'); }
        if (b.dataset.action === 'run' || b.dataset.action === 'run-rules') {
          b.disabled = true; msg(rm, 'Checking candidate groups against every rule' + (b.dataset.action === 'run' ? ', then asking the AI to assess compatibility…' : '…'));
          const r = await api.post('/api/match/run', { apartment_id: id, dataset: state.dataset, use_ai: b.dataset.action === 'run' });
          msg(rm, `${r.candidates_considered} candidate groups checked from ${r.pool_size} enquiries; ${r.groups_passing} passed every rule. Ranking: ${r.ranking_basis}${r.ai_error ? ' · ' + r.ai_error : ''}.`, true);
          setTimeout(render, 900);
        }
        if (b.dataset.action === 'lease') {
          const lm = p.querySelector('[data-role="leasemsg"]'); const gid = p.querySelector('[data-role="leaseGroup"]').value;
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again: the apartment becomes taken and every other group is re-ranked'; return; }
          b.disabled = true; const r = await api.post(`/api/apartments/${id}/lease`, gid ? { group_id: +gid } : {});
          msg(lm, r.message || `Leased. ${r.affected.length} other group(s) moved to their next best apartment and flagged.`, !(r.rerank && r.rerank.status === 'partial')); setTimeout(render, 1200);
        }
        if (b.dataset.action === 'finish-lease') {   // idempotent: same POST, same group binding; never a second claim, never a switch
          const lm = p.querySelector('[data-role="leasemsg"]'); b.disabled = true;
          const r = await api.post(`/api/apartments/${id}/lease`, a.leased_group_id ? { group_id: a.leased_group_id } : {});
          msg(lm, r.message || `${r.affected.length} other group(s) moved to their next best apartment and flagged.`, !(r.rerank && r.rerank.status === 'partial')); setTimeout(render, 1200);
        }
        if (b.dataset.action === 'record-access') {
          const am = p.querySelector('[data-role="afmsg"]');
          const body = { step_free_access: p.querySelector('[data-role="afValue"]').value };
          const src = p.querySelector('[data-role="afSource"]').value.trim(), scope = p.querySelector('[data-role="afScope"]').value.trim();
          if (src) body.source = src; if (scope) body.scope = scope;
          b.disabled = true;
          const r = await api.post(`/api/apartments/${id}/access-facts`, body);
          const q = (r.groups && r.groups.requeued) || [];
          msg(am, `Recorded: ${r.current.label}. ${q.length ? `${q.length} group(s) offering this apartment queued for a fresh AI review.` : 'No stored review needed re-queuing.'}`, true);
          setTimeout(render, 900);
        }
        if (b.dataset.action === 'relist') { const d = p.querySelector('[data-role="relistDate"]').value; await api.post(`/api/apartments/${id}/relist`, d ? { available_from: d } : {}); setTimeout(render, 300); }
        if (b.dataset.action === 'open-group') navigate('groups', b.dataset.id);
      } catch (ex) { msg(m, ex.message, false); b.disabled = false; }
    });
  }

  function apartmentFormHtml(a = {}) {
    return `<h3>${a.id ? 'Apartment details' : 'New apartment'}</h3><div class="editgrid">
      <div class="field"><label>Address<input name="address" value="${esc(a.address || '')}" placeholder="Unit 12 / 35 Darling Street"></label></div>
      <div class="field"><label>Suburb<select name="suburb">${['Melbourne CBD', 'South Yarra', 'East Melbourne'].map(s => `<option ${(a.suburb || 'Melbourne CBD') === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label></div>
      <div class="field"><label>Type<select name="type_code">${Object.entries(state.ref.apt_types).map(([k, v]) => `<option value="${k}" ${a.type_code === k ? 'selected' : ''}>${k} · ${v.label}</option>`).join('')}</select></label></div>
      <div class="field"><label>Date available<input name="available_from" type="date" value="${esc(a.available_from || '')}"></label>${a.available_raw ? `<span class="small muted">Sheet said “${esc(a.available_raw)}”</span>` : ''}</div>
      <div class="field"><label>Rent / week, 12-month rate<input name="rent_12m" type="number" step="1" value="${a.rent_12m ?? ''}"></label></div>
      <div class="field"><label>Rent / week, 6-month rate<input name="rent_6m" type="number" step="1" value="${a.rent_6m ?? ''}"></label></div>
      <div class="field"><label>Minimum lease (months; usually 2)<input name="min_lease_months" type="number" step="0.5" min="0.5" value="${a.min_lease_months ?? 2}"></label></div>
      <div class="field"><label>Bills / utilities<select name="bills_included"><option value="0" ${!a.bills_included ? 'selected' : ''}>Excluded</option><option value="1" ${a.bills_included ? 'selected' : ''}>Included</option></select></label></div>
      <div class="field"><label>Bedroom 1<input name="bedroom1" value="${esc(a.bedroom1 || '1 king or 2 singles')}"></label></div>
      <div class="field"><label>Bedroom 2<input name="bedroom2" value="${esc(a.bedroom2 || '')}"></label></div>
      <div class="field"><label>Bedroom 3<input name="bedroom3" value="${esc(a.bedroom3 || '')}"></label></div>
    </div><p class="small muted">Bedding values: setups separated by “or”, beds combined with “+”, e.g. “1 king or 2 singles or 1 king+1 single”, “1 single or 1 bunk bed”. Internet is never included.</p>
    <h4 class="mt">Apartment and building features</h4>
    <p class="small muted">Unknown until someone records it; unknown is never treated as no. Enquirers tick the same list as wishes (never a filter).</p>
    ${(state.ref.feature_groups || []).map(g => `<p class="sublabel">${esc(g)}</p><div class="editgrid editgrid--features">${(state.ref.features || []).filter(f => f.group === g).map(f => `<div class="field"><label>${esc(f.label)}<select name="feat_${f.key}">${['unknown', 'yes', 'no'].map(v => `<option value="${v}" ${((a.features || {})[f.key] || 'unknown') === v ? 'selected' : ''}>${v === 'unknown' ? 'Unknown' : v === 'yes' ? 'Yes' : 'No'}</option>`).join('')}</select></label></div>`).join('')}</div>`).join('')}
    <div class="editgrid"><div class="field"><label>Car parking charge <span class="small muted">(as stated, e.g. “$60 per week”; blank = not stated)</span><input name="parking_charge" maxlength="80" value="${esc(a.parking_charge || '')}"></label></div></div>`;
  }
  function readAptForm(p) {
    const o = {}; ['address', 'suburb', 'type_code', 'available_from', 'rent_12m', 'rent_6m', 'min_lease_months', 'bills_included', 'bedroom1', 'bedroom2', 'bedroom3', 'parking_charge'].forEach(k => { const v = p.querySelector(`[name="${k}"]`).value; o[k] = v === '' ? null : v; });
    o.features = {}; (state.ref.features || []).forEach(f => { const el = p.querySelector(`[name="feat_${f.key}"]`); if (el) o.features[f.key] = el.value; });   // 'unknown' | 'yes' | 'no', nothing inferred
    return o;
  }
  async function apartmentForm(p) {
    p.innerHTML = `<div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-role="back">← Apartments</button><h1>Add apartment</h1></div><div class="card card--tight">${apartmentFormHtml({})}<div class="actions"><button class="btn btn--sm" type="button" data-action="create">Add apartment</button></div><p class="msg" data-role="msg"></p></div>`;
    p.querySelector('[data-role="back"]').addEventListener('click', () => navigate('apartments'));
    p.querySelector('[data-action="create"]').addEventListener('click', async () => { try { const a = await api.post('/api/apartments', readAptForm(p)); navigate('apartments', a.id); } catch (ex) { msg(p.querySelector('[data-role="msg"]'), ex.message, false); } });
  }

  // ---- Matching run panel and AI queue (shown under “Details” on Groups) ----
  let pollTimer = null;
  const stopPoll = () => { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } };
  const pct = (d, t) => t ? Math.round(100 * d / t) : 0;
  function runPanelHtml(st) {
    if (!st) return '<p class="small muted">No full run yet for this data set.</p>';
    const pr = st.progress || {}, sm = st.summary || {};
    const running = st.status === 'running';
    const phase = pr.phase_label || st.status;
    const bar = running ? `<div class="progress" role="progressbar" aria-valuenow="${pct(pr.done, pr.total)}" aria-valuemin="0" aria-valuemax="100"><span style="width:${pct(pr.done, pr.total)}%"></span></div>
        <p class="small">${esc(phase)}${pr.total ? ` · ${pr.done}/${pr.total}${pr.phase === 'ai' ? ' AI batches' : pr.phase === 'rules' ? ' apartments' : ''}` : ''}${pr.groups_total ? ` · ${pr.groups_total} rule-passing groups so far` : ''}${pr.ai_calls ? ` · ${pr.ai_calls} AI calls${pr.ai_failed ? `, ${pr.ai_failed} failed` : ''}` : ''}</p>` : '';
    const done = st.status === 'done' ? `<div class="kpis kpis--tight">
        <div class="kpi"><b>${sm.pool_size}</b><span>enquiries considered</span></div>
        <div class="kpi"><b>${(sm.pairs_compared || 0).toLocaleString()}</b><span>pairs compared (${sm.pairs_compatible} compatible)</span></div>
        <div class="kpi"><b>${sm.groups_total}</b><span>groups passed every rule</span></div>
        <div class="kpi"><b>${sm.primary_groups}</b><span>proposed groups (one per enquiry)</span></div>
        <div class="kpi"><b>${sm.enquiries_in_primary_group}</b><span>enquiries in a proposed group (an enquiry may include companions) · ${sm.enquiries_unmatched} enquiries in no candidate group yet</span></div>
        ${sm.groups_new != null ? `<div class="kpi"><b>${sm.groups_new}</b><span>new or changed groups, queued for the AI</span></div>
        <div class="kpi"><b>${sm.groups_unchanged}</b><span>unchanged groups kept their AI assessment${sm.apartment_refresh_queued ? ` · ${sm.apartment_refresh_queued} apartment-ranking refresh(es) queued` : ''}</span></div>` : `<div class="kpi"><b>${sm.ai_calls || 0}</b><span>AI calls during the run (older run format)</span></div>`}
        <div class="kpi"><b>${sm.duration_s}s</b><span>run time (rules only; no AI call in the run itself)</span></div>
      </div>${sm.ai_error ? `<p class="msg err">AI: ${esc(sm.ai_error)}</p>` : ''}` : '';
    const failed = st.status === 'failed' ? `<p class="msg err">Run failed: ${esc(pr.error || st.ai_error || 'unknown error')}.</p>` : st.status === 'interrupted' ? '<p class="msg err">This run was interrupted by a server restart; its results were not saved. Run again.</p>' : '';
    return `<p class="small muted">Run #${st.run_id} · ${esc(st.dataset)} data · started ${fmtDT(st.started_at)} by ${esc(st.staff_member || '')}${st.finished_at ? ` · finished ${fmtDT(st.finished_at)}` : ''} · <b>${esc(st.status)}</b></p>${bar}${done}${failed}`;
  }

  function aiQueueHtml(q) {
    const ds = (q.per_dataset || {})[state.dataset] || {};
    const pend = (ds.pending || 0) + (ds.running || 0), ready = ds.ok || 0, failed = ds.failed || 0, refresh = ds.refresh || 0;
    const total = pend + ready + failed;
    const pct = total ? Math.round(ready / total * 100) : 0;
    const passPct = q.pass_total ? Math.round(q.pass_done / q.pass_total * 100) : 0;
    return `<h3>AI assessment queue</h3>
      <p class="small">Every new or changed group is assessed by the AI (${esc(q.model || '')}) and its apartments ranked before it is <b>Ready for reservations</b>. Travel is checked last, on each group's page, and is not part of that. Unchanged groups are never re-run. ${q.ai_configured ? '' : '<span class="badge badge--warn">no AI credentials in this server process</span> '}${q.thread_alive ? '' : '<span class="badge badge--warn">queue worker not running</span>'}</p>
      <div class="progress" title="${ready} of ${total} live ${state.dataset === 'test' ? 'TEST' : 'real'} groups are AI-assessed"><span class="${pend ? 'progress--live' : ''}" style="width:${pct}%"></span></div>
      <div class="kpis kpis--tight">
        <div class="kpi ${pend ? 'kpi--flag' : ''}"><b>${pend}</b><span>AI assessment pending${ds.running ? ` · ${ds.running} being assessed now` : ''}</span></div>
        <div class="kpi"><b>${ready}</b><span>ready for reservations${refresh ? ` · ${refresh} apartment-ranking refresh queued` : ''}</span></div>
        <div class="kpi ${failed ? 'kpi--flag' : ''}"><b>${failed}</b><span>AI failed (staff retry)</span></div>
        <div class="kpi"><b>${q.calls || 0}</b><span>AI calls since start-up${q.failed_calls ? ` · ${q.failed_calls} failed` : ''}${q.last_batch_s ? ` · last batch ${q.last_batch_s}s` : ''}</span></div>
      </div>
      ${q.busy ? `<p class="small">Working: batch ${q.pass_done || 0} of ${q.pass_total || 0} in this pass (${q.workers} calls in parallel, ${q.batch} groups per call, up to ${q.attempts} attempts each) · ${passPct}%</p>` : `<p class="small muted">Idle${q.last_finished_at ? ` · last finished ${fmtDT(q.last_finished_at)}` : ''}. ${q.workers} calls in parallel, ${q.batch} groups per call, up to ${q.attempts} attempts each.</p>`}
      ${q.last_error ? `<p class="msg err small">Last AI problem${q.last_error_at ? ` (${fmtDT(q.last_error_at)})` : ''}: ${esc(aiErrorText(q.last_error))}</p>` : ''}
      ${(q.failed_groups || []).length ? `<p class="small">Failed: ${q.failed_groups.map(f => `<a href="#/staff/groups/${f.id}">Group ${f.id}</a>`).join(', ')}</p>` : ''}
      <div class="actions">
        ${failed ? '<button class="btn btn--sm" type="button" data-action="ai-retry-all">Retry failed AI assessments</button>' : ''}
        <button class="btn btn--ghost btn--sm" type="button" data-action="ai-reassess" title="Deliberate full refresh: every live group in this data set is re-assessed (about one AI call per ${q.batch} groups). MateBot AI never does this on its own.">Refresh every AI assessment (${state.dataset === 'test' ? 'test' : 'real'})</button>
      </div>`;
  }

  views.matching = async p => { navigate('groups', state.param); };
  views.groups = async p => {
    if (state.param) return groupDetail(p, +state.param);
    stopPoll();
    const f = state.cache.grpFilter || {};
    const [runs, grp, sched, aiq] = await Promise.all([api.get('/api/match/runs?dataset=' + state.dataset), api.get('/api/groups?' + new URLSearchParams({ dataset: state.dataset, ...(f.status ? { status: f.status } : {}), ...(f.ai ? { ai_state: f.ai } : {}), alternatives: f.alts ? 'true' : 'false' })), api.get('/api/scheduler'), api.get('/api/ai-queue')]);
    let last = runs.items[0] || null;
    const ds = (aiq.per_dataset || {})[state.dataset] || {};
    const qPend = (ds.pending || 0) + (ds.running || 0), qFail = ds.failed || 0;
    const next = sched && sched.next_check_at ? fmtDT(sched.next_check_at) : (sched && sched.next_run_at ? fmtDT(sched.next_run_at) : 'on the hour');
    p.innerHTML = `
      <div class="pane-head"><h1>Groups</h1><span class="spacer"></span>
        <button class="btn btn--sm" type="button" data-action="run-all" title="Same as the hourly run, right now: groups are formed from the rules; the AI then assesses every new or changed group before it is ready">Run matching now</button>
        <button class="btn btn--ghost btn--sm" type="button" data-action="toggle-details" aria-expanded="${state.cache.showDetails ? 'true' : 'false'}">Details</button>
        <button class="btn btn--ghost btn--sm" type="button" data-go="outcomes" title="Where every person stands right now, with the reason and the next step">Outcomes by person</button>
        <p class="statusline">${last ? `Last matching run ${fmtDT(last.finished_at || last.started_at)}${last.status === 'running' ? ' (running now)' : ''}` : 'No matching run yet'} · next hourly check ${next}${qPend ? ` · <b>${qPend} group${qPend > 1 ? 's' : ''} being assessed by the AI</b>` : ''}${qFail ? ` · <span class="badge badge--warn">${qFail} AI assessment${qFail > 1 ? 's' : ''} failed</span>` : ''}</p>
        <p>Each group is shown with the apartment it suits best, the whole weekly rent and a guide share per person.</p><details class="inline-help"><summary>How this works</summary><span>A group is <b>Ready for reservations</b> once the AI has assessed it and ranked its apartments; until then it shows as <b>AI assessment pending</b>. Each person is in one proposed group at a time. To change a group, reject one or more members and the AI finds replacements. Apartments are never held: one is taken only when you mark a group Leased, and every other group then moves to its next best apartment.</span></details>
        </div>
      <div class="details-box" data-role="details" ${state.cache.showDetails ? '' : 'hidden'}>
        <p class="small"><b>How a matching run works.</b> Every ${state.dataset === 'test' ? 'test' : 'real'} enquiry that is new or in a proposed group is checked against the rules and compared with every other enquiry; candidate groupings that pass every rule at an available apartment are formed within a bounded search (the run says when that bound was reached), and one proposed group is chosen per person. A group keeps its AI assessment only while its members, apartment offer and recorded facts are unchanged; every new or changed group, and any group whose evidence changed, is queued and the AI assesses it (${runs.ai_batch} groups per call, ${runs.ai_workers} calls in parallel, up to ${aiq.attempts} attempts; failures are labelled for retry) and ranks the apartments it is best suited to. Only then is it Ready for reservations. “Run matching now” does exactly what the hourly run does, without waiting for the hour.</p>
      <div class="card card--tight" data-role="run">${runPanelHtml(last)}</div>
      <div class="card card--tight mt" data-role="aiq">${aiQueueHtml(aiq)}</div>
      <div class="card card--tight mt" data-role="sched"><h3>Hourly matching</h3>${schedulerHtml(sched, true)}<div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="check-now">Run the hourly check now</button><span class="small muted">Same rule: runs only if a new applicant has arrived.</span></div></div>
      </div>
      <div class="toolbar toolbar--plain">
        <select data-role="status"><option value="">All statuses</option>${['Proposed', 'Introduced', 'Group confirmed', 'Awaiting replacement', 'Leased'].map(s => `<option ${f.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select>
        <select data-role="ai"><option value="">Ready or pending</option><option value="ready" ${f.ai === 'ready' ? 'selected' : ''}>Ready for reservations (travel not confirmed)</option><option value="pending" ${f.ai === 'pending' ? 'selected' : ''}>AI assessment pending</option><option value="failed" ${f.ai === 'failed' ? 'selected' : ''}>AI assessment failed or held for retry</option></select>
        <label class="small"><input type="checkbox" data-role="alts" ${f.alts ? 'checked' : ''}> Show alternative groupings</label>
        <span class="small muted">${grp.items.filter(g => g.is_primary !== false).length} group(s) · ${grp.items.filter(g => g.is_primary && g.ready_for_reservations).length} ready for reservations (travel not confirmed)${grp.items.filter(g => g.is_primary && g.status === 'Proposed' && !g.ready_for_reservations).length ? ` · ${grp.items.filter(g => g.is_primary && g.status === 'Proposed' && !g.ready_for_reservations).length} waiting for the AI` : ''}</span>
      </div>
      <div class="groups" data-role="groups">${grp.items.map(g => groupCard(g, { compact: true })).join('') || '<p class="muted">No groups yet. Click “Run matching now”.</p>'}</div>`;
    p.querySelector('[data-action="toggle-details"]').addEventListener('click', e => { e.preventDefault(); const d = p.querySelector('[data-role="details"]'); d.hidden = !d.hidden; state.cache.showDetails = !d.hidden; e.currentTarget.setAttribute('aria-expanded', String(!d.hidden)); });
    p.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));  // inc1b: 'Outcomes by person' had no listener
    p.querySelector('[data-role="status"]').addEventListener('change', e => { state.cache.grpFilter = { ...f, status: e.target.value }; render(); });
    p.querySelector('[data-role="ai"]').addEventListener('change', e => { state.cache.grpFilter = { ...f, ai: e.target.value }; render(); });
    p.querySelector('[data-role="alts"]').addEventListener('change', e => { state.cache.grpFilter = { ...f, alts: e.target.checked }; render(); });
    const runBox = p.querySelector('[data-role="run"]'), aiBox = p.querySelector('[data-role="aiq"]');
    const buttons = [...p.querySelectorAll('[data-action^="run"], [data-action="check-now"]')];
    let qTimer = null;
    const stopQ = () => { if (qTimer) { clearInterval(qTimer); qTimer = null; } };
    const watchQueue = () => {
      stopQ();
      qTimer = setInterval(async () => {
        if (!document.body.contains(aiBox)) { stopQ(); return; }
        try { const q = await api.get('/api/ai-queue'); aiBox.innerHTML = aiQueueHtml(q); const ds = (q.per_dataset || {})[state.dataset] || {}; if (!((ds.pending || 0) + (ds.running || 0) + (ds.refresh || 0)) && !q.busy) { stopQ(); render(); } }
        catch (ex) { stopQ(); }
      }, 2000);
    };
    { const ds = (aiq.per_dataset || {})[state.dataset] || {}; if ((ds.pending || 0) + (ds.running || 0) + (ds.refresh || 0) || aiq.busy) watchQueue(); }
    aiBox.addEventListener('click', async e => {
      const b = e.target.closest('button[data-action]'); if (!b) return;
      b.disabled = true;
      try {
        if (b.dataset.action === 'ai-retry-all') await api.post('/api/ai-queue/retry', {});
        if (b.dataset.action === 'ai-reassess') { if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.disabled = false; b.textContent = 'Click again to re-assess every group'; return; } await api.post('/api/ai-queue/reassess', { dataset: state.dataset }); }
        aiBox.innerHTML = aiQueueHtml(await api.get('/api/ai-queue')); watchQueue();
      } catch (ex) { msg(aiBox, ex.message, false); b.disabled = false; }
    });
    const watch = async id => {
      buttons.forEach(b => b.disabled = true);
      stopPoll();
      pollTimer = setInterval(async () => {
        try {
          const st = await api.get(`/api/match/runs/${id}`);
          runBox.innerHTML = runPanelHtml(st);
          if (st.status !== 'running') { stopPoll(); buttons.forEach(b => b.disabled = false); if (st.status === 'done') { stopQ(); render(); } }
        } catch (ex) { stopPoll(); runBox.innerHTML = `<p class="msg err">${esc(ex.message)}</p>`; buttons.forEach(b => b.disabled = false); }
      }, 1500);
    };
    if (last && last.status === 'running') watch(last.run_id);
    buttons.forEach(b => b.addEventListener('click', async () => {
      if (b.dataset.action === 'check-now') { try { b.disabled = true; const r = await api.post('/api/scheduler/check', { dataset: state.dataset }); const c = r.checks[0]; p.querySelector('[data-role="sched"]').innerHTML = `<h3>Hourly matching</h3><p class="msg ${c.outcome === 'ran' ? 'ok' : ''}">Check at ${fmtDT(c.checked_at)}: <b>${esc(c.outcome)}</b> · ${esc(c.reason || '')}${c.run_id ? ` · run #${c.run_id}` : ''}</p>` + schedulerHtml(r.status, true); if (c.outcome === 'ran') watch(c.run_id); else b.disabled = false; } catch (ex) { msg(p.querySelector('[data-role="sched"]'), ex.message, false); b.disabled = false; } return; }
      try { const st = await api.post('/api/match/run-all', { dataset: state.dataset }); runBox.innerHTML = runPanelHtml(st); watch(st.run_id); }
      catch (ex) { runBox.innerHTML = `<p class="msg err">${esc(ex.message)}</p>`; }
    }));
    p.addEventListener('click', e => { const b = e.target.closest('[data-action="open-group"]'); if (b) navigate('groups', b.dataset.id); const a = e.target.closest('[data-apt-link]'); if (a) { e.preventDefault(); navigate('apartments', a.dataset.aptLink); } });
  };

  // ---- Groups ----
  const shareRange = rooms => { const sh = rooms.flatMap(r => r.occupants.map(o => o.share)).filter(x => x != null); return sh.length ? (Math.min(...sh) === Math.max(...sh) ? `${money(sh[0])} each` : `${money(Math.min(...sh))}–${money(Math.max(...sh))} each`) : ''; };
  function roomsHtml(rooms) {
    return `<div class="rooms">${rooms.map(r => `<div class="room"><b>Bedroom ${r.room} · ${esc(r.setup.join(' + '))}</b>${r.room_cost != null ? `<span class="room__cost mono small">${money(r.room_cost)}/wk room${r.extra_bed ? ` <span class="badge badge--test">incl. $${r.extra_bed} extra bed</span>` : ''}</span>` : ''}<ul>${r.occupants.map(o => `<li>${esc(o.name)} <span class="small muted">· ${esc(o.bed)} · ${esc((o.arrangement || []).join(', '))} · guide share <b>${money(o.share)}</b>/wk</span></li>`).join('')}${Array.from({ length: r.empty_beds || 0 }).map(() => '<li class="empty-bed"><span class="badge badge--flag">empty bed</span> <span class="small muted">awaiting a replacement</span></li>').join('')}</ul></div>`).join('')}</div>`;
  }
  const takenHtml = a => [...(a.taken_by || []).map(t => `<span class="badge badge--leased" title="Reservations marked this apartment Leased${t.group_id ? ` for Group ${t.group_id}` : ''}">leased${t.group_id ? ` · Group ${t.group_id}` : ''}</span>`),
    ...(a.also_first_choice_of || []).map(t => `<span class="badge" title="Another proposed group also has this as its best-suited apartment. Nothing is held: whichever group leases first takes it">also best fit for Group ${t.group_id}</span>`)].join(' ');
  // GP14: a listed apartment that a current member has declined (server: declined_apartments on the group) - shown, never offerable
  const declinedHtml = (a, g) => ((g.declined_apartments || {})[String(a.apartment_id)] || []).map(d =>
    `<span class="badge badge--alt" title="Recorded by staff; the apartment stays listed as history and is not offered to this group while the decline stands">declined by ${esc(d.name)} on ${fmtD(d.at)}${d.scope === 'building' ? ' (whole building)' : ''}</span>`).join(' ');
  const practicalHoldHtml = a => a.practical_need_hold
    ? `<p class="msg small" data-role="apartment-practical-hold"><b>Not a confirmed suitable option.</b> ${esc(a.practical_need_reason || 'An essential practical need requires confirmation.')}<br>Selecting it keeps the group held for staff review.</p>` : '';
  function aptRowHtml(a, g, opts) {
    const cur = a.is_current;
    const declined = !!((g.declined_apartments || {})[String(a.apartment_id)] || []).length;
    return `<li class="apt-opt ${cur ? 'is-current' : ''} ${(a.taken_by || []).length || declined ? 'is-taken' : ''}">
      <span class="apt-opt__rank mono">${a.rank}</span>
      <div class="apt-opt__body"><b><a href="#/staff/apartments/${a.apartment_id}" data-apt-link="${a.apartment_id}">${esc(a.address)}</a></b> <span class="small muted">${esc(a.type_label || a.type_code)} · ${esc(a.suburb || '')} · ${fmtD(a.lease_start)} → ${fmtD(a.lease_end)} · ${a.rate_kind === '6m' ? '6-month rate' : '12-month rate'}${a.bills_included ? ' · bills incl.' : ''}</span>
        <div class="small"><span class="mono"><b>${money(a.total_weekly)}/wk</b> whole apartment${a.extra_bed ? ` (incl. $${a.extra_bed} extra bed)` : ''}</span> · shares ${shareRange(a.rooms || [])} ${cur ? '<span class="badge badge--ok">current</span>' : ''} ${a.shortlist_role === 'reserve' ? '<span class="badge" data-role="apartment-reserve" title="Not one of the group\'s three shortlisted apartments. It moves up only when a shortlisted apartment is leased or declined">reserve</span>' : ''} ${takenHtml(a)} ${declinedHtml(a, g)}</div>
        ${practicalHoldHtml(a)}
        ${a.ai_reason ? `<p class="small apt-opt__why">${esc(a.ai_reason)}</p>` : `<p class="small muted apt-opt__why">Rule preference score ${Math.round(a.rule_score)}/100${a.rule_rank ? ` (rule rank ${a.rule_rank})` : ''}.</p>`}</div>
      ${opts.offer && !cur && !declined && a.shortlist_role !== 'reserve' && !(a.taken_by || []).length && ['Proposed', 'Introduced', 'Group confirmed'].includes(g.status) ? `<button class="btn btn--ghost btn--sm" type="button" data-offer="${a.apartment_id}" title="${a.practical_need_hold ? 'Select this candidate for staff review; the group stays held until its essential need is confirmed' : 'Offer this apartment from the group\'s own ranked list instead'}">${a.practical_need_hold ? 'Select for staff review' : 'Offer this apartment'}</button>` : ''}
    </li>`;
  }
  const FLAG_LABELS = { lease: 'Moved after a lease', stock: 'Apartment changed', rejection: 'Member rejected', bar: 'Member barred', replacement: 'Replacement accepted', dissolved: 'Dissolved by staff', mate: 'Friend brought along' };
  const flagLabel = g => FLAG_LABELS[g.flag_kind] || (g.status === 'Dissolved' ? 'Dissolved' : 'Note');
  /* MateBot groups: travel is checked last, live, and never saved. Until a check has been run on the group page and the AI has
     assessed its results, the apartments listed for a group are candidates and are labelled so; they never read as confirmed. */
  function travelProvisionalHtml(g, opts = {}) {
    if (['Leased', 'Dissolved'].includes(g.status) || !(g.apartments || []).length) return '';
    const live = g.travel_live || {};
    return `<p class="msg small" data-role="travel-provisional" data-state="${live.enabled ? 'live' : 'off'}"><b>Provisional.</b> Travel to campus or work is checked last and has not been checked for the apartments listed here, so they are candidates and not a confirmed shortlist. ${
      live.enabled ? (opts.offer ? 'The live travel check below shows the result, with the AI\'s assessment; it is not saved.' : 'Open the group to run the live travel check; its result is shown there and is not saved.')
        : esc(live.plain || 'Live travel checks are switched off.') + ' Travel time needs confirmation before an apartment is treated as suitable.'}</p>`;
  }
  const mbTravelShown = new Map();
  function groupTravelView(r) {
    if (!r) return '';
    if (!r.ok) return `<p class="msg small" data-role="mb-travel-refused" data-state="${esc(r.state || '')}">${esc(r.plain)}</p>`;
    const k = r.check || {}, gate = r.ai_gate || {}, ai = r.ai_reply || {};
    const missed = (k.failures || 0) + (k.not_requested || 0);
    const sources = x => {
      const src = x.travel_sources || {};
      const links = (src.links || []).filter(q => /^https:\/\//.test(q.url || '')).map(q => `<a href="${esc(q.url)}" target="_blank" rel="noopener noreferrer">${esc(q.title)}</a>`);
      if (!x.measured && !links.length) return '';
      return `<p class="small" data-role="mb-travel-sources">Sources: ${GMAPS_TEXT}${links.length ? ' · ' + links.join(' · ') : ''}</p>${src.walking_warning ? `<p class="small muted" data-role="mb-walk-warning">${esc(src.walking_warning)}</p>` : ''}`;
    };
    const aiNote = x => {
      const a = x.ai_travel;
      if (!a) return '';
      return `<p class="small" data-role="mb-travel-ai-view"><b>AI assessment${a.of > 1 ? ` (its choice ${a.preference} of ${a.of})` : ''}.</b> Travel: ${esc(a.travel_rating_plain)}.
        ${(a.strengths || []).length ? `<br>Strengths: ${a.strengths.map(esc).join(' ')}` : ''}${(a.weaknesses || []).length ? `<br>Weaknesses: ${a.weaknesses.map(esc).join(' ')}` : ''}
        <br>Advice: ${esc(a.advice_plain)}</p>
        <p class="small" data-role="mb-travel-ai-note"><b>Journey figures the AI reported.</b> ${esc(a.note)}<br>
        Each person against their own stated time: ${a.within_own_limit} within, ${a.over_own_limit} over, ${a.not_known} not known, ${a.no_stated_limit} with no stated time${a.no_destination ? `, ${a.no_destination} with no destination to measure to` : ''}.
        Group result: ${esc(a.group_plain)}.</p>`;
    };
    const item = (x, label) => `<li class="apt-opt" data-role="mb-travel-option" data-result="${esc(x.role)}" data-apartment="${x.apartment_id}">
      <div class="apt-opt__body"><b>${esc(x.address || '')}</b> <span class="small muted">${esc(x.type_label || '')} · ${esc(x.suburb || '')} · ${money(x.total_weekly)}/wk whole apartment</span>
        <span class="badge ${x.role === 'failed' ? 'badge--flag' : gate.held ? 'badge--alt' : 'badge--ok'}">${esc(label)}</span>${x.is_current ? ' <span class="badge">current offer</span>' : ''}${x.replacement ? ' <span class="badge badge--alt">moved up after another failed travel</span>' : ''}
        <p class="small"><b>${esc(x.result_plain)}</b>${(x.reasons_plain || []).length ? ' Why: ' + x.reasons_plain.map(esc).join('; ') + '.' : ''}</p>
        ${x.coverage ? `<p class="small${x.coverage.complete ? ' muted' : ' msg'}" data-role="mb-travel-coverage" data-complete="${x.coverage.complete ? '1' : '0'}"><b>Who this covers.</b> ${esc(x.coverage.plain)}</p>` : ''}
        ${x.practical_need_hold ? `<p class="small msg">${esc(x.practical_need_reason || 'An essential practical need is not confirmed for this apartment.')}</p>` : ''}
        ${aiNote(x)}
        <details class="small" ${x.role === 'failed' ? '' : 'open'}><summary>Journey estimates for each person</summary>${(x.journey_lines || []).map(esc).join('<br>')}${x.measured ? '<br>' + GMAPS_ATTR : ''}</details>
        ${sources(x)}</div></li>`;
    const provisional = gate.held !== false;
    return `<div data-role="mb-travel-result"><p class="small">${GMAPS_ATTR}<br>Journey estimates. ${esc(k.basis || '')}. Checked ${fmtDT(k.checked_at)}.</p>
      ${r.current_offer ? `<p class="msg ${r.current_offer.state === 'failed' ? 'err ' : r.current_offer.state === 'confirmed' ? 'ok ' : ''}small" data-role="mb-travel-offer" data-state="${esc(r.current_offer.state || '')}"><b>${r.current_offer.state === 'failed' ? 'Current offer: failed travel.' : r.current_offer.state === 'confirmed' ? 'Current offer: travel confirmed.' : r.current_offer.state === 'none' ? 'No current offer.' : 'Current offer: provisional.'}</b> ${esc(r.current_offer.plain)}</p>` : ''}
      ${gate.plain ? `<p class="${gate.held ? 'msg ' : ''}small" data-role="mb-travel-gate" data-state="${esc(gate.state || '')}"><b>${gate.held ? 'Held.' : 'AI-assessed.'}</b> ${esc(gate.plain)}</p>` : ''}
      ${ai.plain ? `<p class="small${ai.state === 'ready' ? '' : ' muted'}" data-role="mb-travel-ai" data-state="${esc(ai.state || '')}">${esc(ai.plain)}</p>` : ''}
      <p class="small muted">The apartments are chosen without travel first. Travel is checked last, for up to ${r.shortlist_max || 3} apartments; one that fails is replaced by the next stored candidate and the replacement is checked. ${esc(r.plain)}</p>
      ${r.limit_note ? `<p class="msg small" data-role="mb-travel-limit-note">${esc(r.limit_note)}</p>` : ''}
      ${missed ? `<p class="msg small">${missed} journey${missed === 1 ? '' : 's'} could not be checked this time; where a time is missing the apartment reads "Travel time needs confirmation".</p>` : ''}
      ${!k.requests ? '<p class="small muted">No journeys were requested: no member has both a place of study or work that a journey can be measured to and an answer for how they would travel.</p>' : ''}
      ${(r.shortlist || []).length ? `<h4 class="small">${provisional ? 'Provisional shortlist (not confirmed)' : 'Shortlist'} · up to ${r.shortlist_max || 3}</h4><ol class="apt-list">${r.shortlist.map(x => item(x, provisional ? 'provisional' : 'AI-assessed')).join('')}</ol>` : '<p class="small">No apartment is left on the shortlist after the travel check.</p>'}
      ${(r.failed || []).length ? `<h4 class="small">Failed the travel check (not on the shortlist)</h4><ol class="apt-list">${r.failed.map(x => item(x, 'failed travel')).join('')}</ol>` : ''}
      ${(r.not_checked || []).length ? `<p class="small muted" data-role="mb-travel-not-checked">Travel not checked (reserves, no place open): ${r.not_checked.map(x => esc(x.address || ('apartment ' + x.apartment_id))).join('; ')}.</p>` : ''}
      <details class="small"><summary>Who was counted (${(r.people || []).length} people)</summary>${(r.people || []).map(q => `Person ${q.position}${q.name ? ' (' + esc(q.name) + ')' : ''}: ${
        q.destination_shown ? (q.destination_kind === 'work' ? 'works at ' : 'studies at ') + esc(q.destination_shown)
          : q.needs_confirmation ? 'travel time needs confirmation, ' + esc(q.needs_confirmation) : esc(q.no_destination || 'no destination')}; maximum travel time ${q.stated_time ? esc(q.stated_time) : 'not stated'}; would travel by ${(q.travel_choices || []).length ? q.travel_choices.map(esc).join(', ') : 'not answered'}`).join('<br>')}</details></div>`;
  }
  /* The group page's live travel check: run once when the page opens (when that is switched on) and again on request. The reply
     is kept in this page only, for at most 30 minutes, so a redraw does not ask again; nothing is saved anywhere. */
  function groupTravelPanel(box, g, onChange) {
    const live = g.travel_live || {};
    if (!box || !live.enabled || ['Leased', 'Dissolved'].includes(g.status) || !(g.apartments || []).length) { if (box) box.innerHTML = ''; return; }
    mbTravelShown.forEach(kept => { if (kept.reply && Date.now() - kept.at > HB_TRAVEL_KEEP_MS) kept.reply = null; });
    // What this page holds for a group is held for ONE state of it: `live.key` is the server's name for the group, its
    // apartments (address, availability) and every member's travel answers as they stand. Anything changes, the key changes,
    // and nothing held for the earlier state (a result, a known failure) is shown or acted on again.
    const key = `${g.id}:${live.key || ''}:${g.apartment_id}:${(g.apartments || []).map(a => a.apartment_id + '.' + a.shortlist_role).join(',')}:${(g.members || []).map(m => m.enquiry_id).join(',')}`;
    const kept = mbTravelShown.get(key) || { at: 0, reply: null, busy: false, tried: false, note: '', noteErr: false, show: null, fail: null };
    mbTravelShown.set(key, kept);
    box.innerHTML = `<div class="card card--tight mt" data-role="mb-travel"><h3>Travel check · live</h3>
      <div class="actions"><button class="btn btn--sm" type="button" data-action="mb-travel">${kept.reply ? 'Check travel times again' : 'Check travel times now'}</button></div>
      <p class="small muted">${esc(live.plain || '')}</p>
      <div data-role="mb-travel-out" aria-live="polite">${kept.reply ? groupTravelView(kept.reply) : ''}</div></div>`;
    const out = box.querySelector('[data-role="mb-travel-out"]'), btn = box.querySelector('button[data-action="mb-travel"]');
    // the latest drawing of this group's panel shows where the one check stands, so a redraw while a check is running
    // shows "checking" and then its result, and never starts a second one
    kept.show = () => {
      if (!out.isConnected) return;
      btn.disabled = kept.busy;
      btn.textContent = kept.reply ? 'Check travel times again' : 'Check travel times now';
      out.innerHTML = kept.busy ? '<p class="small muted">Checking travel times… this can take up to a minute.</p>'
        : kept.reply ? groupTravelView(kept.reply)
        : kept.note ? `<p class="${kept.noteErr ? 'msg err small' : 'small muted'}">${esc(kept.note)}</p>` : '';
      // the rest of the group page (readiness, the offer and introduction buttons) follows what this check shows now
      if (!onChange) return;
      if (!kept.busy && kept.reply && kept.reply.ok) {
        // a completed check is the only thing that sets, or lifts, a known failure
        const ids = (kept.reply.failed || []).map(x => x.apartment_id), cur = kept.reply.current_offer || {};
        kept.fail = cur.state === 'failed' || ids.length ? { offer: cur.state === 'failed', plain: cur.plain || '', ids } : null;
        onChange({ ...cur, failed: ids });
        return;
      }
      // No completed check to show: one is running, it could not run, or its journey times were cleared. A failure this page
      // already knows for the group as it stands is still a failure until a completed check says otherwise, so what it
      // switched off stays off. Nothing else is carried over: a pass is never remembered.
      const why = kept.busy ? 'Travel is being checked again.'
        : kept.reply ? `Travel was not checked again. ${kept.reply.plain || ''}`
        : kept.stale ? 'Reload the group to check travel for it as it stands.' : 'Its journey times are no longer shown. Check travel times again to see current ones.';
      if (kept.fail && kept.fail.offer) onChange({ state: 'failed', plain: `${kept.fail.plain} ${why}`.trim(), failed: kept.fail.ids, held: true });
      else if (kept.busy) onChange({ state: 'checking', failed: kept.fail ? kept.fail.ids : [] });
      else if (kept.reply) onChange({ state: 'provisional', why: 'not_checked', failed: kept.fail ? kept.fail.ids : [], plain: `Travel was not checked for the apartment currently offered. ${kept.reply.plain || ''} This is provisional: not a confirmed pass and not a failure.` });
      else onChange(kept.fail ? { ...(g.travel_offer || { state: 'provisional', plain: '' }), failed: kept.fail.ids } : null);
    };
    const run = async asked => {
      if (kept.busy) return;   // one request at a time for this group: a second click or redraw while one is running asks nothing
      kept.busy = true; kept.tried = true; kept.note = ''; kept.stale = false;
      kept.show();
      try {
        const reply = await api.post(`/api/groups/${g.id}/travel-check`, {});
        if (reply && reply.ok && (live.key_unavailable || (live.key && reply.key !== live.key))) {
          // The check was made for the group as it is now, and this page cannot tell that is the group it is showing: it
          // changed after the page was opened, or its state could not be named when it was. The result is not used. A
          // failure this page already knows is NOT lifted by it: only a completed check for the state on the page does that.
          kept.reply = null; kept.noteErr = true; kept.stale = true;
          kept.note = live.key_unavailable ? 'Travel for this group cannot be confirmed on this page: its travel details could not be read in full. Reload the group; if this stays, check the members\' travel answers.'
            : 'The group, one of its apartments or a travel answer changed after this page was opened. Reload the group to check travel for it as it stands.';
          return;
        }
        kept.reply = reply;
        kept.at = Date.now();
        const shownAt = kept.at;
        setTimeout(() => {
          if (kept.at !== shownAt || !kept.reply) return;   // a later check took its place, or it was already dropped: nothing to clear
          kept.reply = null; kept.noteErr = false;
          kept.note = 'The journey times shown earlier have been cleared. Check travel times again to see current ones.';
          kept.show();
        }, HB_TRAVEL_KEEP_MS);
      } catch (ex) {
        kept.reply = null; kept.noteErr = true;
        kept.note = asked ? ex.message : 'Travel times could not be checked just now. Use the button to try again.';
      } finally { kept.busy = false; kept.show(); }
    };
    btn.addEventListener('click', () => run(true));
    kept.show();
    // when the group opens, check travel once for this group as it stands. Not again on a redraw, not again after the shown
    // result is cleared and not again after a failed attempt: only the button asks again. Nothing is kept beyond this page.
    if (live.auto && !kept.tried) run(false);
  }

  function groupCard(g, opts = {}) {
    const apts = g.apartments || [];
    const top = apts.find(a => a.is_current) || apts[0];
    const rooms = (top && top.rooms) || (g.allocation && g.allocation.rooms) || [];
    const ai = g.ai_rating != null;
    const ruleScore = g.rule_score == null ? 'not recorded' : `${Math.round(g.rule_score)}/100`;
    const names = (g.members || []).map(m => `${esc(m.first_name)} ${esc(m.last_name)}${m.party && m.party !== 'Alone' ? ` <span class="small muted">(${m.party.startsWith('Couple') ? 'couple' : `×${m.party_size}`})</span>` : ''}`).join(', ');
    const others = apts.filter(a => a !== top);
    const nextBest = g.next_best;
    return `<article class="group ${g.is_primary === false ? 'group--alt' : ''}" data-group="${g.id}">
      <div class="group__head">
        <span class="rating ${ai ? '' : 'rating--rules'}" title="${ai ? 'AI compatibility rating out of 10' : g.rule_score == null ? 'No current preference score recorded' : 'Rule-based preference score'}">${ai ? Number(g.ai_rating).toFixed(g.ai_rating % 1 ? 1 : 0) : g.rule_score == null ? '?' : Math.round(g.rule_score)}</span>
        <div><h4>Group ${g.id}${names ? ` · ${names}` : ''}</h4>
          <span class="small muted">${g.people} ${g.people === 1 ? 'person' : 'people'} · ${apts.length ? `${apts.length} apartment candidate${apts.length > 1 ? 's' : ''}` : (g.address ? esc(g.address) : '')}</span></div>
        ${badge(g.status)}${stateBadge(g)}${g.is_primary === false ? '<span class="badge badge--alt" title="Its members are already in a better proposed group">alternative</span>' : ''}${g.is_test ? '<span class="badge badge--test">TEST</span>' : ''}${ai ? `<span class="badge badge--ai">AI${g.ai_refresh ? ' · apartment ranking refresh queued' : ''}</span>` : ''}
        <span class="spacer"></span>
        ${opts.compact ? `<button class="btn btn--ghost btn--sm" type="button" data-action="open-group" data-id="${g.id}">Open</button>` : ''}
      </div>
      ${g.status === 'Awaiting replacement' ? `<div class="rerank rerank--awaiting"><span class="badge badge--flag">Awaiting a replacement</span><span>${esc(g.awaiting_note || '')}${g.replacement ? ` <b>A replacement that passes every rule is now available: ${g.replacement.map(r => esc(r.added_names.join(' + '))).join(' / ')}.</b> Open the group to accept or decline it; nothing is applied automatically.` : g.replacement_checked_at ? ` Last checked ${fmtDT(g.replacement_checked_at)}: no rule-passing replacement yet.` : ''}</span></div>` : g.awaiting_note && g.status !== 'Dissolved' ? `<p class="small muted">${esc(g.awaiting_note)}${g.empty_beds ? ` ${g.empty_beds} empty bed(s) remain.` : ''}</p>` : ''}
      ${g.rerank_note ? `<div class="rerank"><span class="badge badge--flag">${flagLabel(g)}</span><span>${esc(g.rerank_note)}</span><span class="spacer"></span>${opts.offer && g.status !== 'Dissolved' ? `<button class="btn btn--ghost btn--sm" type="button" data-action="rerank-ack" title="Clear this flag once the team has seen it">Seen</button>` : ''}</div>` : ''}
      ${g.stock_recheck_pending ? `<div class="rerank rerank--awaiting"><span class="badge badge--flag">Re-check pending</span><span>An apartment on this group's list changed; MateBot AI is re-checking the group's apartments. It is not ready for reservations until that finishes.</span></div>` : ''}
      ${g.offer_changed_since_intro && !['Leased', 'Dissolved'].includes(g.status) ? `<div class="rerank"><span class="badge badge--flag">Offer changed since the introduction</span><span>The apartment offered to this group changed after it was introduced. Check the current apartment with the group${g.status === 'Proposed' ? '' : '; anyone opening their response link sees that the apartment details have changed'}.</span><span class="spacer"></span>${opts.offer ? `<button class="btn btn--ghost btn--sm" type="button" data-action="offer-ack" title="Record that the team has gone through the current introduction and apartment offer with the group (requires a valid current apartment and a current introduction draft; membership responses and the 24 h clock are not changed)">Acknowledge current introduction and offer</button>` : ''}</div>` : ''}
      ${(g.linked_parties || []).length ? `<p class="small keep-together"><b>Keep together:</b> ${g.linked_parties.map(pt => esc(pt.names.join(' + '))).join('; ')} <span class="muted">(linked friends who came through a "Bring a mate" link; they are never split up)</span></p>` : ''}
      ${(g.mate_requests || []).length ? g.mate_requests.map(r => `<div class="rerank rerank--awaiting" data-mate-request="${r.id}"><span class="badge badge--flag">Friend waiting to join</span><span><b>${esc(r.friend_name)} ${esc(r.friend_last || '')}</b> came through ${esc(r.root_name)}'s link and passes every rule for this group${r.replace_names.length ? ` if <b>${esc(r.replace_names.join(' and '))}</b> make${r.replace_names.length === 1 ? 's' : ''} room (they go back into matching; not a rejection)` : ''}. This group has already been shared with its members, so the change needs your approval. If you decline, speak with ${esc(r.root_name)} first: they choose either to leave the group with ${esc(r.friend_name)} (matched together again) or to stay without ${esc(r.friend_name)}, who is then matched on their own.</span>
          ${opts.offer ? `<span class="spacer"></span><button class="btn btn--sm" type="button" data-action="mate-approve" data-rid="${r.id}">Approve</button><button class="btn btn--ghost btn--sm" type="button" data-action="mate-decline" data-rid="${r.id}">Decline…</button>
          <span class="decline-choice" data-decline-choice="${r.id}" hidden><span class="small">Declined. ${esc(r.root_name)} chose to:</span> <button class="btn btn--ghost btn--sm" type="button" data-action="mate-decline-go" data-rid="${r.id}" data-choice="leave">Leave with ${esc(r.friend_name)}</button> <button class="btn btn--ghost btn--sm" type="button" data-action="mate-decline-go" data-rid="${r.id}" data-choice="stay">Stay; ${esc(r.friend_name)} is matched alone</button></span>` : `<a class="small" href="#/staff/groups/${g.id}">Open to decide</a>`}</div>`).join('') : ''}
      ${g.replaced_from_group_id ? `<p class="small muted">Replaced Group ${g.replaced_from_group_id}: ${g.flag_kind === 'mate' || /friend/.test(g.awaiting_note || '') ? 'a member brought a friend along and the group was rebuilt around them' : (g.rejected || []).length ? `staff rejected ${g.rejected.length} member(s) and the AI refilled the group` : 'a member was barred from matching and the AI refilled the group'}.</p>` : ''}
      ${noCurrentApartment(g) ? `<p class="rationale rationale--failed"><b>Held: no current apartment.</b> The group is not ready to share. Check the current requirements and re-check its apartment options${opts.offer ? ' using the staff control below' : ' from the group detail page'}. Retrying the AI alone cannot supply a suitable apartment.</p>` : g.ai_rationale ? `<p class="rationale"><b>AI assessment${g.status === 'Awaiting replacement' ? ' (of the group before the member left; the AI assesses the refilled group before it is ready again)' : ''}:</b> ${esc(g.ai_rationale)}${g.ai_assessed_at ? ` <span class="small muted">(${fmtDT(g.ai_assessed_at)})</span>` : ''}</p>`
        : g.ai_status === 'failed' ? `<p class="rationale rationale--failed"><b>AI assessment failed.</b> ${esc(g.ai_error || 'The AI assessment did not complete')}. The group is NOT ready for reservations; its apartments are in rule order (preference score ${ruleScore}) until the AI has assessed it. ${opts.offer ? '<button class="btn btn--sm" type="button" data-action="ai-retry">Retry AI assessment</button>' : 'Open the group to retry.'}</p>`
        : g.review_state === 'AI assessment held' && g.ai_retry_available ? `<p class="rationale rationale--failed"><b>AI assessment held.</b> ${esc((g.hold && g.hold.label) || 'The AI could not read a member\'s notes after the automatic retries')}. The group is NOT ready for reservations and has not been assessed. ${opts.offer ? '<button class="btn btn--sm" type="button" data-action="ai-retry">Retry AI assessment</button>' : 'Open the group to retry.'}</p>`
        : ['Leased', 'Dissolved'].includes(g.status) ? `<p class="rationale rationale--rules"><b>Rules ranking.</b> Preference score ${ruleScore}.</p>`
        : `<p class="rationale rationale--pending"><b>AI assessment pending.</b> The rules formed this group${g.ai_queued_at ? ` (queued ${fmtDT(g.ai_queued_at)})` : ''}; the AI is assessing how well these people will get on and ranking the apartments below, best first. Until then the apartments are in rule order (preference score ${ruleScore}) and the group is not shown to reservations as ready.</p>`}
      ${top ? `<div class="best-apt"><div class="best-apt__head"><span class="eyebrow">${top.practical_need_hold ? 'Apartment requires confirmation' : top.rank === 1 ? 'Best-suited apartment' : `Current apartment (ranked #${top.rank})`}</span>
            <b><a href="#/staff/apartments/${top.apartment_id}" data-apt-link="${top.apartment_id}">${esc(top.address)}</a></b> <span class="small muted">${esc(top.type_label || top.type_code)} · ${esc(top.suburb || '')} · ${fmtD(top.lease_start)} → ${fmtD(top.lease_end)} · ${top.rate_kind === '6m' ? '6-month rate' : '12-month rate'} · ${esc(top.exit_window || '')}</span>
            <span class="spacer"></span><span class="mono"><b>${money(top.total_weekly)}/wk</b> whole apartment${top.extra_bed ? ` <span class="small">(incl. $${top.extra_bed} extra-bed charge)</span>` : ''}</span> ${takenHtml(top)}</div>
          ${practicalHoldHtml(top)}
          ${top.ai_reason ? `<p class="small apt-opt__why">${esc(top.ai_reason)}</p>` : ''}
          <p class="liability small">${esc(top.liability_note || g.liability_note || '')}</p>
          ${roomsHtml(rooms)}
          ${(top.taken_by || []).length ? `<p class="msg err small">This apartment has been leased${top.taken_by[0].group_id ? ` (Group ${top.taken_by[0].group_id})` : ''}. ${nextBest ? `Next best: <b>${esc(nextBest.address)}</b> at ${money(nextBest.total_weekly)}/wk.` : 'No other available apartment on this group\'s list; run full matching.'}</p>` : ''}</div>`
        : g.no_current_offer ? `<div class="best-apt"><div class="best-apt__head"><span class="eyebrow">Apartment</span><b>No current offer</b><span class="spacer"></span></div><p class="small muted">No apartment is currently selected for this group.${noCurrentApartment(g) ? ' Check its current requirements and the stock, then use Re-check apartment options on the group page. Its members remain together.' : ''}</p></div>`
        : `<div class="best-apt"><div class="best-apt__head"><span class="eyebrow">Apartment</span><b>${esc(g.address || '')}</b><span class="spacer"></span><span class="mono"><b>${money(g.total_weekly)}/wk</b> whole apartment</span></div><p class="liability small">${esc(g.liability_note || '')}</p>${roomsHtml(rooms)}</div>`}
      ${travelProvisionalHtml(g, opts)}
      ${others.length ? `<details class="more apt-more" ${opts.offer ? 'open' : ''}><summary>Other apartment candidates (${others.length}) · check any warnings before offering; nothing is reserved</summary>
          ${others.some(a => a.shortlist_role === 'reserve') ? `<p class="small muted" data-role="shortlist-limit">At most ${g.shortlist_max || 3} apartments are shortlisted for a group, the current one included. The others are reserves, kept in their ranked order: one moves up only when a shortlisted apartment is leased or declined.</p>` : ''}
          <ol class="apt-list">${others.map(a => aptRowHtml(a, g, opts)).join('')}</ol></details>` : ''}
    </article>`;
  }

  function recipientsText(g) {
    const list = g.intro_recipients || g.members.filter(m => m.consent_share).map(m => ({ name: m.first_name, email: m.email }));
    return list.length ? list.map(r => `${esc(r.name)} <${esc(r.email)}>`).join(', ') : 'none yet';
  }

  // a stored reason as its own sentence: capitalised, ending in a full stop, followed by a space (empty when there is none)
  const sentence = t => { const w = (t || '').trim(); return w ? w.charAt(0).toUpperCase() + w.slice(1) + (/[.!?]$/.test(w) ? '' : '.') + ' ' : ''; };
  /* Readiness to share an introduction. `travel` is where the apartment currently offered stands on travel: what the server
     sent with the group (never checked: nothing about travel is stored) or what this page's live check has just shown. Green
     only when that check confirmed the offer; a known failure is not ready; anything else is provisional and says so. */
  function readinessHtml(g, travel) {
    if (['Leased', 'Dissolved'].includes(g.status)) return '';
    const t = travel || g.travel_offer || { state: 'provisional', plain: 'Travel to campus or work has not been checked for the apartment currently offered. This is provisional: not a confirmed pass and not a failure.' };
    if (g.ready) {
      if (t.state === 'failed') return `<p class="msg err small" data-role="readiness" data-travel="failed"><b>Not ready to circulate.</b> ${esc(t.plain)}</p>`;
      if (t.state === 'checking') return `<p class="msg small" data-role="readiness" data-travel="checking"><b>Checking travel…</b> The AI review approved this group under the current rules and the staff brief is current. Travel for the apartment currently offered is being checked now; wait for the result before sharing.</p>`;
      // green only with a CURRENT introduction draft; a reviewed group with no draft, or an out-of-date one, is not shareable yet
      if (g.intro && g.intro.current) {
        if (t.state === 'confirmed') return `<p class="msg ok small" data-role="readiness" data-travel="confirmed"><b>Ready to circulate.</b> The AI review approved this group under the current rules, the staff brief is current, every member is only in this proposed group and the introduction below is current. ${esc(t.plain)}</p>`;
        return `<p class="msg small" data-role="readiness" data-travel="provisional"><b>Provisionally ready: travel not confirmed.</b> The AI review approved this group under the current rules, the staff brief is current, every member is only in this proposed group and the introduction below is current. ${esc(t.plain)}</p>`;
      }
      return `<p class="msg small" data-role="readiness" data-travel="${esc(t.state || 'provisional')}"><b>Group reviewed; create a current introduction before sharing.</b> The AI review approved this group under the current rules and the staff brief is current, but ${g.intro && g.intro.text ? 'the introduction draft below is out of date' : 'no introduction has been drafted yet'}. ${esc(t.plain)}</p>`;
    }
    const why = ((g.circulation || {}).reasons || []);
    return `<p class="msg err small"><b>Not ready to circulate.</b> ${why.length ? esc(why.join('; ')) : 'The checks have not completed.'}${g.is_primary === false ? ' This is an alternative grouping shown for comparison only.' : ''}</p>`;
  }

  function briefSectionsHtml(sections) {
    return (sections || []).map(sec => `<div class="brief-sec"><b class="small">${esc(sec.title)}</b><ul class="small">${sec.items.map(it => `<li>${esc(it.note)}${(it.evidence || []).length ? `<details class="evidence"><summary>Based on</summary><ul>${it.evidence.map(e => `<li>${esc(e)}</li>`).join('')}</ul></details>` : ''}</li>`).join('')}</ul></div>`).join('');
  }

  function reviewBriefHtml(g) {
    if (!g.review && !g.brief) return '';
    const r = g.review, b = g.brief;
    return `<div class="card card--tight mt" data-role="review-brief"><h3>AI review and staff brief</h3>
      <p class="small muted">The review checks the group against the rules and the recorded practical answers; the brief tells the team what is likely to work, what could cause issues and what is not recorded. Neither uses anyone's country, sex or age.</p>
      ${g.hold && !g.ready ? `<p class="small hold-reason" data-role="hold-reason"><b>Why it is held:</b> ${esc(g.hold.label)}.</p>` : ''}
      ${r ? `<p class="small ${r.current ? '' : 'muted'}"><b>${esc(r.label)}</b>${r.rating != null ? ` · rating ${Number(r.rating).toFixed(1)}` : ''} · ${fmtDT(r.reviewed_at)}${r.rationale ? `<br>${esc(r.rationale)}` : ''}${r.coverage ? `<br><span class="muted">${esc(r.coverage)}</span>` : ''}${r.names_available === false ? '<br>Written about an earlier set of members, so the people in it are not named here.' : ''}</p>` : ''}
      ${b ? `<div class="${b.current ? '' : 'muted'}"><p class="small"><b>${esc(b.label)}</b> · ${fmtDT(b.written_at)}${b.error ? ` · ${esc(b.error)}` : ''}${b.coverage ? `<br><span class="muted" data-role="brief-coverage">${esc(b.coverage)}</span>` : ''}${b.names_available === false ? '<br>Written about an earlier set of members or facts, so the people in it are not named here.' : ''}</p>${briefSectionsHtml(b.sections)}
        ${(b.members || []).length ? `<details class="more"><summary>Per-person notes (${b.members.length})</summary>${b.members.map(m => `<div class="brief-member"><b class="small">${esc(m.name)}</b>${m.individually_recorded === false ? ' <span class="small muted">(Party-wide preferences were reported by the enquirer; this person’s own lifestyle answers are not recorded.)</span>' : ''}${briefSectionsHtml(m.sections)}</div>`).join('')}</details>` : ''}</div>` : ''}
    </div>`;
  }

  // GP13: each person in an introduced group answers through their own link. This panel shows who has answered, makes the links
  // (shown once, for staff to paste into their own message), and explains in plain words why the group can or cannot be confirmed.
  async function groupResponsesPanel(box, g, introBox, remainingBox) {
    if (!box || ['Leased', 'Dissolved'].includes(g.status)) return;
    let st;
    try { st = await api.get(`/api/groups/${g.id}/responses`); } catch (ex) { box.innerHTML = `<p class="msg err small">${esc(ex.message || 'The answers could not be loaded.')}</p>`; return; }
    const shown = {};   // link_id -> clear link, kept only in this page while it is open
    const roleWord = r => r === 'lead' ? 'enquirer' : 'companion';
    const linkFor = out => `${location.origin}${location.pathname}${out.path}`;
    const remainingBlock = () => {
      // Awaiting a replacement: the group may only be asked (and later confirmed) after staff record that the remaining group and the
      // empty space were explained to everyone. The button carries the exact membership and the exact details shown here; if anything
      // changed underneath, the server refuses and nothing is written.
      const r = st.remaining_group; if (!r) return '';
      const c = r.context || {};
      const peopleText = (c.people || []).map(x => `${esc(x.display_name)} (${x.proposed_weekly_share != null ? money(x.proposed_weekly_share) + '/wk proposed' : 'share not set yet'})`).join(', ');
      const facts = `<ul class="small">
          <li>Apartment: ${esc(c.address || '')}${c.suburb ? `, ${esc(c.suburb)}` : ''}${c.lease_start ? ` · lease ${esc(c.lease_start)}${c.lease_end ? ` to ${esc(c.lease_end)}` : ''}` : ''} · whole rent ${money(c.total_weekly || 0)}/wk</li>
          <li>Remaining people (${r.people_count}): ${peopleText}</li>
          <li>Empty space${r.empty_space_count === 1 ? '' : 's'}: ${r.empty_space_count} · not covered by the current shares: <b>${r.uncovered_weekly == null ? 'not known - the stored bed allocation does not let MateBot AI work it out; check the allocation before quoting a figure' : money(r.uncovered_weekly) + '/wk'}</b></li>
          <li>Discount note: ${r.discount_note ? esc(r.discount_note) : '<span class="muted">none recorded</span>'}</li>
        </ul>`;
      if (r.anchored) {
        return `<div class="mt"><p class="msg ok small"><b>Recorded:</b> the remaining group and the empty space were explained to everyone${st.version && st.version.introduced_at ? ` (${fmtDT(st.version.introduced_at)}${st.version.introduced_by ? ` by ${esc(st.version.introduced_by)}` : ''})` : ''}. Each person now answers through their own link below; everyone must say yes before the group can be marked confirmed. They are saying yes to the housemates, not to the rent.</p>
          ${r.disclosure_changed ? '<p class="msg small">The details above changed since they were recorded as explained (for example the discount note). Nobody\'s answer is reset by that; tell the group yourself.</p>' : ''}${facts}</div>`;
      }
      return `<div class="mt"><h3>Leasing with the empty space</h3>
        <p class="small">Before this group can be asked to confirm, explain to <b>everyone</b> who is left exactly what is shown here, then record it. Nothing is sent by MateBot; no status changes; the clock for their answers starts from this record.</p>
        ${facts}${r.shares_unproven && r.shares_unproven.length ? `<p class="small muted">Share not set yet for ${r.shares_unproven.map(esc).join(', ')}: their page will say so rather than show another person's figure.</p>` : ''}
        <div class="actions"><button class="btn btn--sm" type="button" data-action="introduce-remaining" data-mh="${esc(r.membership_hash || '')}" data-dh="${esc(r.disclosure_hash || '')}">I have explained the remaining group and the empty space to everyone</button></div></div>`;
    };
    const draw = () => {
      const people = st.people || [];
      const rb = remainingBox && remainingBox();
      if (rb) rb.innerHTML = remainingBlock();
      const dueText = st.version && st.version.introduced_at ? `Introduced ${fmtDT(st.version.introduced_at)}${st.version.introduced_by ? ` by ${esc(st.version.introduced_by)}` : ''}; answers are reviewed from ${fmtDT(st.version.review_due_at)}.` : '';
      box.innerHTML = `<div class="card card--tight mt"><h3>Each person's answer</h3>
        ${!st.applies && st.remaining_group ? `<p class="small muted">This group is awaiting a replacement. Once you have recorded (in the panel above) that the remaining group and the empty space were explained to everyone, each person gets their own link to say whether they are happy to share with these housemates.</p>` : ''}
        ${!st.applies && !st.remaining_group ? `<p class="small muted">Once this group is marked introduced, each person gets their own link to say whether they are happy to share with these housemates. Links are made here, one per person, and you paste each into your own message.</p>` : ''}
        ${!remainingBox || !remainingBox() ? remainingBlock() : ''}
        ${st.applies && st.needs_reintroduction ? `<p class="msg err small"><b>The people in this group changed after it was introduced.</b> Everyone must say yes again to the new group. Send the updated introduction yourself, then click the button below; after that make a new link for each person.</p>
          <div class="actions"><button class="btn btn--sm" type="button" data-action="introduce-revised">I have introduced the revised group</button></div>` : ''}
        ${st.applies && !st.needs_reintroduction ? `<p class="small muted">${dueText} ${st.version && st.version.review_due && people.some(x => !x.response) ? '<b>24 hours have passed and not everyone has answered.</b> Talk with the group about waiting or looking for a replacement; nothing is removed automatically.' : ''}</p>` : ''}
        ${st.applies ? `<p class="msg ${st.all_accepted ? 'ok' : ''} small">${st.all_accepted ? 'Everyone in the current group has said yes to sharing with these housemates. You can mark the group confirmed.' : esc(st.confirmation_block || '')}</p>` : ''}
        <p class="small muted">${esc(st.meaning || '')}</p>
        <div class="table-wrap mt"><table class="tbl"><thead><tr><th>Person</th><th>Answer</th><th>Link</th><th></th></tr></thead><tbody>
        ${people.map(x => `<tr>
          <td>${esc(x.display_name)} <span class="muted">· ${roleWord(x.role)}</span></td>
          <td>${x.response ? `<b>${esc(x.response.label)}</b> <span class="muted">${fmtDT(x.response.responded_at)}</span>${x.response.note ? `<br><span class="muted">“${esc(x.response.note)}”</span>` : ''}${x.response.response !== 'accept' ? `<br><button class="btn btn--ghost btn--sm" type="button" data-action="seen" data-rid="${x.response.id}">Mark as seen by staff</button>` : ''}` : x.overdue ? '<span class="warn-text">No answer yet (over 24 hours)</span>' : '<span class="muted">No answer yet</span>'}</td>
          <td>${x.link ? `${x.link.expired ? '<span class="warn-text">Link expired</span>' : 'Link made'} ${fmtDT(x.link.created_at)}${x.links_made > 1 ? ` <span class="muted">(${x.links_made} made)</span>` : ''}` : '<span class="muted">No link yet</span>'}
            ${x.link && shown[x.link.id] ? `<div class="mt"><input class="small" type="text" readonly value="${esc(shown[x.link.id])}" data-role="linkbox"> <button class="btn btn--ghost btn--sm" type="button" data-action="copy-link" data-link="${esc(shown[x.link.id])}">Copy</button><br><span class="small muted">Shown once. Paste it into your own message to ${esc(x.display_name.split(' ')[0])}; MateBot sends nothing.</span></div>` : ''}</td>
          <td>${st.applies && !st.needs_reintroduction ? `<button class="btn btn--ghost btn--sm" type="button" data-action="mint" data-uid="${esc(x.person_uid)}" title="${x.link ? 'The earlier link stops working; answers already given are kept' : ''}">${x.link ? 'Make a new link' : 'Make link'}</button>` : ''}</td>
        </tr>`).join('')}
        </tbody></table></div>
        <p class="msg small" data-role="rmsg" hidden></p>
        ${(st.history || []).length ? `<details class="more mt"><summary>Earlier memberships of this group (${st.history.length}) — their answers do not count for the current group</summary><ul class="small">${st.history.map(h => `<li>Version ${h.version}: ${h.people.map(esc).join(', ')} · ${h.accepted_count} of ${h.people_count} had said yes · changed ${fmtDT(h.superseded_at)}</li>`).join('')}</ul></details>` : ''}
      </div>`;
    };
    draw();
    const onClick = async ev => {
      const b = ev.target.closest('button'); if (!b) return;
      const rm = box.querySelector('[data-role="rmsg"]');
      const say = (t, ok) => { if (rm) { rm.hidden = false; rm.textContent = t; rm.className = `msg small ${ok ? 'ok' : 'err'}`; } };
      try {
        if (b.dataset.action === 'copy-link') { await navigator.clipboard.writeText(b.dataset.link); b.textContent = 'Copied'; return; }
        if (b.dataset.action === 'mint') {
          b.disabled = true;
          const out = await api.post(`/api/groups/${g.id}/responses/links`, { person_uid: b.dataset.uid });
          st = await api.get(`/api/groups/${g.id}/responses`);
          shown[out.link_id] = linkFor(out);
          draw();
          return;
        }
        if (b.dataset.action === 'seen') { b.disabled = true; await api.post(`/api/groups/${g.id}/responses/${b.dataset.rid}/reviewed`); st = await api.get(`/api/groups/${g.id}/responses`); draw(); return; }
        if (b.dataset.action === 'introduce-revised') {
          b.disabled = true;
          const ta = introBox && introBox();
          const bound = ta ? { draft_id: ta.dataset.draftId ? Number(ta.dataset.draftId) : null, context_hash: ta.dataset.contextHash || null } : {};
          st = await api.post(`/api/groups/${g.id}/responses/introduce`, bound);
          draw(); say('Recorded: the revised group has been introduced. Make a new link for each person.' + (st.travel_at_action ? ' ' + st.travel_at_action.plain : ''), !st.travel_at_action || st.travel_at_action.outcome === 'passed');
          return;
        }
        if (b.dataset.action === 'introduce-remaining') {
          b.disabled = true;
          st = await api.post(`/api/groups/${g.id}/responses/introduce-remaining`, { membership_hash: b.dataset.mh, disclosure_hash: b.dataset.dh });
          draw(); say('Recorded: the remaining group and the empty space were explained to everyone. Now make each person their own link.', true);
          return;
        }
      } catch (ex) {
        say(ex.message || 'That did not work. Reload the page and try again.', false);
        const again = box.querySelector(`[data-action="${b.dataset.action}"]`) || (remainingBox && remainingBox() && remainingBox().querySelector(`[data-action="${b.dataset.action}"]`));
        if (again) again.disabled = false;
        if (b.dataset.action === 'introduce-remaining') { try { st = await api.get(`/api/groups/${g.id}/responses`); draw(); } catch (_) { /* keep the message */ } }
      }
    };
    box.addEventListener('click', onClick);
    const rb0 = remainingBox && remainingBox();
    if (rb0) rb0.addEventListener('click', onClick);
  }

  async function groupDetail(p, id) {
    const g = await api.get(`/api/groups/${id}`);
    setDataset(g.is_test ? 'test' : 'real');
    const flash = state.cache.flash; setTimeout(() => { if (state.cache.flash === flash) { state.cache.flash = null; state.cache.flashHeld = false; state.cache.flashNeutral = false; } }, 0);
    const consentMissing = g.members.filter(m => !m.consent_share);
    p.innerHTML = `
      <div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-role="back">← Groups</button><h1>Group ${g.id}</h1>${g.apartment_id != null ? `<a class="small" href="#/staff/apartments/${g.apartment_id}">${esc(g.address)}</a>` : `<span class="small muted">No current offer</span>`}</div>
      ${state.cache.flash ? `<p class="msg ${state.cache.flashHeld || /^No replacement/.test(state.cache.flash) ? 'err' : state.cache.flashNeutral ? '' : 'ok'}">${esc(state.cache.flash)}</p>` : ''}
      ${groupCard(g, { offer: true })}
      <div data-role="mb-travel-panel"></div>
      <div data-role="exit-exceptions"></div>
      ${(g.alternatives || []).length ? `<details class="more mt"><summary>Alternative groupings for these people (${g.alternatives.length}) — view only (the AI, not staff, decides who is grouped)</summary>
        <ul class="small alt-list">${g.alternatives.map(a => `<li><a href="#/staff/groups/${a.id}">Group ${a.id}</a> ${a.is_primary ? '<span class="badge badge--ok">proposed</span>' : '<span class="badge badge--alt">alternative</span>'} · ${a.ai_rating != null ? `AI ${Number(a.ai_rating).toFixed(1)}` : a.rule_score == null ? 'rules score not recorded' : `rules ${Math.round(a.rule_score)}`} · ${a.members.map(esc).join(', ')} · ${esc(a.address)}</li>`).join('')}</ul></details>` : ''}
      <div class="detail detail--2 mt">
        <div class="card card--tight"><h3>Staff actions</h3>
          ${['Proposed', 'Introduced', 'Group confirmed'].includes(g.status) && g.is_primary !== false ? `<details class="more mb" data-role="recheck-confirm"><summary>Re-check apartment options</summary>
            <p class="small">Check this same group's apartments against its current requirements. Invalid options may be removed and the current apartment may change. A full AI review is required again before sharing; existing membership responses and the introduction clock are not reset. Nothing is sent.</p>
            <button class="btn btn--sm" type="button" data-action="recheck-options">Confirm option re-check</button>
            <p class="msg" role="status" aria-live="polite" data-role="recheck-msg"></p>
          </details>` : ''}
          <div class="actions">
            ${g.is_primary === false && g.status === 'Proposed' ? '<p class="small muted">This is a view-only alternative grouping: its members are already in their proposed group. It cannot be promoted by hand.</p>' : ''}
            ${g.status === 'Proposed' && g.is_primary !== false ? `<button class="btn btn--sm" type="button" data-status="Introduced" ${g.ready && g.intro && g.intro.current ? '' : 'disabled'} title="${esc(g.ready && g.intro && g.intro.current ? 'Records that the team has introduced this group (nothing is sent by MateBot)' : !g.ready ? 'Not ready to introduce: ' + ((g.circulation || {}).reasons || []).join('; ') : 'Draft the introduction with the AI first')}">Mark Introduced (after you have sent the introduction)</button>` : ''}
            ${g.status === 'Introduced' ? '<button class="btn btn--sm" type="button" data-status="Group confirmed">Mark Group confirmed</button>' : ''}
            ${g.status === 'Group confirmed' ? '<button class="btn btn--sm" type="button" data-status="Leased" title="The apartment becomes taken and every other group moves to its next best apartment">Mark Leased</button>' : ''}
            ${g.status === 'Awaiting replacement' ? '<button class="btn btn--sm" type="button" data-status="Group confirmed" title="The group leases the whole apartment now and fills the last space later">Mark Group confirmed (leasing with the empty space)</button>' : ''}
            ${g.status === 'Awaiting replacement' ? '<button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="dissolve-pool">Dissolve; members rejoin the next run</button>' : g.status !== 'Leased' && g.status !== 'Dissolved' ? '<button class="btn btn--ghost btn--sm btn--danger" type="button" data-status="Dissolved">Dissolve group</button>' : ''}
          </div>
          ${g.status === 'Awaiting replacement' ? `<div class="awaiting-panel mt">
            <h3>Awaiting a replacement</h3>
            <p class="small">${esc(g.awaiting_note || '')} Shares shown are the guide from before the ${g.awaiting_reason === 'bar' ? 'bar' : 'rejection'} and stay unchanged; the group is liable for the whole rent, so the <b>uncovered share is ${g.uncovered_weekly == null ? 'not known (the stored bed allocation does not let MateBot AI work it out; check the allocation before quoting a figure)' : money(g.uncovered_weekly) + '/wk'}</b> on top of the remaining ${g.people || g.members.length} ${(g.people || g.members.length) === 1 ? "person's" : "people's"} own shares. No one joins this group except through a replacement you accept below.</p>
            <ol class="small options-list">${(g.staff_options || []).map(o => `<li><b>${esc(o.label)}.</b> ${esc(o.detail)}</li>`).join('')}</ol>
            <div class="field"><label>Discount note (staff discretion, free text; MateBot AI never changes a price)<textarea data-role="discount" rows="2" placeholder="e.g. offered $20/wk off the whole rent for the first 8 weeks">${esc(g.discount_note || '')}</textarea></label></div>
            <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="save-discount">Save discount note</button></div>
            <div data-role="remaining"></div>
            <h3 class="mt">Replacement search</h3>
            ${g.replacement ? `<p class="msg ok"><b>${g.awaiting_reason === 'bar' ? 'A member was barred and the AI found and ranked a replacement' : 'A replacement that passes every rule is available'}</b> (found ${fmtDT(g.replacement_found_at)}). Nothing is applied until you accept it.</p>
              <ul class="small alt-list">${g.replacement.map((r, i) => `<li><b>${esc(r.added_names.join(' + '))}</b> <span class="muted">· rules ${Math.round(r.rule_score)}${r.ai_rating != null ? ` · AI ${Number(r.ai_rating).toFixed(1)}` : ''}</span>
                <button class="btn btn--sm" type="button" data-action="accept-replacement" data-ids="${r.added_enquiry_ids.join(',')}">Accept${i === 0 ? ' (best)' : ''}</button>
                <button class="btn btn--ghost btn--sm" type="button" data-action="decline-replacement" data-ids="${r.added_enquiry_ids.join(',')}">Decline</button></li>`).join('')}</ul>
              <p class="small muted">Accepting recomputes the refill now, lets the AI rank it and continues the group as a new Proposed group. Declining records a rejection, so these people are never offered to this group again.</p>`
            : `<p class="small muted">No rule-passing replacement yet${g.replacement_checked_at ? ` (last checked ${fmtDT(g.replacement_checked_at)})` : ''}. Every matching run keeps looking and flags a find here and on the Overview; it is never applied automatically.</p>
              <div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="accept-replacement" data-ids="">Check now and accept the best rule-passing replacement</button></div>`}
          </div>` : ''}
          <p class="small muted mt">Each member's enquiry status follows the group. Leased members are kept beyond the 12-month retention period. Marking Leased is the only thing that takes an apartment.</p>
          ${['Proposed', 'Introduced', 'Group confirmed', 'Awaiting replacement'].includes(g.status) && g.is_primary !== false ? `<h3 class="mt">Reject members; the AI replaces them</h3>
          <p class="small muted">Tick the suggested member(s) the group doesn't want. MateBot AI records the rejection (they are never proposed again with the members who stay; they can still be grouped with others), the AI ranks every rule-passing replacement and the best one becomes the new proposed group. If nothing passes, the group is kept together and marked Awaiting replacement. Staff don't choose or rearrange members.</p>
          <div class="checklist">${g.members.map(m => `<label><input type="checkbox" value="${m.enquiry_id}" data-linked="${(m.linked_with || []).length ? '1' : ''}"> ${esc(m.first_name)} ${esc(m.last_name)} ${(m.linked_with || []).length ? `<span class="badge badge--alt" title="Came through, or sent, a Bring a mate link">friends with ${esc(m.linked_with.join(' + '))}</span>` : ''}<span class="small muted">· ${esc(m.party === 'Alone' ? 'alone' : m.party)}</span></label>`).join('')}</div>
          ${g.members.some(m => (m.linked_with || []).length) ? `<fieldset class="reject-kind" data-role="reject-kind"><legend class="small">If a ticked member has friends in this group (a "Bring a mate" party), who is rejecting them?</legend>
            <label class="small"><input type="radio" name="reject_kind_${g.id}" value="housemates" checked> <span><b>The housemates don't want this person.</b> The whole friend party leaves together and is grouped again together.</span></label>
            <label class="small"><input type="radio" name="reject_kind_${g.id}" value="alm"> <span><b>ALM rejects this person.</b> Speak with the remaining friends first and explain why, then record what they chose:</span></label>
            <div class="reject-kind__sub" data-role="friends-choice" hidden><label class="small"><input type="radio" name="friends_choice_${g.id}" value="leave"> <span>The friends leave the group with them and are grouped again together</span></label>
            <label class="small"><input type="radio" name="friends_choice_${g.id}" value="stay"> <span>The friends stay in the group without them; the rejected person is matched on their own</span></label>
            <p class="small muted">Either way only the rejected person is recorded as rejected, never the friends. If ALM never wants this person grouped again, open their enquiry and bar them from matching; the friends are not barred with them.</p></div></fieldset>` : ''}
          <div class="actions"><button class="btn btn--sm btn--danger" type="button" data-action="reject">Reject ticked member(s) and let the AI replace them</button></div>` : ''}
          <p class="msg" data-role="msg"></p>
          <div data-role="options" class="groups mt"></div>
        </div>
        <div class="card card--tight"><h3>Introduction</h3>
          <p class="small muted">AI-drafted for the team to send themselves. Each person is introduced from their own answers only; contact details are included only where the person agreed to share them${consentMissing.length ? `; <b>${consentMissing.map(m => esc(m.first_name)).join(', ')}</b> did not, so ALM relays for them` : ''}. Nothing is sent automatically: copy the text or open your email program.</p>
          <div data-role="introduction-readiness">${readinessHtml(g)}</div>
          ${g.intro && g.intro.text && !g.intro.current ? `<p class="msg err small"><b>${esc(g.intro.label)}.</b> ${esc(sentence(g.intro.stale_reason))}Earlier text is kept below for reference and cannot be copied or sent until a fresh draft passes the checks.</p>` : ''}
          ${g.facts_note ? `<p class="msg small"><b>Apartment changed.</b> ${esc(g.facts_note)}</p>` : ''}
          <div class="actions"><button class="btn btn--sm" type="button" data-action="draft" ${g.ready ? '' : 'disabled'} title="${esc(g.ready ? '' : 'Not ready: ' + ((g.circulation || {}).reasons || []).join('; '))}">${g.intro && g.intro.text ? 'Draft again with AI' : 'Draft introduction with AI'}</button>
            <button class="btn btn--ghost btn--sm" type="button" data-action="copy" ${g.intro && g.intro.current ? '' : 'disabled'}>Copy text</button>
            <a class="btn btn--ghost btn--sm" data-role="mailto" href="#" ${g.intro && g.intro.current ? '' : 'hidden'} target="_blank" rel="noopener noreferrer">Open in email</a>
            <button class="btn btn--ghost btn--sm" type="button" data-action="save-intro" ${g.intro && g.intro.current ? '' : 'disabled'}>Save edits</button>
            <button class="btn btn--sm" type="button" data-action="reload-group" hidden>Reload this group</button></div>
          <p class="msg" data-role="imsg"></p>
          ${g.intro && g.intro.text ? `<p class="small"><b data-role="intro-label">${esc(g.intro.label)}</b>${g.intro.drafted_at ? ` · ${fmtDT(g.intro.drafted_at)}` : ''}${g.intro.basis && g.intro.basis !== 'AI' ? ` · ${esc(g.intro.basis)}` : ''}</p>` : ''}
          <textarea class="intro-box mt" data-role="intro" data-drafted-at="${esc(g.intro && g.intro.drafted_at || '')}" data-draft-id="${esc(g.intro && g.intro.draft_id != null ? String(g.intro.draft_id) : '')}" data-context-hash="${esc(g.intro && g.intro.context_hash || '')}" ${g.intro && g.intro.current ? '' : 'readonly'}>${esc((g.intro && g.intro.text) || '')}</textarea>
          <p class="small muted" data-role="recipients">Recipients (own email, where sharing was agreed): ${recipientsText(g)}</p>
          ${(g.intro_missing || []).length ? `<p class="small warn-text"><b>Not recorded (the introduction leaves these out rather than guessing):</b> ${g.intro_missing.map(esc).join('; ')}</p>` : ''}
          ${(g.intro_history || []).length ? `<details class="more mt"><summary>Earlier introduction drafts and changes (${g.intro_history.length})</summary><ul class="small">${g.intro_history.map(h => `<li><b>${esc(h.label)}</b> · ${fmtDT(h.at)}${h.by ? ` · ${esc(h.by)}` : ''}${h.reason ? ` · ${esc(h.reason)}` : ''}${h.text ? `<details><summary>Show text</summary><pre class="small">${esc(h.text)}</pre></details>` : ''}</li>`).join('')}</ul></details>` : ''}
        </div>
      </div>
      <div data-role="responses"></div>
      ${reviewBriefHtml(g)}
`;
    p.querySelector('[data-role="back"]').addEventListener('click', () => navigate('groups'));
    groupResponsesPanel(p.querySelector('[data-role="responses"]'), g, () => p.querySelector('[data-role="intro"]'), () => p.querySelector('[data-role="remaining"]'));
    const m = p.querySelector('[data-role="msg"]'), im = p.querySelector('[data-role="imsg"]'), ta = p.querySelector('[data-role="intro"]');
    // recipients: every real person's OWN email where sharing was agreed (from the draft response, else the consented leads)
    let recipients = g.intro_recipients || null;
    const recipientList = () => recipients ? recipients.map(r => r.email) : g.members.filter(x => x.consent_share).map(x => x.email);
    let travelNow = null;   // where the apartment currently offered stands on travel, from this page's live check (see applyTravel)
    const travelFailed = () => !!(travelNow && travelNow.state === 'failed');
    const mailto = () => {
      const a = p.querySelector('[data-role="mailto"]'); const current = !!(g.intro && g.intro.current) && !!ta.value.trim() && !travelFailed();
      a.href = current ? `mailto:${recipientList().join(',')}?subject=${encodeURIComponent('Your proposed housemates – Apartment Living Melbourne')}&body=${encodeURIComponent(ta.value)}` : '#';
      a.hidden = !current; p.querySelector('[data-action="copy"]').disabled = !current; p.querySelector('[data-action="save-intro"]').disabled = !current;
    };
    mailto();
    // r7: once the server has refused an action because the draft this page shows is no longer the current one (409 from the
    // introduction draft, edit or Mark Introduced), this page's copy of the draft is NOT current: copying, opening in email,
    // saving and marking introduced are switched off until the group is reloaded. Unsaved text stays visible (read-only) so
    // nothing typed is lost; nothing is replaced behind the staff member's back.
    const introStale = why => {
      if (g.intro) g.intro.current = false;
      ta.readOnly = true; mailto();
      const mi = p.querySelector('[data-status="Introduced"]'); if (mi) { mi.disabled = true; mi.title = 'Reload the page: the introduction draft shown here is no longer the current one'; }
      const rl = p.querySelector('[data-action="reload-group"]'); if (rl) rl.hidden = false;
      const lb = p.querySelector('[data-role="intro-label"]'); if (lb) lb.textContent = 'Earlier draft: reload required';
      msg(im, `${why} The draft shown here is no longer the current one: copying, emailing and saving are switched off until you reload this group. Your unsaved text remains in the text box.`, false);
    };
    // Travel for the apartment currently offered, as this page's live check last showed it (null: not checked on this page). A
    // known failure switches off drafting, copying, emailing and Mark Introduced, and the Offer button of any apartment that
    // failed; the server refuses the same actions with its own check, so this only saves the staff member a refused click.
    const applyTravel = t => {
      travelNow = t;
      const readiness = p.querySelector('[data-role="introduction-readiness"]');
      if (readiness && readiness.querySelector('[data-role="readiness"]')) readiness.innerHTML = readinessHtml(g, t);
      const failedIds = new Set(((t && t.failed) || []).map(Number));
      p.querySelectorAll('button[data-offer]').forEach(b => {
        if (!b.dataset.label) { b.dataset.label = b.textContent; b.dataset.tip = b.title || ''; }
        const failed = failedIds.has(+b.dataset.offer);
        b.disabled = failed; b.textContent = failed ? 'Failed the travel check' : b.dataset.label;
        b.title = failed ? 'This apartment failed the live travel check on this page, so it cannot be offered. Check travel times again if the group\'s details have changed.' : b.dataset.tip;
      });
      const blocked = travelFailed(), why = 'The apartment currently offered failed the travel check. Offer another apartment from the group\'s list first.';
      // switch a control off for a failed offer, and back on only if this is what switched it off and nothing else has since
      // (a stale draft, a booking change in progress, a request already on its way)
      const gate = (el, offAnyway) => {
        if (!el) return;
        if (blocked) {
          if (!el.dataset.travelOff) { el.dataset.travelOff = el.disabled ? 'kept' : 'set'; el.dataset.tip = el.title || ''; }
          el.disabled = true; el.title = why;
        } else if (el.dataset.travelOff) {
          if (el.dataset.travelOff === 'set' && !offAnyway()) el.disabled = false;
          if (el.title === why) el.title = el.dataset.tip;
          delete el.dataset.travelOff;
        }
      };
      gate(p.querySelector('[data-status="Introduced"]'), () => !(g.ready && g.intro && g.intro.current));
      gate(p.querySelector('[data-action="draft"]'), () => !g.ready);
      mailto();
    };
    groupTravelPanel(p.querySelector('[data-role="mb-travel-panel"]'), g, applyTravel);
    window.MMAI_STAFF_EXCEPTIONS.mount(p.querySelector('[data-role="exit-exceptions"]'), {
      bot: 'matebot', id, owner: g,
      canWrite: ['Proposed', 'Introduced', 'Group confirmed', 'Awaiting replacement'].includes(g.status) && g.is_primary !== false,
      onChanged: render, onBeforeWrite: blockSharing
    });
    function blockSharing() {
      if (g.intro) g.intro.current = false;
      g.ready = false;
      ta.readOnly = true; mailto();
      const draft = p.querySelector('[data-action="draft"]'); if (draft) draft.disabled = true;
      const mi = p.querySelector('[data-status="Introduced"]'); if (mi) mi.disabled = true;
      const reload = p.querySelector('[data-action="reload-group"]'); if (reload) reload.hidden = false;
      const label = p.querySelector('[data-role="intro-label"]'); if (label) label.textContent = 'Reload to verify the current introduction';
      const readiness = p.querySelector('[data-role="introduction-readiness"]');
      if (readiness) readiness.textContent = 'Sharing is paused until this booking change has been checked.';
      p.querySelectorAll('.badge--ready, [data-role="ready-badge"]').forEach(b => { b.className = 'badge badge--warn'; b.textContent = 'Reload to check readiness'; });
      msg(im, 'A booking decision or option re-check is in progress. Reload this group before copying, emailing or editing its introduction.', false);
    }
    p.addEventListener('click', async ev => {
      const b = ev.target.closest('button'); if (!b || b.closest('[data-role="exit-exceptions"]') || b.closest('[data-role="mb-travel-panel"]')) return;
      try {
        if (b.dataset.action === 'recheck-options') {
          b.disabled = true;
          blockSharing();
          const rm = p.querySelector('[data-role="recheck-msg"]');
          msg(rm, 'Re-checking this group’s apartment options…');
          try {
            const result = await api.post(`/api/groups/${id}/recheck-options`, {});
            state.cache.flash = result.message;
            state.cache.flashHeld = result.full_review_queued === false && result.apartment_id == null;
            render();
          } catch (ex) {
            msg(rm, ex.message + ((!ex.status || ex.status >= 500) ? ' Reload this group to check the outcome before trying again.' : ''), false);
            if (ex.status && ex.status < 500) b.disabled = false;
            else {
              const reload = document.createElement('button');
              reload.type = 'button'; reload.className = 'btn btn--ghost btn--sm'; reload.textContent = 'Reload this group';
              reload.addEventListener('click', render); rm.append(' ', reload);
            }
          }
          return;
        }
        if (b.dataset.action === 'reload-group') { render(); return; }
        if (b.dataset.offer) {
          // the server checks travel for this one apartment before it switches the offer: a known failure refuses it
          b.disabled = true; msg(m, 'Checking travel for this apartment before offering it…');
          try {
            const r = await api.patch(`/api/groups/${id}`, { apartment_id: +b.dataset.offer });
            if (r.travel_at_action) { state.cache.flash = `Offer changed. ${r.travel_at_action.plain}`; state.cache.flashNeutral = r.travel_at_action.outcome !== 'passed'; }
            render();
          } catch (ex) {
            msg(m, ex.message, false);
            if (ex.data && ex.data.travel_refused && ex.data.travel_at_action && ex.data.travel_at_action.outcome === 'failed') { b.textContent = 'Failed the travel check'; b.title = ex.message; }
            else b.disabled = false;
          }
          return;
        }
        if (b.dataset.action === 'rerank-ack') { await api.patch(`/api/groups/${id}`, { rerank_ack: true }); render(); return; }
        if (b.dataset.action === 'offer-ack') {
          b.disabled = true;
          const r = await api.patch(`/api/groups/${id}`, { offer_ack: true });
          if (r.travel_at_action) { state.cache.flash = `Acknowledged. ${r.travel_at_action.plain}`; state.cache.flashNeutral = r.travel_at_action.outcome !== 'passed'; }
          render(); return;
        }
        if (b.dataset.action === 'mate-decline') {  // GP11(l): staff speak with the inviter, then record their choice
          const box = p.querySelector(`[data-decline-choice="${b.dataset.rid}"]`); if (box) { box.hidden = !box.hidden; } return;
        }
        if (b.dataset.action === 'mate-approve' || b.dataset.action === 'mate-decline-go') {
          const approve = b.dataset.action === 'mate-approve';
          b.disabled = true;
          const r = await api.post(`/api/mate-requests/${b.dataset.rid}/decide`, approve ? { approve: true } : { approve: false, choice: b.dataset.choice });
          state.cache.flash = r.message; if (r.new_group_id) navigate('groups', r.new_group_id); else render();
          return;
        }
        if (b.dataset.action === 'ai-retry') { b.disabled = true; await api.post('/api/ai-queue/retry', { group_ids: [id] }); state.cache.flash = 'Re-queued for AI assessment. The group shows as pending until the AI has assessed it.'; render(); return; }
        if (b.dataset.status) {
          if (b.dataset.status === 'Dissolved' && !b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to dissolve'; return; }
          if (b.dataset.status === 'Leased' && !b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again: marks the apartment Leased and re-ranks every other group'; return; }
          // Mark Introduced names the draft row this page showed (stable id + its context): a newer draft or a new apartment underneath refuses it
          const bound = b.dataset.status === 'Introduced' && ta ? { draft_id: ta.dataset.draftId ? Number(ta.dataset.draftId) : null, context_hash: ta.dataset.contextHash || null } : {};
          let r;
          try { r = await api.patch(`/api/groups/${id}`, { status: b.dataset.status, ...bound }); }
          catch (ex) {
            if (b.dataset.status === 'Introduced' && ex.data && ex.data.travel_refused) { msg(im, ex.message, false); b.disabled = !!(ex.data.travel_at_action && ex.data.travel_at_action.outcome === 'failed'); return; }
            if (b.dataset.status === 'Introduced' && ex.status === 409) { introStale(ex.message); return; }
            throw ex;
          }
          if (r.travel_at_action) { state.cache.flash = `Marked introduced. ${r.travel_at_action.plain}`; state.cache.flashNeutral = r.travel_at_action.outcome !== 'passed'; }
          if (r.gp8) { state.cache.flash = r.gp8.message ? `${r.gp8.address}: ${r.gp8.message}` : `Leased: ${r.gp8.address} is now taken. ${r.gp8.affected.length} other group(s) moved to their next best apartment and flagged.`; }
          render();
        }
        if (b.dataset.action === 'dissolve-pool') {
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again: dissolve; every member rejoins the next matching run'; return; }
          const r = await api.post(`/api/groups/${id}/dissolve`, { note: p.querySelector('[data-role="discount"]')?.value ? 'Discount note: ' + p.querySelector('[data-role="discount"]').value : '' });
          state.cache.flash = r.message; render(); return;
        }
        if (b.dataset.action === 'save-discount') { await api.patch(`/api/groups/${id}`, { discount_note: p.querySelector('[data-role="discount"]').value }); msg(m, 'Discount note saved (free text only; prices unchanged).'); return; }
        if (b.dataset.action === 'accept-replacement') {
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to accept: a new Proposed group continues this one'; return; }
          b.disabled = true; msg(m, 'Recomputing the refill, asking the AI to rank it…');
          const ids = b.dataset.ids ? b.dataset.ids.split(',').map(Number) : null;
          const r = await api.post(`/api/groups/${id}/fill-replacement`, ids ? { added_enquiry_ids: ids } : {});
          state.cache.flash = `${r.message} Ranking: ${r.ranking_basis}.`; navigate('groups', r.new_group_id); return;
        }
        if (b.dataset.action === 'decline-replacement') {
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to decline'; return; }
          const r = await api.post(`/api/groups/${id}/decline-replacement`, { added_enquiry_ids: b.dataset.ids.split(',').map(Number) });
          state.cache.flash = r.message; render(); return;
        }
        if (b.dataset.action === 'reject') {
          const ids = [...p.querySelectorAll('.checklist input:checked')].map(i => +i.value);
          if (!ids.length) return msg(m, 'Tick at least one member to reject.', false);
          if (ids.length >= g.members.length) return msg(m, 'At least one member must stay. Dissolve the group instead.', false);
          const linkedTicked = [...p.querySelectorAll('.checklist input:checked')].some(i => i.dataset.linked);
          const kind = p.querySelector(`input[name="reject_kind_${id}"]:checked`)?.value || 'housemates';
          const friendsChoice = p.querySelector(`input[name="friends_choice_${id}"]:checked`)?.value || null;
          if (linkedTicked && kind === 'alm' && !friendsChoice) return msg(m, 'Speak with the friends first, then tick whether they leave with this person or stay without them.', false);
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = linkedTicked && kind === 'housemates' ? `Click again: the whole friend party leaves together and the AI refills the group` : linkedTicked && friendsChoice === 'leave' ? `Click again: the friends leave together with them; the AI refills the group` : linkedTicked && friendsChoice === 'stay' ? `Click again: only this person leaves; the friends stay` : `Click again to reject ${ids.length} member(s): the AI will refill the group`; return; }
          b.disabled = true; msg(m, 'Recording the rejection, searching for replacements that pass every rule, then asking the AI to rank them…');
          const r = await api.post(`/api/groups/${id}/reject`, { reject_enquiry_ids: ids, reason_kind: kind, friends_choice: linkedTicked && kind === 'alm' ? friendsChoice : null });
          if (r.new_group_id) { state.cache.flash = `${r.message} The AI chose the top-rated replacement (${r.ranking_basis}); ${Math.max(0, r.options.length - 1)} other rule-passing option(s) were considered.`; navigate('groups', r.new_group_id); }
          else { state.cache.flash = r.message; render(); }
        }
        if (b.dataset.action === 'draft') {
          b.disabled = true; msg(im, 'Drafting…');
          try {
            const r = await api.post(`/api/groups/${id}/introduction`, {});
            state.cache.flash = r.current ? `Introduction drafted by the AI for ${r.recipients.length} recipient(s).` + (r.without_email.length ? ` No email on record for: ${r.without_email.join(', ')}.` : '')
              : `${r.basis}. This template is not an AI draft: it cannot be copied or sent; draft again when the AI is available.`;
            if (r.travel_at_action) { state.cache.flash += ` ${r.travel_at_action.plain}`; state.cache.flashNeutral = r.travel_at_action.outcome !== 'passed'; }
          } catch (ex) {
            if (ex.data && ex.data.travel_refused) { msg(im, ex.message, false); b.disabled = !!(ex.data.travel_at_action && ex.data.travel_at_action.outcome === 'failed'); return; }
            if (ex.status === 409) introStale(ex.message); else msg(im, ex.message, false); b.disabled = false; return;
          }
          render(); return;   // re-read the group so the current/historical labels, recipients and buttons come from the server
        }
        if (b.dataset.action === 'copy') { if (!(g.intro && g.intro.current)) return msg(im, 'Only a current draft can be copied. Draft it again first.', false); await navigator.clipboard.writeText(ta.value); msg(im, 'Copied.'); }
        if (b.dataset.action === 'save-intro') {
          if (!(g.intro && g.intro.current)) return msg(im, 'Only the current draft can be edited. Draft it again first.', false);
          // the edit names the draft row it was made on (stable id + the context that draft was written for); the timestamp is display only
          try { await api.patch(`/api/groups/${id}`, { intro_text: ta.value, draft_id: ta.dataset.draftId ? Number(ta.dataset.draftId) : null, context_hash: ta.dataset.contextHash || null, intro_drafted_at: ta.dataset.draftedAt || null }); msg(im, 'Saved as a staff-edited draft.'); setTimeout(render, 500); }
          catch (ex) { if (ex.status === 409) introStale(ex.message); else msg(im, ex.message, false); }
        }
      } catch (ex) { msg(m, ex.message, false); b.disabled = false; }
    });
    ta.addEventListener('input', mailto);
    p.addEventListener('change', ev => {  // GP11(k): the friends' choice is only asked when ALM is the one rejecting
      if (ev.target.name === `reject_kind_${id}`) { const sub = p.querySelector('[data-role="friends-choice"]'); if (sub) sub.hidden = ev.target.value !== 'alm'; }
    });
  }

  // ---- New phone enquiry ----
  // ---------------- Outcomes by person (read-only summary of the outcome ledger) ----------------
  // Plain-language answer to "where does each person stand, why, and what happens next". Read from /api/outcome-ledger;
  // changes nothing. A ready proposal is never a placement; figures are a snapshot, not a live view.
  views.outcomes = async p => {
    stopPoll();
    const test = state.dataset === 'test';
    const led = await api.get('/api/outcome-ledger?' + new URLSearchParams({ dataset: state.dataset }));
    const states = led.states || {}, cov = led.coverage || {}, tot = led.totals || {}, cons = led.consistency || {};
    const order = Object.keys(states);
    const stateRows = order.map(k => `<tr><td>${esc(states[k].label || k)}</td><td class="num">${esc(String(states[k].residents))}</td><td class="num">${esc(String(states[k].forms))}</td></tr>`).join('');
    const reasons = (led.unresolved_reasons || []).map(r => `<tr><td>${esc(r.text)}</td><td class="num">${esc(String(r.residents))}</td><td class="num">${esc(String(r.forms))}</td></tr>`).join('');
    const hist = (led.history && led.history.residents) || {};
    const histLine = ['introduced', 'responded', 'accepted', 'leased'].map(k => `${esc(String(hist[k] || 0))} ${k}`).join(' · ');
    const f = state.cache.outFilter || {};
    const people = (led.people || []).filter(x => (!f.state || x.state === f.state) && (!f.q || `${x.name || ''} ${x.resident_id || ''} ${x.form_id || ''}`.toLowerCase().includes(f.q.toLowerCase())));
    const personRows = people.map(x => `<tr>
        <td>${esc(x.name || 'Name not recorded')}<div class="small muted">${esc(x.role || '')}${x.form_id ? ` · <a href="#/staff/enquiries/${encodeURIComponent(x.form_id)}">enquiry ${esc(String(x.form_id))}</a>` : ''}${x.group_id ? ` · <a href="#/staff/groups/${encodeURIComponent(x.group_id)}">group #${esc(String(x.group_id))}</a>` : ''}</div></td>
        <td><span class="badge ${x.counts_as_ready_proposal ? 'badge--ok' : ''}">${esc(x.state_label || x.state || '')}</span></td>
        <td>${esc(x.outcome || '')}${(x.also || []).length ? `<details class="more"><summary>More</summary><ul class="small">${x.also.map(t => `<li>${esc(t)}</li>`).join('')}</ul></details>` : ''}</td>
        <td>${esc(x.next_action || '')}</td>
        <td class="small">${['introduced', 'responded', 'accepted', 'leased'].filter(k => x.history && x.history[k]).map(k => esc(k)).join(', ') || '—'}</td>
      </tr>`).join('');
    p.innerHTML = `<div class="outcomes">
      <div class="pane-head"><h1>Outcomes by person</h1><span class="badge ${test ? 'badge--test' : ''}">${test ? 'Test data' : 'Real enquiries'}</span><span class="spacer"></span>
        <button class="btn btn--ghost btn--sm" type="button" data-go="groups">Back to groups</button>
        <button class="btn btn--sm" type="button" data-action="refresh">Refresh</button></div>
      <p class="msg">Read-only snapshot taken ${cons.read_started_at ? esc(fmtDT(cons.read_started_at)) : 'just now'} for business date ${esc(led.business_date || '')}. A <b>ready proposal</b> means the person is in the one proposed group that staff may introduce; it is not an apartment placement and nothing is reserved by it. ${cons.checked ? (cons.single_instant === true ? 'All figures were read at one moment.' : 'Figures may have changed while they were being read; refresh to re-check.') : 'Whether all figures were read at one moment was not checked on this database.'}</p>
      <div class="card card--tight">
        <h3>Where people stand</h3>
        <p class="small">${esc(String(tot.residents || 0))} people on ${esc(String(tot.enquiry_forms || 0))} enquiry forms${tot.linked_parties ? ` (${esc(String(tot.linked_parties))} linked parties)` : ''}. Ready proposals: <b>${esc(String(cov.ready_proposal_residents || 0))}</b> people on ${esc(String(cov.ready_proposal_forms || 0))} forms.</p>
        <table class="tbl"><thead><tr><th>Current standing</th><th class="num">People</th><th class="num">Forms</th></tr></thead><tbody>${stateRows || '<tr><td colspan="3">No enquiries in this data set.</td></tr>'}</tbody></table>
        <p class="small muted mt">Already happened (kept separate, never added to current proposals): ${histLine}.</p>
      </div>
      <div class="card card--tight mt">
        <h3>Why people are not in a ready proposal</h3>
        <table class="tbl"><thead><tr><th>Reason</th><th class="num">People</th><th class="num">Forms</th></tr></thead><tbody>${reasons || '<tr><td colspan="3">Nothing unresolved.</td></tr>'}</tbody></table>
      </div>
      <div class="card card--tight mt">
        <h3>Each person</h3>
        <form class="toolbar outcomes-filters" data-role="filters">
          <input type="search" name="q" placeholder="Search name or enquiry number" value="${esc(f.q || '')}">
          <select name="state"><option value="">Any standing</option>${order.map(k => `<option value="${esc(k)}" ${f.state === k ? 'selected' : ''}>${esc(states[k].label || k)}</option>`).join('')}</select>
          <button class="btn btn--sm" type="submit">Filter</button>
        </form>
        <p class="small muted">${esc(String(people.length))} of ${esc(String((led.people || []).length))} people shown.</p>
        <div class="table-wrap"><table class="tbl"><thead><tr><th>Person</th><th>Standing</th><th>What is happening and why</th><th>Next step</th><th>History</th></tr></thead><tbody>${personRows || '<tr><td colspan="5">No one matches this filter.</td></tr>'}</tbody></table></div>
      </div>
      <details class="more mt"><summary>What this summary can and cannot tell you</summary><ul class="small">${(led.limitations || []).map(t => `<li>${esc(t)}</li>`).join('')}</ul></details></div>`;
    p.querySelectorAll('[data-go]').forEach(b => b.addEventListener('click', () => navigate(b.dataset.go)));
    p.querySelector('[data-action="refresh"]').addEventListener('click', () => render());
    p.querySelector('[data-role="filters"]').addEventListener('submit', e => { e.preventDefault(); const fd = new FormData(e.target); state.cache.outFilter = { q: fd.get('q') || '', state: fd.get('state') || '' }; render(); });
  };

  views.new = async p => {
    p.innerHTML = `<div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-go="enquiries">← Enquiries</button><h1>New phone enquiry</h1><p>Same questions as the website form; recorded as a phone enquiry with your name.</p></div><div data-role="formhost"></div>`;
    p.querySelector('[data-go]').addEventListener('click', () => navigate('enquiries'));
    window.mountForm(p.querySelector('[data-role="formhost"]'), {
      mode: 'staff', meta: state.meta, staffList: state.ref.staff_list, staffName: state.user.display_name,
      storageKey: 'staff:new:' + state.user.username,
      onSubmit: (answers, o) => api.post('/api/enquiries', { ...answers, channel: 'Phone' }, { headers: { 'X-Submission-Key': o.key } }),
      // I19(p): the email copy is built from the SAVED record (authenticated read; answers_json), never from the screen
      onLoadSaved: id => api.get(`/api/enquiries/${id}`),
    });
  };

  // ---- Emailed form links ----
  views.email = async p => {
    const links = await api.get('/api/email-links');
    const base = publicBase();
    p.innerHTML = `<div class="pane-head"><button class="btn btn--ghost btn--sm" type="button" data-go="enquiries">← Enquiries</button><h1>Send form by email</h1><p>Create a personal link to the enquiry form for someone. Their answers are recorded as an emailed form. Nothing is sent automatically: copy the link or open your email client.</p></div>
      <div class="card card--tight"><div class="editgrid"><div class="field"><label>Recipient name<input name="rn"></label></div><div class="field"><label>Recipient email<input name="re" type="email"></label></div></div>
        <div class="actions"><button class="btn btn--sm" type="button" data-action="create">Create link</button></div><p class="msg" data-role="msg"></p>
        <div data-role="result" hidden class="mt"><input readonly data-role="url"><div class="actions"><button class="btn btn--ghost btn--sm" type="button" data-action="copy">Copy link</button><a class="btn btn--ghost btn--sm" data-role="mail" target="_blank" rel="noopener noreferrer">Compose email</a></div></div></div>
      <div class="table-wrap mt"><table class="tbl"><thead><tr><th>Created</th><th>Recipient</th><th>By</th><th>Link</th><th>Used</th></tr></thead><tbody>
        ${links.map(l => `<tr><td class="mono small">${fmtDT(l.created_at)}</td><td>${esc(l.recipient_name || '')} <span class="small muted">${esc(l.recipient_email || '')}</span></td><td class="small">${esc(l.staff_member)}</td><td class="small mono">…#/enquire?t=${esc(l.token)}</td><td>${l.used_at ? '<span class="badge badge--ok">submitted</span>' : '<span class="badge">not yet</span>'}</td></tr>`).join('') || '<tr><td colspan="5" class="muted">No links yet.</td></tr>'}</tbody></table></div>`;
    const m = p.querySelector('[data-role="msg"]');
    p.querySelector('[data-go="enquiries"]').addEventListener('click', () => navigate('enquiries'));
    p.querySelector('[data-action="create"]').addEventListener('click', async () => {
      const rn = p.querySelector('[name="rn"]').value, re = p.querySelector('[name="re"]').value;
      const r = await api.post('/api/email-links', { recipient_name: rn, recipient_email: re });
      const url = `${base}#/enquire?t=${r.token}${state.dataset === 'test' ? '&test=1' : ''}`;
      p.querySelector('[data-role="result"]').hidden = false; p.querySelector('[data-role="url"]').value = url;
      p.querySelector('[data-role="mail"]').href = `mailto:${encodeURIComponent(re)}?subject=${encodeURIComponent('Find your housemates with Apartment Living Melbourne')}&body=${encodeURIComponent(`Hi ${rn || ''},\n\nPlease tell us about yourself so MateBot AI can find you highly compatible housemates:\n${url}\n\nIt takes a couple of minutes.\n\nApartment Living Melbourne`)}`;
      msg(m, 'Link created.');
    });
    p.querySelector('[data-action="copy"]').addEventListener('click', async () => { await navigator.clipboard.writeText(p.querySelector('[data-role="url"]').value); msg(m, 'Copied.'); });
  };

  // ---- Settings ----
  views.data = async p => {
    const isManager = state.user.role === 'owner';
    const [ret, audit, s, users] = await Promise.all([api.get('/api/retention/preview'), api.get('/api/audit?limit=60'), api.get('/api/stats'), isManager ? api.get('/api/auth/users').catch(() => []) : Promise.resolve([])]);
    const qt = state.ref.question_types;
    p.innerHTML = `<div class="pane-head"><h1>Settings</h1><p>Test data, loading an apartment list, data retention and the audit log.</p></div>
      <div class="detail detail--2">
        <div>
          <div class="card card--tight"><h3>Synthetic TEST data</h3>
            <p class="small muted">${s.enquiries_test} TEST enquiries in the data bank. They carry a TEST flag, @test.mmai.invalid emails and a “TEST-nnn” surname, and never appear with real data.</p>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="seed">Seed ~180 TEST enquiries</button><button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="wipe">Delete all TEST data</button></div><p class="msg" data-role="tmsg"></p></div>
          <div class="card card--tight mt card--danger"><h3>Load an apartment list</h3>
            <p class="small"><b>Use with care.</b> Loading a list updates every apartment that is on it (type, rents, bedrooms, bills and date available) from the spreadsheet, adds any new addresses and leaves other apartments as they are. Edits staff have made to those details are overwritten; lease status, minimum lease, groups and a date staff entered where the list still has none are kept. The agreed list corrections are re-applied automatically after every load. There is no undo.</p>
            <div class="editgrid">
              <div class="field"><label>Which list<select data-role="importSource"><option value="default">The 23 October 2026 list</option><option value="upload">A new .xlsx file</option></select></label></div>
              <div class="field" data-role="uploadField" hidden><label>Spreadsheet<input type="file" accept=".xlsx" data-role="upload"></label></div>
              <div class="field"><label>Type IMPORT to confirm<input data-role="importConfirm" autocomplete="off" placeholder="IMPORT"></label></div>
              <div class="field"><label class="small"><input type="checkbox" data-role="featuresOverwrite"> Let the sheet's feature Yes/No values replace ones staff recorded here</label><span class="small muted">Normally a differing sheet value is reported and the staff value kept. Blank cells never change anything; other text is reported as an error for that cell.</span></div>
            </div>
            <div class="actions"><button class="btn btn--sm btn--danger" type="button" data-action="import" disabled>Load the list</button></div>
            <p class="msg" data-role="imsg"></p></div>
          <div class="card card--tight mt"><h3>Data retention</h3>
            <p class="small muted">People who never lease are deleted 12 months after their last contact. Cut-off today: ${fmtD(ret.cutoff)}. A scheduled purge runs daily (next about ${fmtDT(ret.next_scheduled_run)}); you can also run it now. “Log contact” on an enquiry resets its clock.</p>
            <p class="small">${ret.due.length} enquiry(ies) due for deletion${ret.due.length ? ': ' + ret.due.map(d => `${esc(d.first_name)} ${esc(d.last_name)}${d.is_test ? ' (TEST)' : ''}`).join(', ') : ''}.</p>
            <div class="actions"><button class="btn btn--ghost btn--sm btn--danger" type="button" data-action="purge" ${ret.due.length ? '' : 'disabled'}>Run purge now</button></div><p class="msg" data-role="rmsg"></p></div>
          <div class="card card--tight mt"><h3>Question types</h3>
            <p class="small muted">Whether each form question is a must-match, a preference or information only is set in the software. Information-only answers are not used for matching. Staff cannot change these classifications here.</p>
            <dl class="kv small">${Object.entries(qt).map(([q, t]) => `<dt>${q}</dt><dd>${t}</dd>`).join('')}</dl></div>
        </div>
        <div>
          <div class="card card--tight"><h3>Your sign-in</h3><p class="small muted">You are signed in as <b>${esc(state.user.display_name)}</b> (username <span class="mono">${esc(state.user.username || '')}</span>). Everything you do is recorded under your name.</p>
            <div class="actions"><button class="btn btn--sm" type="button" data-action="pw">Change my password</button></div></div>
          ${isManager ? `<div class="card card--tight mt"><h3>Staff accounts</h3><p class="small muted">One sign-in per person. If someone forgets their password, reset it here and pass the new temporary password on in person; they can then change it under “Change my password”.</p>
            <div class="table-wrap"><table class="tbl"><thead><tr><th>Name</th><th>Username</th><th>Role</th><th></th></tr></thead><tbody>${users.map(u => `<tr><td>${esc(u.display_name)}</td><td class="mono small">${esc(u.username)}</td><td class="small">${u.disabled ? '<span class="badge">sign-in closed</span>' : esc(ROLE_LABEL[u.role] || u.role)}</td><td>${u.disabled || u.username === state.user.username ? '' : `<button class="btn btn--ghost btn--sm" type="button" data-action="reset" data-user="${esc(u.username)}">Reset password</button>`}</td></tr>`).join('')}</tbody></table></div>
            <p class="msg" data-role="umsg"></p></div>` : ''}
          <div class="card card--tight mt"><h3>AI</h3><p class="small">${s.ai_configured ? `Model <b>${esc(state.ref.ai_model)}</b> is configured to assess rule-passing groups and prepare staff summaries and introduction drafts. Configuration alone does not confirm a successful AI connection or a ready group.` : 'The AI is not connected on this server. Matching runs still form groups by the rules, but every new group waits for a valid AI review and its staff summaries before it is ready for reservations. Introductions are drafted only for groups that have passed that check.'}</p></div>
          <div class="card card--tight mt"><h3>Institutions</h3><p class="small muted">${state.ref.institutions.length - 1} campuses with addresses held in the background for the final travel check; “Other” is last. Unverified addresses are marked.</p>
            <details class="more"><summary>Show list</summary><ul class="small">${state.ref.institutions.filter(i => i.name !== 'Other').map(i => `<li>${esc(i.name)} · ${esc(i.address)} ${i.verified ? '' : '<span class="badge badge--warn">unverified</span>'} ${i.source_url ? `<a href="${esc(i.source_url)}" target="_blank" rel="noopener noreferrer">source</a>` : ''}</li>`).join('')}</ul></details></div>
          <div class="card card--tight mt"><h3>Audit log</h3><p class="small muted">A technical record of every change, kept for accountability. Older entries may use internal wording.</p><details class="more"><summary>Show the last 60 entries</summary><div class="table-wrap mt"><table class="tbl"><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead><tbody>${audit.map(a => `<tr><td class="mono small">${fmtDT(a.at)}</td><td class="small">${esc(a.staff_member || 'system')}</td><td class="small">${esc(a.action)} ${a.entity ? `<span class="muted">${esc(a.entity)} ${esc(a.entity_id || '')}</span>` : ''}</td><td class="small wrap-cell">${esc(a.detail || '')}</td></tr>`).join('')}</tbody></table></div></details></div>
        </div>
      </div>`;
    const tm = p.querySelector('[data-role="tmsg"]'), rm = p.querySelector('[data-role="rmsg"]'), im = p.querySelector('[data-role="imsg"]');
    const impBtn = p.querySelector('[data-action="import"]'), impConfirm = p.querySelector('[data-role="importConfirm"]'), impSource = p.querySelector('[data-role="importSource"]'), impFile = p.querySelector('[data-role="upload"]');
    const impReady = () => { impBtn.disabled = !(impConfirm.value.trim().toUpperCase() === 'IMPORT' && (impSource.value === 'default' || impFile.files.length)); };
    impConfirm.addEventListener('input', impReady); impFile.addEventListener('change', impReady);
    impSource.addEventListener('change', () => { p.querySelector('[data-role="uploadField"]').hidden = impSource.value !== 'upload'; impReady(); });
    p.addEventListener('click', async ev => {
      const b = ev.target.closest('[data-action]'); if (!b) return;
      try {
        if (b.dataset.action === 'import') {
          b.disabled = true; msg(im, 'Loading the list…');
          try {
            const body = impSource.value === 'upload' ? new Uint8Array(await impFile.files[0].arrayBuffer()) : undefined;
            const fo = p.querySelector('[data-role="featuresOverwrite"]');
            const rep = await api.post('/api/apartments/import?confirm=' + encodeURIComponent(impConfirm.value.trim()) + (fo && fo.checked ? '&features_overwrite=true' : ''), body);
            msg(im, `Done: ${rep.imported} rows read; ${rep.added} apartment(s) added, ${rep.updated} updated, ${rep.unchanged} unchanged${rep.kept_not_in_list.length ? `, ${rep.kept_not_in_list.length} not on the list kept as they were` : ''}${rep.dates_kept.length ? `; staff-entered date kept for ${rep.dates_kept.map(d => d.address).join(', ')}` : ''}. ${rep.corrections.length ? `List corrections re-applied: ${rep.corrections.join('; ')}.` : 'List corrections checked: already in place.'}${rep.errors.length ? ` ${rep.errors.length} row(s) could not be read.` : ''}${rep.feature_rows ? ` Features: ${rep.feature_rows} row(s) carried feature columns${rep.feature_errors.length ? `; ${rep.feature_errors.length} cell(s) not understood and left unknown: ${rep.feature_errors.slice(0, 8).map(x => `${x.address} · ${x.feature} “${x.text}”`).join('; ')}${rep.feature_errors.length > 8 ? '…' : ''}` : ''}${rep.feature_conflicts.length ? `; ${rep.feature_conflicts.length} differing value(s) kept as staff recorded: ${rep.feature_conflicts.slice(0, 8).map(x => `${x.address} · ${x.feature} (here ${x.recorded}, sheet ${x.sheet})`).join('; ')}${rep.feature_conflicts.length > 8 ? '…' : ''}` : ''}${rep.feature_overwritten.length ? `; ${rep.feature_overwritten.length} staff value(s) replaced from the sheet` : ''}.` : ''}`);
            impConfirm.value = ''; impReady();
          } catch (ex) { msg(im, ex.message, false); impReady(); }
          return;
        }
        if (b.dataset.action === 'seed') { b.disabled = true; msg(tm, 'Seeding…'); const r = await api.post('/api/test-data/seed'); msg(tm, `${r.created} TEST enquiries created${r.failed.length ? `, ${r.failed.length} skipped by validation` : ''}.`); setTimeout(render, 700); }
        if (b.dataset.action === 'wipe') { if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to delete all TEST data'; return; } const r = await api.del('/api/test-data'); msg(tm, `${r.deleted} TEST enquiries and their groups deleted.`); setTimeout(render, 700); }
        if (b.dataset.action === 'purge') { const r = await api.post('/api/retention/purge'); msg(rm, `${r.deleted} enquiry(ies) deleted (cut-off ${fmtD(r.cutoff)}).`); setTimeout(render, 700); }
        if (b.dataset.action === 'pw') navigate('password');
        if (b.dataset.action === 'reset') {
          const um = p.querySelector('[data-role="umsg"]');
          if (!b.dataset.confirm) { b.dataset.confirm = '1'; b.textContent = 'Click again to confirm'; return; }
          try { const r = await api.post(`/api/auth/users/${encodeURIComponent(b.dataset.user)}/reset-password`, {}); msg(um, `New temporary password for ${r.display_name}: ${r.temporary_password} — write it down now; it is shown only once. Their other devices have been signed out.`); b.textContent = 'Reset password'; delete b.dataset.confirm; }
          catch (ex) { msg(um, ex.message, false); }
        }
      } catch (ex) { msg(tm, ex.message, false); }
    });
  };

  // ---- Change my password ----
  views.password = async p => {
    p.innerHTML = `<div class="pane-head"><h1>Change my password</h1><p>For ${esc(state.user.display_name)}. Choose at least 10 characters; a short phrase of a few words is easy to remember and hard to guess.</p></div>
      <div class="card login">
        <form data-role="pwform" autocomplete="off">
          <div class="field"><label>Current password<input name="current" type="password" autocomplete="current-password" required></label></div>
          <div class="field"><label>New password (at least 10 characters)<input name="next" type="password" autocomplete="new-password" minlength="10" required></label></div>
          <div class="field"><label>Type the new password again<input name="again" type="password" autocomplete="new-password" minlength="10" required></label></div>
          <div class="actions"><button class="btn" type="submit">Change password</button><button class="btn btn--ghost" type="button" data-action="back">Back to Settings</button></div>
          <p class="msg" data-role="pwmsg"></p>
        </form>
      </div>`;
    const f = p.querySelector('[data-role="pwform"]'), m = p.querySelector('[data-role="pwmsg"]');
    p.querySelector('[data-action="back"]').addEventListener('click', () => navigate('data'));
    f.addEventListener('submit', async e => {
      e.preventDefault();
      if (f.next.value !== f.again.value) return msg(m, 'The two new passwords do not match.', false);
      if (f.next.value.length < 10) return msg(m, 'The new password must be at least 10 characters long.', false);
      try { const r = await api.post('/api/auth/password', { current_password: f.current.value, new_password: f.next.value }); f.reset(); msg(m, r.message + ' This device stays signed in; any other device will need the new password.'); }
      catch (ex) { msg(m, ex.message, false); }
    });
  };

  // ---------------- Public API ----------------
  window.MMAI_STAFF = {
    async mount(host, meta, view, param) {
      state.host = host; state.meta = meta; state.view = view || 'overview'; state.param = param || null;
      if (!state.user && !state.resumeTried) {  // T7: remembered sign-in on this device
        state.resumeTried = true;
        host.innerHTML = '<p class="loading">Checking your sign-in…</p>';
        try { const r = await api.resumeSession(); if (r) { state.user = r; state.resumed = r.method; } } catch { /* offline: show the login form */ }
      }
      if (state.user) document.getElementById('topbarRight').innerHTML = `<span>${esc(state.user.display_name)}</span><a href="#/">Public form</a>`;
      if (state.user && !state.ref) state.ref = await api.get('/api/reference');
      render();
    },
    isLoggedIn: () => !!state.user,
  };
})();
