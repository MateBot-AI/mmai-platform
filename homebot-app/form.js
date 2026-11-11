/* HomeBot questionnaire engine (H2–H6; change request 4: saved to the shared MMAI backend for manual staff review).
   Network: GET /api/public/homebot/meta (price guide + providers), POST /api/public/homebot/enquiries with an X-Submission-Key
   (same key for every retry of one form session, so a retry can never make a second enquiry). No storage. State lives in the controls.
   Conventions borrowed from the MateBot client so the two forms behave the same:
     .step[data-title]          a section (card) · the one with [data-review] is the summary
     .scr[data-scr]             one screen (question) at a time · data-tip = HomeBot's comment
     data-show-when="n=v|v2"    screen/reveal shown only while the named control has one of the values
     data-hide-when="n=v"       screen hidden while the control has the value
     data-terminal              a routing screen: only Back (and its own buttons) — the form does not continue past it
     fieldset[data-required-group]  at least one radio/checkbox ticked · [data-all-required] every box ticked
   Summary: one group per section, Change jumps to that section's first screen; the green "Back to check your answers"
   button then returns directly to the summary (I19(m)/H5). Submit saves the enquiry and shows the reference; the completion screen
   uses neutral H30 completion copy and an explicit receipt-status notice, never claiming a completed match or inbox delivery. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MAX_PEOPLE = 7;
  const TIP_IMG = 'img/homebot_tip.webp';
  let DATA = { types: [], institutions: [], extra_bed_charge_per_week: 70 };
  /* API base: <meta name="mmai-api-base"> is rewritten at deploy time. An unresolved "__PORT_…__" placeholder means a copy that
     was never pointed at a server: same origin is assumed on localhost only (self-host / local testing); anywhere else the form
     shows a visible "not connected" notice and cannot submit. "" = same origin (the backend serves this folder at /homebot/). */
  function apiBase() {
    const meta = document.querySelector('meta[name="mmai-api-base"]');
    const raw = ((meta && meta.getAttribute('content')) || '').trim();
    const local = ['localhost', '127.0.0.1', '[::1]', ''].includes(location.hostname);
    if (/^__PORT_\d+__$/.test(raw)) return local ? { base: '', ok: true } : { base: '', ok: false };
    return { base: raw.replace(/\/+$/, ''), ok: true };
  }
  const API = apiBase();
  const SLEEP_CHOICES = ['Own bedroom', 'Shares a bed with partner', 'Twin room (two single beds)', 'Twin room or bunk', 'Bunk bed'];
  const BUNK_CHOICES = ['Twin room or bunk', 'Bunk bed'];
  const newKey = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '') : Array.from({ length: 32 }, () => Math.floor(Math.random() * 16).toString(16)).join(''));
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const money = n => '$' + Number(n).toLocaleString('en-AU');
  const typeLabel = t => /bathroom/.test(t.label) ? t.label : `${t.label}, ${t.bathrooms} bathroom${t.bathrooms === 1 ? '' : 's'}`;
  // A6(b) (rules.rate_kind, literal register wording): the 6-month rate applies to a January/February start that ends in July,
  // whichever year that July falls in. Dates are read from their ISO parts (YYYY-MM-DD), never via Date, so the month cannot
  // shift with the browser's time zone.
  const ymd = v => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || '')); return m ? { y: +m[1], m: +m[2], d: +m[3] } : null; };
  function rateKind(a, b) { const s = ymd(a), e = ymd(b); if (!s || !e) return '12m'; return ((s.m === 1 || s.m === 2) && e.m === 7) ? '6m' : '12m'; }

  window.mountHomeBotForm = function mount(root, opts) {
    opts = opts || {};
    const form = $('form', root);
    const nav = $('[data-role=nav]', root);
    const btnBack = $('[data-action=back]', nav), btnNext = $('[data-action=next]', nav), btnReturn = $('[data-action=toReview]', nav), btnSubmit = $('[data-action=submit]', nav);
    const count = $('[data-role=count]', nav);
    const formError = $('[data-role=formError]', root);
    const done = $('[data-role=done]', root);
    const tipLane = $('[data-role=tipLane]', root);
    const stepList = $('[data-role=stepList]', root);
    const meterLabel = $('[data-role=meterLabel]', root), meterFill = $('[data-role=meterFill]', root), meterCount = $('[data-role=meterCount]', root);
    let idx = 0;            // index into visible screens
    let dataReady = false;
    let submitKey = newKey();   // one key per form session; a retry of the same answers replays, never duplicates
    let submitting = false;
    const isTest = () => !!(opts.isTest && opts.isTest());
    // the MateBot hand-off destination: the saved form must land in the dataset the MateBot link opens (defaults to this page's mode)
    const handoffIsTest = () => opts.handoffIsTest ? !!opts.handoffIsTest() : isTest();
    let editing = false;    // came from the summary via Change (green return shown)
    let reviewedOnce = false;

    const steps = () => $$('.step', form);
    const reviewStep = () => $('.step[data-review]', form);
    const allScreens = () => $$('.scr', form);

    /* ---------- values and conditions ---------- */
    function value(name) {
      const els = $$(`[name="${name}"]`, form);
      if (!els.length) return '';
      if (els[0].type === 'radio') { const c = els.find(e => e.checked); return c ? c.value : ''; }
      if (els[0].type === 'checkbox') return els.filter(e => e.checked).map(e => e.value);
      return els[0].value;
    }
    function condOk(spec) {
      if (!spec) return true;
      return spec.split('&').every(part => {
        const [n, vs] = part.split('=');
        const v = n.trim() === 'people' ? (counts().now ? String(counts().now) : '') : value(n.trim());
        const want = (vs || '').split('|').map(s => s.trim());
        if (want.includes('*')) return Array.isArray(v) ? v.length > 0 : v !== '';   // "n=*": any answer at all
        return Array.isArray(v) ? v.some(x => want.includes(x)) : want.includes(v);
      });
    }
    function scrVisible(scr) {
      if (scr.dataset.showWhen && !condOk(scr.dataset.showWhen)) return false;
      if (scr.dataset.hideWhen && condOk(scr.dataset.hideWhen)) return false;
      return true;
    }
    /* "Which three matter most?": chips mirror the ticked features; an unticked feature drops out; at most three, the rest
       are simply disabled. Wishes only: never a filter, never a ranking. */
    const EXTRAS_MAX_TOP = 3;
    function renderExtrasTop() {
      const box = $('[data-role=extrasTop]', form); if (!box) return;
      const ticked = value('extras'); const keep = new Set(value('extras_top'));
      box.innerHTML = ticked.map(l => `<label class="chip"><input type="checkbox" name="extras_top" value="${esc(l)}" ${keep.has(l) ? 'checked' : ''}>${esc(l)}</label>`).join('');
      const chosen = value('extras_top');
      if (chosen.length >= EXTRAS_MAX_TOP) $$('input[name="extras_top"]', box).forEach(i => { if (!i.checked) { i.disabled = true; i.closest('.chip').classList.add('is-disabled'); } });
      const hint = $('[data-role=extrasTopHint]', form);
      if (hint) hint.textContent = chosen.length >= EXTRAS_MAX_TOP ? 'That is your three. Untick one to swap it.' : chosen.length ? `${chosen.length} chosen; you can pick ${EXTRAS_MAX_TOP - chosen.length} more.` : 'Pick up to three of the features you ticked.';
    }
    function applyReveals() {
      renderExtrasTop();
      $$('.reveal[data-show-when]', form).forEach(r => r.classList.toggle('is-shown', condOk(r.dataset.showWhen)));
      syncSleep();   // the bunk-condition reveal depends on the per-person bed choices, not on a single control
    }
    function shown(el) {
      if (el.closest('.reveal') && !el.closest('.reveal').classList.contains('is-shown')) return false;
      if (el.closest('.person-sub') && el.closest('.person-sub').hidden) return false;
      const scr = el.closest('.scr');
      return !scr || scrVisible(scr);
    }
    const visibleScreens = () => allScreens().filter(scrVisible);
    const current = () => visibleScreens()[idx];

    /* ---------- derived numbers (H3/H4): current people vs wanted people ---------- */
    function counts() {
      const mode = value('need_more');
      if (mode === 'more') {
        const now = parseInt(value('headcount_now'), 10) || 0;
        const tgt = value('headcount_target');
        const target = tgt === '8+' ? 8 : parseInt(tgt, 10) || 0;
        return { mode, now, target, help: value('want_help'), complete: false, tooMany: tgt === '8+' };
      }
      const h = value('headcount');
      const now = h === '8+' ? 8 : parseInt(h, 10) || 0;
      return { mode, now, target: now, help: '', complete: true, tooMany: h === '8+' };
    }
    function peopleCount() { const c = counts(); return Math.min(MAX_PEOPLE, Math.max(1, c.now || 1)); }

    function refreshDerived() {
      const c = counts();
      const extra = Math.max(0, c.target - c.now);
      $$('[data-role=hoNow]', form).forEach(e => e.textContent = c.now || '?');
      $$('[data-role=hoTarget]', form).forEach(e => e.textContent = c.target || '?');
      $$('[data-role=hoExtra]', form).forEach(e => e.textContent = c.now && c.target ? extra : '?');
      $$('[data-role=hoNowNoun]', form).forEach(e => e.textContent = c.now === 1 ? 'person' : 'people');
      $$('[data-role=hoExtraNoun]', form).forEach(e => e.textContent = extra === 1 ? 'housemate' : 'housemates');
      const stay = $('[data-role=stayHint]', form);
      if (stay) { const a = value('move_in'), b = value('move_out'); const days = a && b ? Math.round((new Date(b) - new Date(a)) / 86400000) : 0; stay.textContent = days > 0 ? `That's about ${days < 28 ? days + ' days' : Math.max(1, Math.round(days / 30.4)) + ' month' + (Math.round(days / 30.4) > 1 ? 's' : '')}.` : ''; }
      const hint = $('[data-role=countsHint]', form);
      if (hint) {
        if (c.now && c.target && !c.tooMany) hint.textContent = c.target <= c.now ? 'That total is not more than you have now — if your group is already complete, go back and choose "Yes, we\'re complete".' : `So you are looking for ${extra} more ${extra === 1 ? 'person' : 'people'}.`;
        else hint.textContent = '';
        hint.classList.toggle('counts-big--warn', !!(c.now && c.target && !c.tooMany && c.target <= c.now));
      }
    }

    /* ---------- price guide (H9): checkbox list, one box per actual apartment type with its whole-apartment weekly range
       (from the shared apartment list via the API). More than one type may be ticked; the guide is required and there is no
       "Not sure". Ticked codes survive a re-render (date change → rate refresh). ---------- */
    function rentOf(t, kind) { return t[kind === '6m' ? 'rent_6m' : 'rent_12m'] || null; }
    function renderBudget() {
      const host = $('[data-role=budgetList]', form);
      if (!host) return;
      const kind = rateKind(value('move_in'), value('move_out'));
      const keep = new Set($$('[name=budget_types]:checked', form).map(b => b.value));
      const boxes = DATA.types.map(t => {
        const r = rentOf(t, kind);
        const range = r && r.min != null ? `${money(r.min)} to ${money(r.max)} a week, whole apartment` : 'ask the team for the current rent';
        return `<label class="price" data-testid="price-${esc(t.code)}"><input type="checkbox" name="budget_types" value="${esc(t.code)}"${keep.has(t.code) ? ' checked' : ''}><span><b>${esc(typeLabel(t))}</b><small>${range}</small></span></label>`;
      });
      host.innerHTML = boxes.join('');
      const note = $('[data-role=rateNote]', form);
      if (note) note.textContent = kind === '6m' ? 'Your dates (January or February start, July end) use ALM\'s 6-month rates.' : 'Standard 12-month rates. A January or February start ending in July uses ALM\'s 6-month rates instead.';
      $$('[data-role=extraBed]', form).forEach(e => e.textContent = money(DATA.extra_bed_charge_per_week));
    }
    // every ticked type with the rate that applies to the entered dates (summary and email)
    function budgetText() {
      const picked = value('budget_types'); if (!Array.isArray(picked) || !picked.length) return '';
      const kind = rateKind(value('move_in'), value('move_out'));
      const parts = [];
      DATA.types.forEach(t => { if (picked.includes(t.code)) { const r = rentOf(t, kind); parts.push(`${typeLabel(t)}${r && r.min != null ? ` · ${money(r.min)} to ${money(r.max)} a week for the whole apartment (${kind === '6m' ? '6-month' : '12-month'} rate guide)` : ''}`); } });
      return parts.join('; ');
    }
    function loadData() {
      if (!API.ok) { showApiError(); return Promise.resolve(); }
      return fetch(API.base + '/api/public/homebot/meta', { cache: 'no-store', credentials: 'omit' }).then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }).then(d => {
        if (!d || !Array.isArray(d.types) || !d.types.length || !Array.isArray(d.institutions)) throw new Error('bad data');
        DATA = d; dataReady = true; $$('[data-role=dataError]', form).forEach(err => { err.hidden = true; err.textContent = ''; }); renderBudget(); renderSleepRows(); renderPeople();
      }).catch(() => {
        dataReady = false;
        DATA = { types: [], institutions: [{ name: 'Other', group: 'Other' }], extra_bed_charge_per_week: DATA.extra_bed_charge_per_week };
        $$('[data-role=dataError]', form).forEach(err => { err.hidden = false; err.innerHTML = 'The price guide and the list of study providers could not be loaded from the enquiry service, so no prices are shown. <button type="button" class="linkbtn" data-action="reloadData">Try loading again</button> or reload the page. The enquiry cannot be sent until this loads.'; });
        renderBudget(); renderSleepRows(); renderPeople();
      });
    }
    function showApiError() {
      const badge = document.querySelector('[data-role=apiBadge]'); if (badge) badge.hidden = false;
      $$('[data-role=dataError]', form).forEach(err => { err.hidden = false; err.textContent = 'This copy of the form is not connected to the enquiry service, so no prices are shown and nothing can be saved. Please tell the ALM team.'; });
    }

    /* ---------- section 3: one row per person — optional first name, bed choice (H21) and weekly maximum (H9) ---------- */
    function personName(i) { return i === 1 || i === '1' ? (value('first_name') || 'You').trim() : (value(`person_${i}_name`) || '').trim(); }
    function renderSleepRows() {
      const host = $('[data-role=sleepRows]', form);
      if (!host) return;
      const n = peopleCount();
      const keep = {};
      $$('input, select', host).forEach(el => { keep[el.name] = el.value; });
      const rows = [];
      for (let i = 1; i <= n; i++) {
        const isLead = i === 1;
        const sleepOpts = ['<option value="">Choose</option>'].concat(SLEEP_CHOICES.map(c => `<option>${esc(c)}</option>`)).join('');
        rows.push(`<div class="party-row sleep-row" data-person="${i}">
          <div class="party-row__title">${isLead ? `Person 1 · ${esc(personName(1))} (you)` : `Person ${i}`}</div>
          ${isLead ? '' : `<div class="party-cell"><label for="person_${i}_name"><small>First name <span class="hint-inline">(optional)</span></small></label><input id="person_${i}_name" name="person_${i}_name" maxlength="40" autocomplete="off" data-testid="person_${i}_name"></div>`}
          <div class="party-cell party-cell--wide"><label for="person_${i}_sleep"><small>${isLead ? 'How you would sleep' : 'How they would sleep'}</small></label><select id="person_${i}_sleep" name="person_${i}_sleep" required data-testid="person_${i}_sleep">${sleepOpts}</select></div>
          <div class="party-cell"><label for="person_${i}_weekly_max"><small>${isLead ? 'The most you can pay a week ($)' : 'The most they can pay a week ($)'}</small></label><input id="person_${i}_weekly_max" name="person_${i}_weekly_max" type="number" min="1" step="1" inputmode="numeric" required placeholder="e.g. 300" data-testid="person_${i}_weekly_max"></div>
        </div>`);
      }
      host.innerHTML = rows.join('');
      Object.entries(keep).forEach(([k, v]) => { const el = $(`[name="${k}"]`, host); if (el) el.value = v; });
      syncSleep();
    }
    function sleepChoices() { const n = peopleCount(); const out = []; for (let i = 1; i <= n; i++) out.push(value(`person_${i}_sleep`)); return out; }
    function anyBunk() { return peopleCount() > 1 && sleepChoices().some(c => BUNK_CHOICES.includes(c)); }
    function syncSleep() {
      const fs = $('[data-role=bunkCondition]', form); if (!fs) return;
      const on = anyBunk(); fs.classList.toggle('is-shown', on);
      if (!on) $$('input', fs).forEach(b => { b.checked = false; });
      const hint = $('[data-role=budgetSumHint]', form);
      if (hint) { const n = peopleCount(); let sum = 0, all = true; for (let i = 1; i <= n; i++) { const v = parseInt(value(`person_${i}_weekly_max`), 10); if (v > 0) sum += v; else all = false; } hint.textContent = n > 1 && sum ? `For reference, the amounts you gave for each person add up to ${money(sum)} a week${all ? '' : ' so far'}.` : ''; }
    }

    /* ---------- per-person rows (section 6) ---------- */
    const STUDY = ['Studying', 'Studying and working'];
    const WORK = ['Working', 'Studying and working'];
    const MAYWORK = ['Looking for work'];
    /* Both per-person row sets follow the current headcount (section 3 beds, section 6 work/study). Existing answers are kept
       by renderSleepRows/renderPeople; extra rows drop off, new rows appear blank — so the summary, validation and the payload
       always describe exactly the people in the group today, however the count was changed (I19(m) green return included). */
    function reconcilePeople() { renderSleepRows(); renderPeople(); }
    function renderPeople() {
      const host = $('[data-role=peopleRows]', form);
      if (!host) return;
      const n = peopleCount();
      const keep = {};
      $$('input, select', host).forEach(el => { keep[el.name] = el.value; });
      const instOpts = ['<option value="">Choose</option>'].concat(DATA.institutions.map(i => `<option value="${esc(i.name)}">${esc(i.name)}</option>`)).join('');
      const rows = [];
      for (let i = 1; i <= n; i++) {
        const isLead = i === 1;
        const nm = personName(i);
        rows.push(`<div class="party-row person-row" data-person="${i}">
          <div class="party-row__title">${isLead ? `Person 1 · ${esc(nm)} (you)` : `Person ${i}${nm ? ' · ' + esc(nm) : ''}`}</div>
          <div class="party-cell"><label for="person_${i}_doing"><small>${isLead ? "What you'll be doing" : "What they'll be doing"}</small></label><select id="person_${i}_doing" name="person_${i}_doing" required data-testid="person_${i}_doing"><option value="">Choose</option><option>Studying</option><option>Working</option><option>Studying and working</option><option>Looking for work</option><option>Not yet decided</option></select></div>
          <div class="party-cell party-cell--wide person-sub" data-sub="study" hidden><label for="person_${i}_institution"><small>Where ${isLead ? 'you' : 'they'} study</small></label><select id="person_${i}_institution" name="person_${i}_institution" data-testid="person_${i}_institution">${instOpts}</select></div>
          <div class="party-cell party-cell--wide person-sub" data-sub="other" hidden><label for="person_${i}_institution_other"><small>Name of the institution</small></label><input id="person_${i}_institution_other" name="person_${i}_institution_other" maxlength="80" autocomplete="off" data-testid="person_${i}_institution_other"></div>
          <div class="party-cell party-cell--wide person-sub" data-sub="work" hidden><label for="person_${i}_place"><small data-role="placeLabel">Where ${isLead ? 'you' : 'they'} work <span class="hint-inline">(workplace or suburb)</span></small></label><input id="person_${i}_place" name="person_${i}_place" maxlength="120" autocomplete="off" data-testid="person_${i}_place"></div>
          <div class="party-cell party-cell--wide person-sub" data-sub="primary" hidden><label for="person_${i}_primary"><small>Main daily destination <span class="hint-inline">(the one ${isLead ? 'you' : 'they'} travel to most)</span></small></label><select id="person_${i}_primary" name="person_${i}_primary" data-testid="person_${i}_primary"><option value="">Choose</option><option value="Study">Study</option><option value="Work">Work</option></select></div>
        </div>`);
      }
      host.innerHTML = rows.join('');
      Object.entries(keep).forEach(([k, v]) => { const el = $(`[name="${k}"]`, host); if (el) el.value = v; });
      syncPeople();
    }
    // show the study / work sub-questions that match each person's answer (H12); hidden answers are cleared
    function syncPeople() {
      $$('.person-row', form).forEach(row => {
        const i = row.dataset.person; const doing = value(`person_${i}_doing`);
        const study = STUDY.includes(doing), work = WORK.includes(doing) || MAYWORK.includes(doing);  // H12: no campus/address prompt for students
        const inst = value(`person_${i}_institution`);
        const show = { study, other: study && inst === 'Other', work, primary: doing === 'Studying and working' };
        const pl = $('[data-role=placeLabel]', row); const you = i === '1' ? 'you' : 'they';
        if (pl) pl.innerHTML = MAYWORK.includes(doing) ? `Where might ${you} work? <span class="hint-inline">(optional — preferred work area, if known)</span>`
          : `Where ${you} work <span class="hint-inline">(workplace or suburb)</span>`;
        $$('.person-sub', row).forEach(cell => {
          const on = !!show[cell.dataset.sub]; cell.hidden = !on;
          const el = $('input, select', cell); if (!on && el) el.value = '';
          if (el) el.required = on && !(cell.dataset.sub === 'work' && !WORK.includes(doing));
        });
      });
    }
    function personText(i) {
      const doing = value(`person_${i}_doing`); if (!doing) return 'Not provided';
      const parts = [doing];
      if (STUDY.includes(doing)) { const inst = value(`person_${i}_institution`); const other = value(`person_${i}_institution_other`); if (inst) parts.push('at ' + (inst === 'Other' ? (other ? other + ' (other institution)' : 'Other') : inst)); }
      if (WORK.includes(doing)) { const w = value(`person_${i}_place`); parts.push(w ? `works at ${w}` : 'workplace not provided'); }
      if (doing === 'Studying and working') { const pr = value(`person_${i}_primary`); parts.push(pr ? `main destination: ${pr.toLowerCase()}` : 'main destination not chosen'); }
      if (MAYWORK.includes(doing)) { const w = value(`person_${i}_place`); parts.push(w ? `may work around ${w}` : 'preferred work area not provided'); }
      return parts.join(' · ');
    }
    function sleepText(i) {
      const s = value(`person_${i}_sleep`); const m = value(`person_${i}_weekly_max`);
      return `${s || 'bed choice not made'} · up to ${m ? money(m) : '—'} a week`;
    }

    /* ---------- chrome: steps list, meter, tip ---------- */
    function stepOf(scr) { return scr.closest('.step'); }
    function visibleSteps() {
      const vs = visibleScreens();
      return steps().filter(s => vs.some(scr => stepOf(scr) === s));
    }
    function renderSteps() {
      const cur = current(); const curStep = cur && stepOf(cur);
      const vsteps = visibleSteps();
      stepList.innerHTML = vsteps.map((s, i) => {
        const reached = vsteps.indexOf(curStep) >= i;
        return `<li><button type="button" data-goto-step="${s.dataset.step}" ${s === curStep ? 'aria-current="step"' : ''} ${reached || reviewedOnce ? '' : 'disabled'}>${esc(s.dataset.title)}</button></li>`;
      }).join('');
    }
    function renderMeter() {
      const vs = visibleScreens(); const n = vs.length; const i = idx;
      const pct = n > 1 ? Math.round((i / (n - 1)) * 100) : 0;
      meterFill.style.width = pct + '%';
      const cur = current();
      meterLabel.textContent = cur && stepOf(cur).hasAttribute('data-review') ? 'Nearly done' : (cur && cur.dataset.terminal != null ? 'A quick word' : (i === 0 ? "Let's begin" : pct < 50 ? 'Good start' : pct < 85 ? 'Over halfway' : 'Nearly done'));
      meterCount.textContent = `${Math.min(i + 1, n)} of ${n}`;
      count.textContent = `${Math.min(i + 1, n)} of ${n}`;
    }
    function renderTip(scr) {
      const t = scr && scr.dataset.tip;
      tipLane.hidden = !t;
      tipLane.innerHTML = t ? `<img class="tip__bot" src="${TIP_IMG}" alt="" width="393" height="651"><p class="tip__bubble is-speaking">${esc(t)}</p>` : '';
    }

    /* ---------- summary (H5) ---------- */
    function labelFor(el) {
      const field = el.closest('.field');
      if (!field) return el.name;
      const lg = $('legend', field);
      if (lg) return lg.childNodes[0] ? lg.textContent.replace(/\(optional\)/i, '').trim() : el.name;
      const lab = $('label', field);
      if (!lab) return el.name;
      const clone = lab.cloneNode(true);
      $$('input, select, textarea, .hint-inline', clone).forEach(x => x.remove());
      return clone.textContent.trim();
    }
    function reviewRows(step, full) {
      const rows = []; const seen = new Set();
      $$('input, select, textarea', step).filter(el => shown(el) && el.name && !el.closest('.person-row') && !el.closest('.sleep-row')).forEach(el => {
        if (seen.has(el.name)) return; seen.add(el.name);
        const v = value(el.name);
        let text;
        if (el.type === 'radio') { const c = $$(`[name="${el.name}"]`, form).find(e => e.checked); text = c ? (c.closest('.opt') ? $('b', c.closest('.opt')).textContent : c.parentElement.textContent.trim()) : ''; }
        else if (el.type === 'checkbox') {
          const checked = $$(`[name="${el.name}"]:checked`, form);
          if (el.closest('.consent')) text = checked.length ? 'Confirmed' : 'Not confirmed';
          else if (el.name === 'budget_types') text = budgetText();
          else text = checked.map(c => c.parentElement.textContent.trim()).join(', ');
        }
        else if (el.name === 'phone_code') return;
        else if (el.name === 'phone') { const code = value('phone_code'); text = (code && code !== 'other' ? code + ' ' : '') + v; }
        else if (el.tagName === 'SELECT') text = el.selectedIndex > 0 ? el.options[el.selectedIndex].text : '';
        else if (el.type === 'number') text = v ? money(v) + ' a week' : '';
        else text = v;
        if (el.type === 'checkbox' && el.closest('.consent')) { const ct = el.parentElement.textContent.replace(/Review mode: nothing is stored or sent yet\./, '').replace(/\s+/g, ' ').trim(); rows.push([ct, text, step]); }
        else rows.push([labelFor(el), text || (el.name === 'extras_top' ? 'None singled out' : el.type === 'checkbox' ? 'No preferences selected' : 'Not provided'), step]);
      });
      // per-person rows (section 3: bed and weekly maximum; section 6: work and study)
      const sleepScr = $('[data-scr=sleeping]', step);
      if (sleepScr && scrVisible(sleepScr)) $$('.sleep-row', step).forEach(row => {
        const i = row.dataset.person;
        const name = i === '1' ? `${personName(1)} (you)` : (personName(i) || `Person ${i}`);
        rows.push([`Person ${i}: ${name}`, sleepText(i), step]);
      });
      if (sleepScr && !scrVisible(sleepScr) && peopleCount() === 1) rows.push([`Person 1: ${personName(1)} (you)`, 'Whole apartment to yourself', step]);
      $$('.person-row', step).forEach(row => {
        const i = row.dataset.person;
        const name = i === '1' ? `${personName(1)} (you)` : (personName(i) || `Person ${i}`);
        rows.push([`Person ${i}: ${name}`, personText(i), step]);
      });
      return rows;
    }
    function renderReview() {
      const host = $('[data-role=reviewGroups]', form);
      const groups = visibleSteps().filter(s => !s.hasAttribute('data-review'));
      host.innerHTML = groups.map((s, i) => {
        const rows = reviewRows(s);
        if (!rows.length) return '';
        return `<div class="review__group"><div class="review__head"><h4>${esc(s.dataset.title)}</h4><button type="button" class="review__change" data-change-step="${s.dataset.step}" data-testid="review-change-${s.dataset.step}">Change</button></div>
          <dl class="review__list">${rows.map(([l, v]) => `<div class="review__row"><dt>${esc(l)}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl></div>`;
      }).join('');
      const flag = $('[data-role=reviewFlag]', form);
      const c = counts();
      if (c.mode === 'more' && c.help === 'no') { flag.hidden = false; flag.innerHTML = `<strong>Group still growing — reviewed by hand.</strong> ${c.now} ${c.now === 1 ? 'person' : 'people'} now, ${c.target} wanted. The team will not look for the missing ${Math.max(0, c.target - c.now)}; the team follows this enquiry up rather than treating it as ready for an apartment.`; }
      else { flag.hidden = true; flag.textContent = ''; }
    }

    /* ---------- validation ---------- */
    function clearErrors(scope) { $$('.has-error', scope).forEach(e => e.classList.remove('has-error')); $$('.field-error', scope).forEach(e => e.remove()); }
    function flag(el, msg) {
      const f = el.closest('.consent') || el.closest('.party-cell') || el.closest('.field') || el;
      if (f.classList.contains('has-error')) return;
      f.classList.add('has-error');
      const p = document.createElement('p'); p.className = 'field-error'; p.textContent = msg; f.append(p);
    }
    function validate(scr) {
      clearErrors(scr);
      let ok = true;
      $$('fieldset[data-required-group]', scr).filter(shown).forEach(fs => {
        const boxes = $$('input[type=radio], input[type=checkbox]', fs);
        const checked = boxes.filter(b => b.checked);
        if (fs.hasAttribute('data-all-required') ? checked.length !== boxes.length : !checked.length) { ok = false; flag(fs, fs.hasAttribute('data-all-required') ? 'Please tick each box to continue.' : 'Please choose an option.'); }
      });
      $$('input, select, textarea', scr).filter(shown).forEach(el => {
        if (el.type === 'radio' || el.type === 'checkbox') return;
        const v = (el.value || '').trim();
        if (el.required && !v) { ok = false; flag(el, 'Please fill this in.'); return; }
        if (v && el.type === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) { ok = false; flag(el, 'That email does not look right.'); }
        if (v && el.type === 'tel' && (!/^[0-9 ().+-]{6,30}$/.test(v) || (v.match(/\d/g) || []).length < 6)) { ok = false; flag(el, 'Digits, spaces and dashes only (at least 6 digits).'); }
        if (v && el.name === 'phone' && value('phone_code') === 'other' && !/^\+[1-9]/.test(v)) { ok = false; flag(el, 'Please start with + and the country code, e.g. +33 6 12 34 56 78.'); }
        if (v && el.type === 'number') { const n = Number(v); if (!Number.isFinite(n) || n <= 0 || n !== Math.floor(n)) { ok = false; flag(el, 'Please enter a positive whole-dollar amount.'); } }
        if (v && el.type === 'date') { const d = new Date(v); const today = new Date(); today.setHours(0, 0, 0, 0); if (isNaN(d) || d < today) { ok = false; flag(el, 'Please choose a date from today onwards.'); } }
      });
      // H3: wanted total must exceed current size when asking for more people
      if (scr.dataset.scr === 'counts') { const c = counts(); if (!c.tooMany && c.now && c.target && c.target <= c.now) { ok = false; flag($('[name=headcount_target]', scr), 'Choose a total bigger than your group today, or go back and say your group is complete.'); } }
      // H28(f): "flexible by a month or more" needs finite earliest/latest limits around the likely date
      const window_ = (base, lo, hi, host) => { const b = value(base), l = value(lo), h = value(hi); if (!(b && l && h)) return; if (!(l <= b && b <= h)) { ok = false; flag($(`[name=${hi}]`, host), 'Earliest ≤ likely date ≤ latest.'); } };
      if (scr.dataset.scr === 'movein' && value('move_in_flex') === 'Flexible by a month or more') window_('move_in', 'move_in_earliest', 'move_in_latest', scr);
      if (scr.dataset.scr === 'moveout') {
        const a = value('move_in'), b = value('move_out'); if (a && b && b <= a) { ok = false; flag($('[name=move_out]', scr), 'Move-out must be after move-in.'); }
        if (value('move_out_flex') === 'Flexible by a month or more') { window_('move_out', 'move_out_earliest', 'move_out_latest', scr); const lo = value('move_out_earliest') || b, inLo = value('move_in_earliest') || a; if (lo && inLo && lo <= inLo) { ok = false; flag($('[name=move_out_earliest]', scr), 'The earliest move-out must be after the earliest move-in.'); } }
      }
      // H21: per-person bed choices must be feasible — partners come in pairs, at most three bedrooms
      if (scr.dataset.scr === 'sleeping' && ok) {
        const ch = sleepChoices(); const partners = ch.filter(c => c === 'Shares a bed with partner').length; const own = ch.filter(c => c === 'Own bedroom').length;
        const first = (which) => { const i = ch.findIndex(c => c === which) + 1; return $(`[name=person_${i}_sleep]`, scr) || $('[data-role=sleepRows]', scr); };
        if (partners % 2 === 1) { ok = false; flag(first('Shares a bed with partner'), '“Shares a bed with partner” needs both partners to choose it — an even number of people.'); }
        else if (own > 3) { ok = false; flag(first('Own bedroom'), 'No apartment has more than three bedrooms, so at most three people can have a bedroom to themselves.'); }
        else if (own + partners / 2 > 3) { ok = false; flag(first('Own bedroom'), 'Those choices need more bedrooms than any apartment has (three at most).'); }
      }
      return ok;
    }

    /* ---------- navigation ---------- */
    function go(i, opts2) {
      const vs = visibleScreens();
      const t = vs.findIndex(s => s.hasAttribute('data-terminal'));
      idx = Math.max(0, Math.min(i, vs.length - 1));
      if (t >= 0 && idx > t) idx = t;
      const scr = vs[idx];
      if (stepOf(scr).hasAttribute('data-review')) reconcilePeople();
      allScreens().forEach(s => s.classList.toggle('is-active', s === scr));
      steps().forEach(s => { const on = s === stepOf(scr); s.classList.toggle('is-active', on); s.classList.toggle('is-current', on); });
      const isReview = stepOf(scr).hasAttribute('data-review');
      const terminal = scr.hasAttribute('data-terminal');
      if (isReview) { renderReview(); reviewedOnce = true; editing = false; }
      if (scr.dataset.scr === 'people' || scr.dataset.scr === 'sleeping') reconcilePeople();
      refreshDerived();
      if (scr.dataset.scr === 'handoff') prepareHandoff(scr);
      btnBack.hidden = idx === 0;
      btnNext.hidden = isReview || terminal;
      btnSubmit.hidden = !isReview;
      btnReturn.hidden = !(editing && !isReview && !terminal);
      nav.classList.toggle('is-editing', editing && !isReview);
      formError.hidden = true;
      renderTip(scr); renderSteps(); renderMeter();
      if (!(opts2 && opts2.silent)) { root.scrollIntoView({ behavior: 'smooth', block: 'start' }); const first = $('input:not([type=hidden]), select, textarea', scr); if (first && window.matchMedia('(min-width: 760px)').matches) try { first.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    }
    /* ---------- H5 hand-off: carry the party's size answers to MateBot as a saved form (server/homebot_handoff.py) ----------
       The server stores only the compatible answers (how many of you, how many others you would like); MateBot opens on its
       first screen so each carried answer is reviewed, and asks everything else itself. If the server cannot be reached the
       link falls back to the plain MateBot form and the notice says nothing was carried (never implied, H2). */
    let handoff = { key: '', token: '', pending: null };
    function prepareHandoff(scr) {
      const c = counts(); const key = `${c.now}:${c.target}:${handoffIsTest() ? 't' : ''}`;
      const notice = $('[data-role=handoffNotice]', scr), list = $('[data-role=handoffCarried]', scr), fb = $('[data-role=handoffFallback]', scr), intro = $('[data-role=handoffCarriedIntro]', scr);
      const links = $$('[data-role=matebotLink]', scr);
      const baseHref = a => a.dataset.baseHref || (a.dataset.baseHref = a.getAttribute('href'));
      const setLinks = token => links.forEach(a => { const b = baseHref(a); const test = /(^|[?&])test=1(&|$)/.test(b.split('#')[1] || ''); a.href = token ? b.split('#')[0] + '#/resume?t=' + encodeURIComponent(token) + (test ? '&test=1' : '') : b; });
      const show = (state, items) => { if (notice) notice.dataset.state = state; if (intro) intro.hidden = state === 'fallback'; if (fb) fb.hidden = state !== 'fallback'; if (list) list.innerHTML = (items || []).map(t => `<li>${esc(t)}</li>`).join(''); };
      if (handoff.key === key && handoff.token) { setLinks(handoff.token); return; }
      const fail = () => { handoff = { key, token: '', pending: null }; setLinks(''); show('fallback'); };
      if (!API.ok || !c.now || !c.target || c.target <= c.now) { fail(); return; }
      setLinks(''); show('pending', ['Preparing the MateBot form…']);
      const body = JSON.stringify({ headcount_now: c.now, headcount_target: c.target, is_test: handoffIsTest() });
      const p = fetch(API.base + '/api/public/homebot/handoff', { method: 'POST', credentials: 'omit', headers: { 'Content-Type': 'application/json' }, body })
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(d => { if (handoff.pending !== p) return; handoff = { key, token: d.token, pending: null }; setLinks(d.token); show('ready', d.carried); })
        .catch(() => { if (handoff.pending === p) fail(); });
      handoff = { key, token: '', pending: p };
    }
    function firstIndexOfStep(stepNo) { const vs = visibleScreens(); return Math.max(0, vs.findIndex(s => stepOf(s).dataset.step === String(stepNo))); }
    function reviewIndex() { const vs = visibleScreens(); return vs.findIndex(s => stepOf(s).hasAttribute('data-review')); }
    // an active routing screen (8+ people, MateBot hand-off) ends the form: nothing after it may be reached
    function activeTerminalIndex() { const vs = visibleScreens(); return vs.findIndex(s => s.hasAttribute('data-terminal')); }
    function guardTerminal() { const t = activeTerminalIndex(); if (t >= 0) { go(t); return true; } return false; }
    function next() {
      const scr = current();
      if (!validate(scr)) { formError.hidden = false; formError.textContent = 'Please check the highlighted answer.'; return; }
      go(idx + 1);
    }
    function toReview() {
      if (guardTerminal()) return;
      reconcilePeople();
      // H5: validate everything on the way back so the summary is never shown over an invalid answer
      const vs = visibleScreens();
      for (let i = 0; i < vs.length; i++) { if (vs[i].hasAttribute('data-terminal') || stepOf(vs[i]).hasAttribute('data-review')) continue; if (!validate(vs[i])) { editing = true; go(i); formError.hidden = false; formError.textContent = 'Please check the highlighted answer before returning to the summary.'; return; } }
      go(reviewIndex());
    }
    function submit() {
      if (guardTerminal()) return;
      reconcilePeople();
      const vs = visibleScreens();
      for (let i = 0; i < vs.length; i++) { if (vs[i].hasAttribute('data-terminal') || stepOf(vs[i]).hasAttribute('data-review')) continue; if (!validate(vs[i])) { editing = true; go(i); formError.hidden = false; formError.textContent = 'Please check the highlighted answer.'; return; } }
      if (!API.ok) { showApiError(); formError.hidden = false; formError.textContent = 'This copy of the form is not connected to the enquiry service, so the enquiry cannot be saved.'; return; }
      if (!dataReady) { formError.hidden = false; formError.textContent = 'The price guide has not loaded yet, so the enquiry cannot be sent. Please try "Try loading again" on the budget step.'; return; }
      if (submitting) return;
      // Snapshot the answers now: the done screen and the applicant's copy show exactly what was sent, never later edits.
      // While the request is pending the whole form (inputs and navigation) is inert so nothing can change under it.
      submitting = true; btnSubmit.textContent = 'Saving…'; form.inert = true; $('.steps', root).inert = true;
      const body = payload();
      const snapshot = { counts: counts(), first_name: value('first_name').trim(), answers: answersText() };
      fetch(API.base + '/api/public/homebot/enquiries', { method: 'POST', credentials: 'omit', headers: { 'Content-Type': 'application/json', 'X-Submission-Key': submitKey }, body: JSON.stringify(body) })
        .then(r => r.json().then(j => ({ status: r.status, j })).catch(() => ({ status: r.status, j: {} })))
        .then(({ status, j }) => {
          if (status === 201 || status === 200) { showDone(j, snapshot); return; }
          const errs = (j && j.errors) || {};
          if (status === 409 && j && j.already_submitted) {
            // Same session key, saved earlier (e.g. the first response was lost): the server keeps the ORIGINAL answers and refuses
            // the later changes. Say so with the original reference; the current answers are never presented as saved.
            const ref = j.result && j.result.reference;
            formError.hidden = false; formError.textContent = errs.form || `This enquiry was already saved${ref ? ' as ' + ref : ''}. The later changes were not saved and no second enquiry was made — please contact the team quoting your reference if something needs changing.`;
            return;
          }
          const msgs = Object.keys(errs).map(k => (k === 'form' ? '' : k.replace(/^people\.(\d+)\.(.*)$/, 'Person $1 — $2').replace(/_/g, ' ') + ': ') + errs[k]);
          formError.hidden = false; formError.textContent = msgs.length ? 'Not saved — ' + msgs.join(' · ') : 'Not saved — please check your answers and try again.';
        })
        .catch(() => { formError.hidden = false; formError.textContent = 'Could not reach the enquiry service. Check your connection and press Submit again — the same enquiry is never saved twice.'; })
        .then(() => { submitting = false; btnSubmit.textContent = 'Submit'; form.inert = false; $('.steps', root).inert = false; });
    }
    /* what is sent: exactly the fields the server allows (server/homebot.py ALLOWED_KEYS); nothing about sex, age or nationality exists */
    function payload() {
      const c = counts(); const n = peopleCount();
      const people = [];
      for (let i = 1; i <= n; i++) {
        const doing = value(`person_${i}_doing`);
        const p = { weekly_max: n === 1 ? Number(value('budget_total')) : Number(value(`person_${i}_weekly_max`)), doing };
        if (n > 1) p.sleep = value(`person_${i}_sleep`);
        if (i > 1 && personName(i)) p.name = personName(i);
        if (STUDY.includes(doing)) { p.institution = value(`person_${i}_institution`); if (p.institution === 'Other') p.institution_other = value(`person_${i}_institution_other`); }
        if (doing !== 'Not yet decided' && value(`person_${i}_place`)) p.place = value(`person_${i}_place`);
        if (doing === 'Studying and working') p.primary = value(`person_${i}_primary`);
        people.push(p);
      }
      const b = {
        is_test: isTest(), company_website: value('company_website') || '',
        need_more: c.mode,
        first_name: value('first_name').trim(), last_name: value('last_name').trim(), email: value('email').trim(),
        phone_code: value('phone_code'), phone: value('phone').trim(), contact_pref: value('contact_pref'),
        people,
        move_in: value('move_in'), move_in_flex: value('move_in_flex'), move_out: value('move_out'), move_out_flex: value('move_out_flex'),
        budget_total: Number(value('budget_total')), budget_types: value('budget_types'), price_guide: rateKind(value('move_in'), value('move_out')) === '6m' ? 'rent_6m' : 'rent_12m',
        bedrooms: value('bedrooms'), bathrooms: value('bathrooms'), area: value('area'), transport: value('transport'), max_travel: value('max_travel'),
        extras: value('extras'), extras_top: value('extras_top'), apartment_notes: value('apartment_notes').trim(), viewing: value('viewing'), heard: value('heard'),
        consent_contact: !!value('consent_contact').length, consent_lease: !!value('consent_lease').length, consent_adults: !!value('consent_adults').length,
      };
      if (c.mode === 'complete') b.headcount = c.now; else { b.headcount_now = c.now; b.headcount_target = c.target; b.want_help = c.help; }
      if (value('move_in_flex') === 'Flexible by a month or more') { b.move_in_earliest = value('move_in_earliest'); b.move_in_latest = value('move_in_latest'); }
      if (value('move_out_flex') === 'Flexible by a month or more') { b.move_out_earliest = value('move_out_earliest'); b.move_out_latest = value('move_out_latest'); }
      if (anyBunk()) b.bunk_condition = value('bunk_condition');
      return b;
    }
    function showDone(res, snap) {
      form.hidden = true; $('.steps', root).hidden = true; $('.meter', root).hidden = true; tipLane.hidden = true;
      $('[data-role=doneName]', done).textContent = snap.first_name;
      $('[data-role=doneRef]', done).textContent = (res && res.reference) || '';
      const c = snap.counts; const df = $('[data-role=doneFlag]', done);
      const bits = [];
      if (c.mode === 'more' && c.help === 'no') bits.push(`Your group is shown to the team as still growing (${c.now} of ${c.target} people) and followed up by hand, not treated as ready for an apartment.`);
      if (res && res.is_test) bits.push('This was saved as a TEST enquiry, kept apart from real enquiries.');
      if (res && res.replayed) bits.push('This enquiry had already been saved — it was not saved a second time.');
      df.hidden = !bits.length; df.textContent = bits.join(' ');
      $('[data-role=receiptNotice]', done).textContent = receiptNotice(res);
      renderEmail(res, snap);
      done.hidden = false; done.focus(); root.scrollIntoView({ behavior: 'smooth', block: 'start' });
      if (opts.onDone) opts.onDone(res);
    }
    function receiptNotice(res) {
      const messages = {
        queued: 'Your email copy was queued for delivery.',
        sending: 'Your email copy is being processed; delivery is not confirmed here.',
        sent: 'Your email copy was submitted to the email service. Please check your inbox and junk folder.',
        dryrun: 'This was an email test only; no email was sent.',
        held: 'Automatic email is held for this enquiry; no delivery is confirmed here.',
        failed: 'Your enquiry is saved, but the email copy could not be sent.',
        error: 'Your enquiry is saved, but the email copy could not be prepared.',
        cancelled: 'The email copy was cancelled.',
      };
      return (messages[res && res.receipt] || 'Email delivery is not confirmed here.') +
        ' You can keep a copy of your answers below. The team reviews apartment options before contacting you.';
    }
    /* ---------- the applicant's copy; submission acknowledgement is not a match or inbox-delivery claim ---------- */
    function answersText() {
      const c = counts();
      const lines = [];
      if (c.mode === 'more' && c.help === 'no') lines.push(`Group still growing: ${c.now} now, ${c.target} wanted — you are finding the other people yourselves.`, '');
      lines.push('Your answers', '===========');
      visibleSteps().filter(s => !s.hasAttribute('data-review')).forEach(step => {
        const rows = reviewRows(step, true); if (!rows.length) return;
        lines.push('', step.dataset.title, '-'.repeat(step.dataset.title.length));
        rows.forEach(([l, v]) => lines.push(`${l}: ${v}`));
      });
      return lines.join('\n');
    }
    function renderEmail(res, snap) {
      const box = $('[data-role=emailBox]', done); if (!box) return;
      const head = [`HomeBot enquiry${res && res.reference ? ' ' + res.reference : ''} — Apartment Living Melbourne`, '', 'Your enquiry has been saved. The Apartment Living Melbourne reservations team will contact you about the next steps.', receiptNotice(res), ''];
      $('[data-role=emailBody]', box).value = head.join('\n') + '\n' + snap.answers;
      $('[data-role=emailCopied]', box).hidden = true;
    }
    function copyEmail() {
      const box = $('[data-role=emailBox]', done); const ta = $('[data-role=emailBody]', box);
      const text = ta.value;
      const msg = $('[data-role=emailCopied]', box);
      const ok = () => { msg.textContent = 'Copied'; msg.hidden = false; };
      const manual = () => { ta.focus(); ta.select(); msg.textContent = 'Copying is blocked in this browser — the text is selected, press Ctrl+C or Cmd+C to copy it.'; msg.hidden = false; };
      const legacy = () => { ta.select(); let done = false; try { done = document.execCommand('copy') === true; } catch (e) { done = false; } return done ? ok() : manual(); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(ok, legacy);
      else legacy();
    }

    function restart() {
      form.reset(); $$('[data-role=peopleRows], [data-role=sleepRows]', form).forEach(h => (h.innerHTML = ''));
      clearErrors(form); editing = false; reviewedOnce = false; submitKey = newKey();
      done.hidden = true; form.hidden = false; $('.steps', root).hidden = false; $('.meter', root).hidden = false; tipLane.hidden = false;
      const ta = $('[data-role=emailBody]', done); if (ta) ta.value = ''; $('[data-role=doneRef]', done).textContent = '';
      applyReveals(); renderBudget(); syncSleep(); go(0);
    }

    /* ---------- events ---------- */
    function clearBranch(mode) {
      const names = mode === 'complete' ? ['headcount_now', 'headcount_target', 'want_help'] : ['headcount'];
      names.forEach(n => $$(`[name="${n}"]`, form).forEach(el => { if (el.type === 'radio' || el.type === 'checkbox') el.checked = false; else el.value = ''; }));
    }
    form.addEventListener('change', e => {
      if (e.target && e.target.name === 'need_more') clearBranch(e.target.value);
      if (e.target && e.target.type === 'checkbox' && e.target.checked) {
        const group = $$(`[name="${e.target.name}"]`, form);
        if (e.target.hasAttribute('data-exclusive')) group.forEach(b => { if (b !== e.target) b.checked = false; });
        else group.forEach(b => { if (b.hasAttribute('data-exclusive')) b.checked = false; });
      }
      if (e.target && (e.target.name === 'move_in' || e.target.name === 'move_out')) renderBudget();
      if (e.target && /^person_\d+_(doing|institution)$/.test(e.target.name || '')) syncPeople();
      if (e.target && /^person_\d+_(sleep|weekly_max)$/.test(e.target.name || '')) syncSleep();
      applyReveals(); refreshDerived();
      const scr = current();
      // a changed gate may hide the current screen (e.g. switching to "complete"); keep the index on a visible screen
      if (scr && !scrVisible(scr)) go(Math.min(idx, visibleScreens().length - 1), { silent: true });
      if (e.target && e.target.closest('.has-error')) { const f = e.target.closest('.has-error'); f.classList.remove('has-error'); $$('.field-error', f).forEach(x => x.remove()); }
      renderSteps(); renderMeter();
    });
    form.addEventListener('input', e => { if (e.target && /^person_\d+_weekly_max$/.test(e.target.name || '')) syncSleep(); if (e.target && e.target.closest('.has-error')) { const f = e.target.closest('.has-error'); f.classList.remove('has-error'); $$('.field-error', f).forEach(x => x.remove()); } });
    form.addEventListener('submit', e => e.preventDefault());
    form.addEventListener('keydown', e => { if (e.key === 'Enter' && e.target && e.target.tagName !== 'TEXTAREA' && e.target.type !== 'button') { e.preventDefault(); if (!btnNext.hidden) next(); } });
    root.addEventListener('click', e => {
      const b = e.target.closest('[data-action], [data-change-step], [data-goto-step]');
      if (!b) return;
      if (b.dataset.changeStep) { if (guardTerminal()) return; editing = true; go(firstIndexOfStep(b.dataset.changeStep)); return; }
      if (b.dataset.gotoStep) { const target = firstIndexOfStep(b.dataset.gotoStep); if (target <= idx || reviewedOnce) { editing = reviewedOnce && !steps().find(s => s.dataset.step === b.dataset.gotoStep).hasAttribute('data-review'); go(target); } return; }
      switch (b.dataset.action) {
        case 'back': go(idx - 1); break;
        case 'next': next(); break;
        case 'toReview': toReview(); break;
        case 'submit': submit(); break;
        case 'restart': restart(); break;
        case 'copyEmail': copyEmail(); break;
        case 'reloadData': $$('[data-role=dataError]', form).forEach(err => { err.hidden = true; }); loadData(); break;
        case 'changeTarget': case 'changeHelp': { const vs = visibleScreens(); const i = vs.findIndex(s => s.dataset.scr === (b.dataset.action === 'changeTarget' ? 'counts' : 'want_help')); go(i < 0 ? 0 : i); break; }
        case 'changeHeadcount': { const vs = visibleScreens(); const i = vs.findIndex(s => s.dataset.scr === 'headcount'); go(i < 0 ? 0 : i); break; }
        default: break;
      }
    });

    function preset(name, val) {
      const el = $$(`[name="${name}"]`, form).find(e => e.value === val);
      if (!el) return false;
      el.checked = true; if (name === 'need_more') clearBranch(val); applyReveals(); refreshDerived();
      const vs = visibleScreens();
      go(Math.min(1, vs.length - 1), { silent: true });
      return true;
    }
    applyReveals(); renderBudget(); go(0, { silent: true }); loadData();
    const api = { go, restart, counts, value, preset, payload, apiOk: API.ok };
    root.homebotForm = api;   // browser QA hook (no data; the same object the caller gets)
    return api;
  };
})();
