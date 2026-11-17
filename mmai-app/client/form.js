/* Enquiry form component (question set v0.8). One component for all three channels (I4).
   Public and emailed forms run in "card mode": one screen at a time, swipe cards for lifestyle questions, a progress meter,
   MateBot tips, save-and-finish-later and the "It's a match" screen. Staff phone entry keeps the plain section layout. */
(() => {
  // HTML-escape anything that came from a person (shared with staff.js through the API client)
  const esc = window.MMAI_API.esc;
  const phoneCodes = [
    ['+61', 'Australia'], ['+64', 'New Zealand'], ['+46', 'Sweden'], ['+47', 'Norway'], ['+45', 'Denmark'], ['+358', 'Finland'],
    ['+44', 'United Kingdom'], ['+353', 'Ireland'], ['+49', 'Germany'], ['+33', 'France'], ['+31', 'Netherlands'], ['+32', 'Belgium'],
    ['+41', 'Switzerland'], ['+43', 'Austria'], ['+39', 'Italy'], ['+34', 'Spain'], ['+351', 'Portugal'], ['+30', 'Greece'],
    ['+1', 'USA / Canada'], ['+52', 'Mexico'], ['+55', 'Brazil'], ['+57', 'Colombia'], ['+56', 'Chile'], ['+54', 'Argentina'],
    ['+86', 'China'], ['+852', 'Hong Kong'], ['+886', 'Taiwan'], ['+81', 'Japan'], ['+82', 'South Korea'], ['+84', 'Vietnam'],
    ['+66', 'Thailand'], ['+62', 'Indonesia'], ['+60', 'Malaysia'], ['+65', 'Singapore'], ['+63', 'Philippines'],
    ['+91', 'India'], ['+92', 'Pakistan'], ['+977', 'Nepal'], ['+880', 'Bangladesh'], ['+94', 'Sri Lanka'],
    ['+971', 'United Arab Emirates'], ['+966', 'Saudi Arabia'], ['+90', 'Turkey'], ['+27', 'South Africa'], ['other', 'Other']
  ];
  const languages = ['English', 'Mandarin', 'Cantonese', 'Vietnamese', 'Hindi', 'Punjabi', 'Nepali', 'Urdu', 'Bengali', 'Sinhala', 'Tamil',
    'Korean', 'Japanese', 'Thai', 'Indonesian', 'Malay', 'Filipino', 'Arabic', 'Turkish', 'Spanish', 'Portuguese', 'French', 'German',
    'Italian', 'Greek', 'Dutch', 'Swedish', 'Norwegian', 'Danish', 'Finnish', 'Other'];

  const iso = d => d.toISOString().slice(0, 10);
  const opt = (v, t = v) => { const o = document.createElement('option'); o.value = v; o.textContent = t; return o; };
  // I14(c) / A6(b) (mirrors server rules.rate_kind): 6-month price guide when move-in is January/February and move-out is in
  // July, whichever year. Months are read from the ISO parts (YYYY-MM-DD) of the date inputs, never via Date, so the guide
  // cannot shift with the visitor's time zone.
  const ymdParts = v => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || '')); return m ? { y: +m[1], m: +m[2], d: +m[3] } : null; };
  const guideKind = (mi, mo) => { const a = ymdParts(mi), b = ymdParts(mo); if (!a || !b) return '12m'; return ((a.m === 1 || a.m === 2) && b.m === 7) ? '6m' : '12m'; };
  const reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const BRAND = ['#ff6b4a', '#f6c544', '#2bb5a0', '#4a8fe7', '#2b1d2e'];
  // Light celebration (I19(h)): a handful of paper pieces, under a second, skipped when the visitor prefers reduced motion.
  function celebrate(anchor, big) {
    if (reducedMotion() || !anchor) return;
    const box = document.createElement('div'); box.className = 'confetti'; box.setAttribute('aria-hidden', 'true');
    const n = big ? 26 : 14;
    for (let i = 0; i < n; i++) {
      const p = document.createElement('i');
      p.style.setProperty('--x', (Math.random() * 2 - 1).toFixed(2)); p.style.setProperty('--r', (Math.random() * 720 - 360).toFixed(0) + 'deg');
      p.style.setProperty('--d', (0.7 + Math.random() * 0.5).toFixed(2) + 's'); p.style.background = BRAND[i % BRAND.length];
      p.style.left = (50 + (Math.random() * 30 - 15)).toFixed(1) + '%'; if (i % 3 === 0) p.style.borderRadius = '50%';
      box.append(p);
    }
    anchor.append(box); setTimeout(() => box.remove(), 1400);
  }
  const copyText = async (text, note) => { try { await navigator.clipboard.writeText(text); note.textContent = 'Copied.'; } catch { note.textContent = 'Select the link and copy it.'; } setTimeout(() => { note.textContent = ''; }, 2500); };
  // Links the form hands out (save-and-finish-later, Bring a mate). Inside the ALM website iframe (I20(g)) the page address is
  // the website's, so the server's MMAI_PUBLIC_BASE_URL (public app address) wins when it is set.
  let publicBaseUrl = null;
  const pageBase = () => publicBaseUrl ? publicBaseUrl + '/' : location.origin + location.pathname;

  /**
   * mountForm(host, { mode: 'public'|'staff', meta, emailToken, emailLink, mateToken, mateLink, isTest, draft, staffList,
   *                   onSubmit(answers) -> Promise<result>, onSaveDraft(answers, step, token) -> Promise<{token}> })
   */
  window.mountForm = function mountForm(host, cfg) {
    const tpl = document.getElementById('tpl-form');
    host.innerHTML = '';
    host.append(tpl.content.cloneNode(true));
    const root = host.querySelector('.form-root');
    const $ = (s, r = root) => r.querySelector(s);
    const $$ = (s, r = root) => [...r.querySelectorAll(s)];
    const R = name => $(`[data-role="${name}"]`);
    const form = R('form');
    const meta = cfg.meta;
    publicBaseUrl = (meta && meta.public_base_url) || null;
    const staffMode = cfg.mode === 'staff';
    const cardMode = !staffMode;  // I19(e): one question per screen for enquirers; staff keep the plain layout (no mascot)
    root.classList.toggle('card-mode', cardMode);

    // ---------- Build options ----------
    const pc = form.elements.phone_code;
    phoneCodes.forEach(([c, n]) => pc.append(opt(c, c === 'other' ? 'Other' : `${c}  ${n}`)));
    const chipGroup = (el, name, list) => list.forEach(v => { const l = document.createElement('label'); l.className = 'chip'; l.innerHTML = `<input type="checkbox" name="${name}" value="${v}">${v}`; el.append(l); });
    chipGroup(R('languages'), 'languages', languages);
    // GP12: a party (one form, a share link, or both) must always be smaller than the apartment, so MateBot AI can add at least
    // one housemate from outside it. The 3.9 list stops one short of the beds in ALM's largest available apartment.
    if (meta.mate_limit) {
      const ps = form.elements.party_size;
      [...ps.options].forEach(o => { if (o.value && +o.value > meta.mate_limit) o.remove(); });
      const note = R('partySizeNote'); if (note) { note.textContent = `Up to ${meta.mate_limit} people can enquire together. Every group needs at least one housemate from outside your own party; bigger groups, please call us.`; note.hidden = false; }
    }
    const school = form.elements.school;
    school.append(opt('', 'Select your institution…'));
    const groups = {};
    meta.institutions.forEach(i => { if (i.name === 'Other') return; (groups[i.group] = groups[i.group] || []).push(i); });
    Object.entries(groups).forEach(([g, list]) => { const og = document.createElement('optgroup'); og.label = g; list.forEach(i => og.append(opt(i.name))); school.append(og); });
    school.append(opt('Other', 'Other (enter name below)'));  // I9: Other last
    const budget = form.elements.budget_option;
    let currentGuide = null;
    function buildGuide(kind) {
      if (kind === currentGuide) return;
      const prev = budget.value; currentGuide = kind;
      budget.innerHTML = ''; budget.append(opt('', 'Choose the most you can pay…'));
      const byType = {};
      meta.price_guide[kind].forEach(o => (byType[o.type_label] = byType[o.type_label] || []).push(o));
      Object.entries(byType).forEach(([t, list]) => { const og = document.createElement('optgroup'); og.label = t; list.forEach(o => og.append(opt(o.id, `${o.occupancy_label}: $${o.min} to $${o.max} per week`))); budget.append(og); });
      if ([...budget.options].some(o => o.value === prev)) budget.value = prev;
      const note = R('guideNote');
      note.hidden = kind !== '6m';
      note.textContent = kind === '6m' ? 'Your dates (January or February to July) qualify for our 6-month rates, so the 6-month price guide is shown.' : '';
    }
    buildGuide('12m');
    const today = new Date();
    form.elements.move_in.min = iso(today);
    if (staffMode) {
      $$('.staff-step').forEach(s => s.hidden = false);
      form.elements.enq_date.value = today.toLocaleDateString('en-AU', { day: 'numeric', month: 'long', year: 'numeric' });
      const sm = R('staffMember'); sm.append(opt('', 'Select…')); (cfg.staffList || []).forEach(s => sm.append(opt(s))); if (cfg.staffName) sm.value = cfg.staffName;
      const b = R('banner'); b.hidden = false; b.className = 'form-banner form-banner--staff'; b.textContent = 'Staff phone entry: complete the enquirer\'s answers with them on the phone. Channel is recorded as "Phone".';
      // I19(m)/I19(p): the staff phone form ends in a summary. The public review block (grouped answers + Change links) is moved
      // into the staff summary section so both summaries are the same code; the section keeps the plain staff layout.
      const host = R('staffReviewHost'), review = R('review');
      if (host && review) { host.append(review); $('.review__title', review).textContent = 'Check the answers with the enquirer'; $('.review__intro', review).innerHTML = 'Read the answers back. Use <b>Change</b> to edit one, then <b>Back to check your answers</b> to return here. When everything is right, press <b>Save enquiry</b>.'; }
      if (cfg.serializerOnly) { $$('.staff-review', form).forEach(x => x.remove()); }  // off-screen serialiser: no summary/email controls
    } else if (cfg.emailToken) {
      const b = R('banner'); b.hidden = false;
      b.textContent = cfg.emailLink && cfg.emailLink.valid ? `This form was emailed to you by Apartment Living Melbourne${cfg.emailLink.recipient_name ? ', ' + cfg.emailLink.recipient_name : ''}. Your enquiry will be linked to that email.` : 'This emailed-form link was not recognised. You can still submit: your enquiry will be recorded from the landing page.';
      if (cfg.emailLink && cfg.emailLink.recipient_email) form.elements.email.value = cfg.emailLink.recipient_email;
      if (cfg.emailLink && cfg.emailLink.recipient_name) form.elements.first_name.value = cfg.emailLink.recipient_name.split(' ')[0];
    }
    // Bring a mate: a friend arriving through an enquirer's share link
    const mate = cfg.mateToken ? (cfg.mateLink || { valid: false, reason: 'This link was not recognised.' }) : null;
    if (mate) {
      const b = R('banner'); b.hidden = false; b.className = 'form-banner form-banner--mate';
      // names and server text are escaped: they come from another person's form (security review H1)
      b.innerHTML = mate.valid
        ? `<b>${esc(mate.enquirer_first_name)} wants you as a housemate.</b> Fill in your own form and MateBot AI keeps you in the same group as ${esc(mate.enquirer_first_name)}. ${mate.room_left > 0 ? `Room for ${esc(mate.room_left)} more ${mate.room_left === 1 ? 'person' : 'people'} on this link.` : `This link is already full (${esc(mate.limit)} people), so you can't join through it; please send your own enquiry instead.`}`
        : `${esc(mate.reason)} You can still send your own enquiry and we will match you in the usual way.`;
    }
    // D3 (9 Nov 2026): the TEST context of the whole page (URL ?test=1, a TEST saved form or HomeBot hand-over, a TEST enquirer's
    // share link) is decided once by app.js, which shows the TEST DATA badges and always posts is_test=true for it. The visible box
    // must say the same at every moment, including after "Start again": form.reset() returns every control to its DEFAULT state, so a
    // box ticked only in script was silently cleared while the badges and the posted flag stayed TEST. The default is set as well
    // and the context is re-applied after each reset. Staff mode has no page context (cfg.isTest is never set there): the box stays
    // the staff member's own explicit choice and Start again clears it like every other answer. Real mode is unchanged.
    const applyTestContext = () => { const t = form.elements.is_test; if (!t || !cfg.isTest) return; t.checked = true; t.defaultChecked = true; };
    applyTestContext();

    // ---------- Conditional logic ----------
    const valuesOf = name => { const els = $$(`[name="${name}"]`, form); if (!els.length) return []; const t = els[0].type; if (t === 'radio' || t === 'checkbox') return els.filter(e => e.checked).map(e => e.value); if (els[0].multiple) return [...els[0].selectedOptions].map(o => o.value); return els[0].value ? [els[0].value] : []; };
    const cond = expr => { const i = expr.indexOf('='); const n = expr.slice(0, i), opts = expr.slice(i + 1).split('|'); if (opts.includes('*')) return valuesOf(n).length > 0; return valuesOf(n).some(x => opts.includes(x)); };

    const countryIndex = new Map(meta.countries.map(c => [c.toLowerCase(), c]));
    const aliases = { uk: 'United Kingdom', england: 'United Kingdom', scotland: 'United Kingdom', wales: 'United Kingdom', britain: 'United Kingdom', usa: 'United States', us: 'United States', america: 'United States', holland: 'Netherlands', korea: 'South Korea', uae: 'United Arab Emirates', 'viet nam': 'Vietnam', prc: 'China', 'hong kong sar': 'Hong Kong' };
    const recogniseCountry = raw => { const v = (raw || '').trim().toLowerCase(); if (!v) return null; return countryIndex.get(v) || aliases[v] || [...countryIndex.values()].find(c => c.toLowerCase().startsWith(v) && v.length >= 4) || null; };
    const setHint = (h, raw) => { if (!(raw || '').trim()) { h.textContent = ''; h.className = 'country-hint'; return; } const hit = recogniseCountry(raw); if (hit) { h.textContent = `Recognised as ${hit}`; h.className = 'country-hint ok'; } else { h.textContent = 'We\'ll confirm this country with you.'; h.className = 'country-hint warn'; } };
    function countryHint(inputName, hintRole) { setHint(R(hintRole), form.elements[inputName].value); }

    // 3.9 / I16(a)(b): one row per other person: name, email and country of birth (text, I15), plus that person's OWN optional
    // introduction-only sex / current age (Bill, 8 Nov 2026: shared in introductions, never used for matching; never copied from
    // the enquirer). Date of birth is not asked; the 6.4 declaration covers every occupant being 18+ at move-in (I11(d)).
    const PM_FIELDS = ['name', 'email', 'birth_country_raw', 'intro_sex', 'intro_sex_other', 'intro_age'];
    const INTRO_SEX_OPTIONS = ['Female', 'Male', 'Another term', 'Prefer not to say'];
    const readPartyRows = () => $$('.party-row').map(r => Object.fromEntries(PM_FIELDS.map(k => [k, (r.querySelector(`[data-pm="${k}"]`) || {}).value || ''])));
    function renderPartyRows() {
      const party = valuesOf('party')[0];
      const wrap = R('partyRows');
      let n = party === 'Couple sharing one bed' ? 2 : (party === 'With friends' ? +form.elements.party_size.value || 0 : 0);
      const sizeSel = form.elements.party_size;
      if (party === 'Couple sharing one bed') { sizeSel.value = '2'; sizeSel.disabled = true; } else sizeSel.disabled = false;
      const want = Math.max(0, n - 1);
      const rowsNow = $$('.party-row', wrap);
      if (rowsNow.length !== want) {
        const existing = readPartyRows();
        wrap.innerHTML = '';
        for (let i = 0; i < want; i++) {
          const row = document.createElement('div'); row.className = 'party-row field'; row.dataset.index = i;
          const who = `Person ${i + 2}${party === 'Couple sharing one bed' ? ' (your partner)' : ''}`;
          row.innerHTML = `<span class="party-row__title">${who}</span>
            <label class="party-cell"><small>Full name</small><input data-pm="name" required aria-label="${who} name" autocomplete="off"></label>
            <label class="party-cell"><small>Email</small><input data-pm="email" type="email" required aria-label="${who} email" autocomplete="off"></label>
            <label class="party-cell"><small>Country of birth</small><input data-pm="birth_country_raw" required autocomplete="off" placeholder="Type their country" aria-label="${who} country of birth"><span class="country-hint" data-pm="hint"></span></label>
            <span class="party-cell party-cell--intro small muted">Optional, for introductions only (not used for matching):</span>
            <label class="party-cell party-cell--sex"><small>Sex (optional)</small><select data-pm="intro_sex" aria-label="${who} sex, for introductions only"><option value="">Prefer to leave blank</option>${INTRO_SEX_OPTIONS.map(o => `<option>${o}</option>`).join('')}</select></label>
            <label class="party-cell party-cell--other"><small>The term they use</small><input data-pm="intro_sex_other" maxlength="40" autocomplete="off" aria-label="${who} term used"></label>
            <label class="party-cell"><small>Current age (optional)</small><input data-pm="intro_age" type="number" inputmode="numeric" min="0" max="999" step="1" placeholder="Years" autocomplete="off" aria-label="${who} current age, for introductions only"></label>`;
          if (existing[i]) PM_FIELDS.forEach(k => { const el = row.querySelector(`[data-pm="${k}"]`); if (el) el.value = existing[i][k] || ''; });
          wrap.append(row);
        }
      }
      $$('.party-row', wrap).forEach(row => setHint(row.querySelector('[data-pm="hint"]'), row.querySelector('[data-pm="birth_country_raw"]').value));  // I16(b): normalised like 1.8
      $$('.party-row', wrap).forEach(syncMemberIntro);
      renderCoupleRows();
    }

    // the "term they use" box only applies (and is only required) when that person chose "Another term"
    function syncMemberIntro(row) {
      const sel = row.querySelector('[data-pm="intro_sex"]'); const other = row.querySelector('.party-cell--other'); if (!sel || !other) return;
      const on = sel.value === 'Another term'; other.classList.toggle('is-shown', on); const inp = other.querySelector('input'); inp.required = on; if (!on) inp.value = '';
    }

    // 3.10 / I16(f): couples within a "With friends" group, stated by the enquirer. Each row = two people from the party.
    let coupleCount = 1;
    const coupleRefs = () => { const rows = readPartyRows(); const first = (form.elements.first_name.value || '').trim(); const out = [['lead', first ? `${first} (you)` : 'You (the enquirer)']]; rows.forEach((m, i) => out.push([`m${i + 1}`, (m.name || '').trim() || `Person ${i + 2}`])); return out; };
    const readCouples = () => $$('.couple-row').map(r => [r.querySelector('[data-cp="a"]').value, r.querySelector('[data-cp="b"]').value]);
    function renderCoupleRows() {
      const wrap = R('coupleRows'); if (!wrap) return;
      const isFriends = valuesOf('party')[0] === 'With friends';
      const refs = coupleRefs();
      const maxCouples = Math.max(1, Math.floor(refs.length / 2));
      coupleCount = Math.min(Math.max(1, coupleCount), maxCouples);
      const prev = readCouples();
      if (!isFriends) { wrap.innerHTML = ''; return; }
      wrap.innerHTML = '';
      for (let i = 0; i < coupleCount; i++) {
        const row = document.createElement('div'); row.className = 'couple-row field'; row.dataset.index = i;
        const sel = (k, lbl) => `<label class="party-cell"><small>${lbl}</small><select data-cp="${k}" aria-label="Couple ${i + 1} ${lbl}"><option value="">Select…</option>${refs.map(([v, t]) => `<option value="${v}">${t.replace(/</g, '&lt;')}</option>`).join('')}</select></label>`;
        row.innerHTML = `<span class="party-row__title">Couple ${i + 1}</span>${sel('a', 'First person')}${sel('b', 'Second person')}${coupleCount > 1 ? `<button class="btn btn--ghost btn--sm couple-row__remove" type="button" data-remove-couple="${i}" aria-label="Remove couple ${i + 1}">Remove</button>` : ''}`;
        if (prev[i]) { row.querySelector('[data-cp="a"]').value = prev[i][0] || ''; row.querySelector('[data-cp="b"]').value = prev[i][1] || ''; }
        wrap.append(row);
      }
      const add = R('addCouple'); if (add) add.hidden = coupleCount >= maxCouples;
    }
    const addBtn = R('addCouple'); if (addBtn) addBtn.addEventListener('click', () => { coupleCount++; renderCoupleRows(); });
    root.addEventListener('click', e => { const b = e.target.closest('[data-remove-couple]'); if (!b) return; const rows = readCouples(); rows.splice(+b.dataset.removeCouple, 1); coupleCount = Math.max(1, coupleCount - 1); renderCoupleRows(); $$('.couple-row').forEach((r, i) => { if (rows[i]) { r.querySelector('[data-cp="a"]').value = rows[i][0]; r.querySelector('[data-cp="b"]').value = rows[i][1]; } }); });

    // 3.12: "Which three matter most to you?" offers only the features ticked in 3.11 (up to three; an unticked feature drops out)
    const FEATURES_MAX_TOP = (meta && meta.features_max_top) || 3;
    function renderFeaturesTop() {
      const box = R('featuresTop'); if (!box) return;
      const wanted = valuesOf('features'); const keep = valuesOf('features_top').filter(v => wanted.includes(v));
      box.innerHTML = '';
      wanted.forEach(v => { const l = document.createElement('label'); l.className = 'chip'; const i = document.createElement('input'); i.type = 'checkbox'; i.name = 'features_top'; i.value = v; i.checked = keep.includes(v); l.append(i, document.createTextNode(v)); box.append(l); });
      const full = keep.length >= FEATURES_MAX_TOP;
      $$('input[name="features_top"]', box).forEach(i => { i.disabled = full && !i.checked; i.closest('.chip').classList.toggle('is-disabled', i.disabled); });
      const h = R('featuresTopHint'); if (h) h.textContent = full ? `That's your three. Untick one to swap it.` : (keep.length ? `${FEATURES_MAX_TOP - keep.length} more if you like.` : `Pick up to ${FEATURES_MAX_TOP} from the features you ticked, or skip this.`);
    }
    function applyConditions() {
      renderFeaturesTop();
      $$('[data-show-when]').forEach(el => el.classList.toggle('is-shown', cond(el.dataset.showWhen)));
      $$('[data-req-when]').forEach(el => { el.required = cond(el.dataset.reqWhen); });
      // 3.7 first choice from 3.5 ticks
      const rf = form.elements.room_first; const picked = valuesOf('rooms'); const prev = rf.value; rf.innerHTML = '';
      if (!picked.length) rf.append(opt('', 'Tick at least one room arrangement above'));
      else { rf.append(opt('', 'Select…')); picked.forEach(p => rf.append(opt(p))); if (picked.includes(prev)) rf.value = prev; }
      // stay length + guide switch (I14(c))
      const a = form.elements.move_in.value, b = form.elements.move_out.value, sl = R('stayLength');
      form.elements.move_out.min = a || iso(today);
      if (a && b && b > a) {
        const days = (new Date(b) - new Date(a)) / 864e5; const months = Math.floor(days / 30.44), weeks = Math.round((days - months * 30.44) / 7);
        sl.textContent = `Length of stay: about ${months ? months + ' month' + (months > 1 ? 's' : '') : ''}${months && weeks ? ' and ' : ''}${weeks ? weeks + ' week' + (weeks > 1 ? 's' : '') : ''}`;
      } else sl.textContent = '';
      buildGuide(guideKind(a, b));
      R('introCount').textContent = form.elements.intro.value.length;
      renderPartyRows();
      countryHint('birth_country_raw', 'hint-birth'); countryHint('live_country_raw', 'hint-live');
    }
    form.addEventListener('input', applyConditions);
    form.addEventListener('change', applyConditions);
    form.addEventListener('change', e => { const row = e.target && e.target.matches && e.target.matches('[data-pm="intro_sex"]') && e.target.closest('.party-row'); if (row) syncMemberIntro(row); });
    // 6.4 / I11(d): the declaration that everyone will be 18+ at move-in is made for a particular group and move-in date. If the
    // enquirer's identity, the move-in date or the group changes after the box was ticked (e.g. a Change from the summary), the
    // tick is cleared and must be given again. Acceptance is never carried over silently, and the server checks the same thing.
    const ADULTS_SCOPE = ['first_name', 'last_name', 'email', 'move_in', 'move_in_flex', 'party', 'party_size'];
    form.addEventListener('change', e => {
      const t = e.target, box = form.elements.adults_confirm; if (!t || !box || t === box) return;
      if (t.dataset && t.dataset.pm && t.dataset.pm.startsWith('intro_')) return;   // introduction-only details are not part of what the declaration covers (who the people are)
      if (!(ADULTS_SCOPE.includes(t.name) || t.closest('.party-row'))) return;
      if (box.checked) { box.checked = false; const note = R('adultsRenew'); if (note) note.hidden = false; }
    });
    if (form.elements.adults_confirm) form.elements.adults_confirm.addEventListener('change', () => { const note = R('adultsRenew'); if (note && form.elements.adults_confirm.checked) note.hidden = true; });
    const clearOne = e => { const f = e.target.closest('.has-error'); if (f) { f.classList.remove('has-error'); f.querySelectorAll('.field-error').forEach(x => x.remove()); } };
    form.addEventListener('input', clearOne); form.addEventListener('change', clearOne);

    // ---------- Steps and screens ----------
    // Staff mode: one unit = one section. Card mode: one unit = one screen (.scr) inside a section.
    let current = 0; const done = new Set(); let furthest = 0;
    const steps = () => $$('.step', form).filter(s => !s.classList.contains('staff-step') || staffMode);
    const units = () => cardMode ? $$('.step:not(.staff-step) .scr', form) : steps();
    const stepOf = u => cardMode ? u.closest('.step') : u;
    function renderSteps() {
      const list = R('stepList'); list.innerHTML = '';
      const all = steps(); const curStep = stepOf(units()[current]);
      all.forEach((s, i) => {
        const li = document.createElement('li'); const b = document.createElement('button'); b.type = 'button'; b.textContent = `${i + 1}. ${s.dataset.title}`;
        if (s === curStep) b.setAttribute('aria-current', 'step'); if (done.has(s.dataset.step)) b.classList.add('is-done');
        const firstUnit = cardMode ? units().findIndex(u => stepOf(u) === s) : i;
        b.disabled = !(firstUnit <= Math.max(current, furthest) || done.has(s.dataset.step) || done.has(all[i - 1]?.dataset.step));
        b.addEventListener('click', () => go(firstUnit)); li.append(b); list.append(li);
      });
    }
    function renderMeter() {
      const m = R('meter'); if (!cardMode) { m.hidden = true; return; }
      m.hidden = false;
      const pct = Math.min(99, Math.round(current / units().length * 100));
      R('meterLabel').textContent = `You're ${pct}% matched`;
      R('meterFill').style.width = pct + '%'; R('meterBar').setAttribute('aria-valuenow', pct);
    }
    // I20(c): MateBot stands beside every question and makes a short comment on each one (the staff form has no mascot, T10)
    function renderTip(u) {
      const w = R('tipWrap'); if (!cardMode || !u || !u.dataset.tip || form.hidden) { w.hidden = true; return; }
      const [pose, text] = u.dataset.tip.split('|');
      const img = R('tipImg'), bubble = R('tipText');
      const src = `img/matebot_${pose}.webp?v=20261106c`; if (img.getAttribute('src') !== src) img.src = src;  // release tag: same-day sessions fetch the P8(a) artwork
      if (bubble.textContent !== text) { bubble.textContent = text; bubble.classList.remove('is-speaking'); void bubble.offsetWidth; bubble.classList.add('is-speaking'); }
      w.hidden = false;
    }
    // ---------- I19(k): Check your answers (card mode) ----------
    // Built from the live form controls, so it always says exactly what collect() will send: labels come from the question
    // text on each screen, values from the chosen option text. Hidden or not-applicable questions are left out.
    const reviewScr = () => $('.scr[data-scr="review"]', form);
    const reviewUnit = () => cardMode ? reviewScr() : $('.step.staff-review', form);   // I19(m): staff summary is a section
    const reviewIndex = () => units().indexOf(reviewUnit());
    const unitOf = el => el ? (cardMode ? el.closest('.scr') : el.closest('.step')) : null;
    let fromReview = false;
    let hasReviewed = false;
    const fmtDate = v => { if (!v) return ''; const d = new Date(v + 'T00:00:00'); return isNaN(d) ? v : d.toLocaleDateString('en-AU', { day: 'numeric', month: 'long', year: 'numeric' }); };
    const labelText = el => { if (!el) return ''; const c = el.cloneNode(true); $$('input, select, textarea, .hint-inline, .opt-tag, small, .pills, .chips, .options, .reveal, .sublabel, .phone, .hint, p', c).forEach(x => x.remove()); return c.textContent.replace(/\s+/g, ' ').trim(); };
    function reviewRows(step) {
      const rows = []; const seen = new Set();
      let answerScreen = null;
      const add = (label, value) => { if (label && value != null && value !== '' && !(Array.isArray(value) && !value.length)) rows.push([label, value, answerScreen]); };
      $$('input, select, textarea', step).filter(el => shown(el) && el.name && !el.closest('.hp') && !el.closest('.party-row') && !el.closest('.couple-row')).forEach(el => {
        const name = el.name; if (seen.has(name)) return; seen.add(name);
        const field = el.closest('.field') || el.closest('.consent');
        if (!field) return;
        answerScreen = unitOf(el);
        if (name === 'phone_code') { const n = form.elements.phone.value.trim(); add('Mobile number', n ? `${form.elements.phone_code.value === 'other' ? '' : form.elements.phone_code.value + ' '}${n}`.trim() : ''); seen.add('phone'); return; }
        const swipeLabel = v => { const b = field.querySelector(`.swipe [data-value="${CSS.escape(v)}"]`); return b ? b.textContent.replace(/[←→]/g, '').replace(/\s+/g, ' ').trim() : null; };
        if (el.type === 'radio') {
          const picked = $$(`input[name="${name}"]`, form).find(r => r.checked); if (!picked) return;
          add(labelText(field.querySelector('legend') || field.querySelector('label:not(.pill)') || field), swipeLabel(picked.value) || (picked.closest('.pill, .opt') || picked.parentElement).textContent.replace(/\s+/g, ' ').trim());
          return;
        }
        if (el.type === 'checkbox') {
          if (el.closest('.consent')) { add(el.closest('.consent').querySelector('span').textContent.replace(/\s+/g, ' ').replace(/Optional$/, '').trim(), el.checked ? 'Yes' : 'Not ticked'); return; }
          const picked = $$(`input[name="${name}"]:checked`, form).map(c => { const o = c.closest('.opt'); return o ? o.querySelector('b').textContent.trim() : c.parentElement.textContent.trim(); });
          const legend = field.querySelector('legend') || field;
          add(labelText(legend) || name, picked);
          return;
        }
        if (el.tagName === 'SELECT') {
          const lbl = labelText(el.closest('label')) || labelText(field.querySelector('legend')) || name;
          if (el.multiple) add(lbl, [...el.selectedOptions].map(o => o.textContent.trim()));
          else if (el.value) { const o = el.selectedOptions[0]; const og = o && o.parentElement.tagName === 'OPTGROUP' ? o.parentElement.label + ' – ' : ''; add(lbl, swipeLabel(el.value) || og + o.textContent.trim()); }
          return;
        }
        const lbl = labelText(el.closest('label')) || labelText(field.querySelector('label')) || name;
        add(lbl, el.type === 'date' ? fmtDate(el.value) : el.value.trim());
      });
      // other people in the party (3.9) and couples (3.10), only when those questions are showing
      if ($$('.party-row', step).some(shown)) readPartyRows().forEach((m, i) => { if (m.name || m.email) { const intro = [m.intro_sex === 'Another term' ? (m.intro_sex_other || '').trim() || 'Another term' : m.intro_sex, (m.intro_age || '').trim()].filter(Boolean).join(', '); rows.push([`Person ${i + 2}`, { main: m.name.trim() || '(no name yet)', sub: [m.email.trim(), m.birth_country_raw.trim(), intro && `for introductions only: ${intro}`].filter(Boolean).join(' · ') }, unitOf($('.party-row', step))]); } });
      if ($$('.couple-row', step).some(shown)) { const names = Object.fromEntries(coupleRefs()); const cps = readCouples().filter(p => p[0] && p[1]).map(p => `${names[p[0]] || p[0]} and ${names[p[1]] || p[1]}`); if (cps.length) rows.push(['Couples in your group', cps, unitOf($('.couple-row', step))]); }
      return rows;
    }
    function renderReview() {
      const wrap = R('reviewGroups'); if (!wrap) return;
      wrap.innerHTML = '';
      const all = units();
      steps().forEach((s, i) => {
        const rows = reviewRows(s); if (!rows.length) return;
        const g = document.createElement('section'); g.className = 'review__group';
        const head = document.createElement('div'); head.className = 'review__head';
        const h = document.createElement('h4'); h.textContent = `${i + 1}. ${s.dataset.title}`;
        head.append(h);
        const dl = document.createElement('dl'); dl.className = 'review__list';
        rows.forEach(([label, value, target]) => {
          const row = document.createElement('div'); row.className = 'review__row';
          const dt = document.createElement('dt'); dt.textContent = label; const dd = document.createElement('dd');
          if (Array.isArray(value)) { const ul = document.createElement('ul'); value.forEach(v => { const li = document.createElement('li'); li.textContent = v; ul.append(li); }); dd.append(ul); }
          else if (value && typeof value === 'object') { dd.textContent = value.main; const sub = document.createElement('span'); sub.className = 'review__sub'; sub.textContent = value.sub; dd.append(sub); }
          else dd.textContent = value;
          const ch = document.createElement('button'); ch.type = 'button'; ch.className = 'review__change'; ch.textContent = 'Change'; ch.setAttribute('aria-label', `Change ${label}`);
          ch.addEventListener('click', () => { fromReview = true; go(all.indexOf(target)); scrollTop(); });
          dd.append(ch);
          row.append(dt, dd); dl.append(row);
        });
        g.append(head, dl); wrap.append(g);
      });
    }
    function go(i) {
      const all = units(); current = Math.max(0, Math.min(i, all.length - 1)); furthest = Math.max(furthest, current);
      const u = all[current], s = stepOf(u);
      $$('.step', form).forEach(x => x.classList.remove('is-active')); s.classList.add('is-active');
      if (cardMode) { $$('.scr', form).forEach(x => x.classList.remove('is-active')); u.classList.add('is-active'); }
      R('back').classList.toggle('is-invisible', current === 0);
      const isReview = u === reviewUnit();
      if (isReview) hasReviewed = true;
      fromReview = hasReviewed && !isReview;
      // I19(m): the final public button reads "Submit"; while an answer is being changed from the summary the green
      // "Back to check your answers" button is the first navigation action and Next is hidden (one obvious way back).
      R('next').textContent = current === all.length - 1 ? (staffMode ? 'Save enquiry' : 'Submit') : 'Next';
      R('next').hidden = fromReview;
      const toReview = R('toReview'); if (toReview) toReview.hidden = !fromReview;
      const navEl = R('nav'); if (navEl) navEl.classList.toggle('is-editing', fromReview);
      R('stepCount').textContent = cardMode ? '' : `Step ${current + 1} of ${all.length}`; // I20(c): no screen count on the public form
      applyConditions(); renderSteps(); renderMeter(); renderTip(u);
      if (cardMode) { const sw = u.querySelector('.swipe'); if (sw) refreshSwipe(sw); }
      if (isReview) { fromReview = false; renderReview(); }
    }

    // ---------- Swipe cards (I19(a)) ----------
    // The real control (radio pills or a select) stays in the form, visually hidden; the card only sets its value, so the
    // answer saved is exactly what the plain form saves.
    function buildSwipes() {
      if (!cardMode) return;
      $$('[data-swipe]', form).forEach(field => {
        const [question, ...optSpecs] = field.dataset.swipe.split('|');
        const opts = optSpecs.map(o => { const i = o.indexOf('='); return i < 0 ? { value: o, label: o } : { value: o.slice(0, i), label: o.slice(i + 1) }; });
        const control = field.querySelector('select') || null; const radios = control ? [] : $$('input[type=radio]', field);
        const name = control ? control.name : radios[0].name;
        (field.querySelector('.pills') || control).classList.add('swipe-src');
        const legend = field.querySelector('legend, label'); if (legend) legend.classList.add('swipe-legend');
        const left = opts[0], right = opts[1], mid = opts[2];
        const sw = document.createElement('div'); sw.className = 'swipe'; sw.dataset.name = name;
        sw.innerHTML = `<div class="swipe__stage"><div class="swipe__card" tabindex="0" role="group" aria-label="${question.replace(/"/g, '&quot;')}">
            <span class="swipe__hint swipe__hint--l" aria-hidden="true">${left.label}</span><span class="swipe__hint swipe__hint--r" aria-hidden="true">${right.label}</span>
            <p class="swipe__q">${question}</p><p class="swipe__chosen" aria-live="polite"></p>
            <p class="swipe__help">Swipe left or right, or tap a button.</p></div></div>
          <div class="swipe__btns">
            <button type="button" class="btn btn--swipe" data-dir="left" data-value="${left.value.replace(/"/g, '&quot;')}"><span aria-hidden="true">←</span> ${left.label}</button>
            ${mid ? `<button type="button" class="btn btn--swipe btn--swipe-mid" data-dir="mid" data-value="${mid.value.replace(/"/g, '&quot;')}">${mid.label}</button>` : ''}
            <button type="button" class="btn btn--swipe" data-dir="right" data-value="${right.value.replace(/"/g, '&quot;')}">${right.label} <span aria-hidden="true">→</span></button>
          </div>`;
        field.append(sw);
        const card = sw.querySelector('.swipe__card');
        const setValue = (value, dir) => {
          if (control) control.value = value; else radios.forEach(r => { r.checked = r.value === value; });
          (control || radios[0]).dispatchEvent(new Event('change', { bubbles: true }));
          card.classList.remove('is-left', 'is-right', 'is-mid'); card.classList.add(dir === 'left' ? 'is-left' : dir === 'right' ? 'is-right' : 'is-mid');
          const ms = reducedMotion() ? 0 : 420;
          setTimeout(() => { refreshSwipe(sw); const all = units(); const idx = all.indexOf(field.closest('.scr')); if (!fromReview && idx === current && idx < all.length - 1) advance(); }, ms);
        };
        sw.addEventListener('click', e => { const b = e.target.closest('[data-value]'); if (b) setValue(b.dataset.value, b.dataset.dir); });
        // pointer swipe
        let startX = null, dx = 0;
        const onMove = e => { if (startX === null) return; dx = e.clientX - startX; card.style.transform = `translateX(${dx}px) rotate(${dx / 18}deg)`; card.classList.toggle('lean-l', dx < -30); card.classList.toggle('lean-r', dx > 30); };
        const onUp = () => { if (startX === null) return; const d = dx; startX = null; card.style.transform = ''; card.classList.remove('lean-l', 'lean-r', 'is-dragging'); if (d < -70) setValue(left.value, 'left'); else if (d > 70) setValue(right.value, 'right'); dx = 0; window.removeEventListener('pointermove', onMove); window.removeEventListener('pointerup', onUp); window.removeEventListener('pointercancel', onUp); };
        card.addEventListener('pointerdown', e => { if (e.button && e.button !== 0) return; startX = e.clientX; dx = 0; card.classList.add('is-dragging'); window.addEventListener('pointermove', onMove); window.addEventListener('pointerup', onUp); window.addEventListener('pointercancel', onUp); });
        card.addEventListener('keydown', e => { if (e.key === 'ArrowLeft') { e.preventDefault(); setValue(left.value, 'left'); } else if (e.key === 'ArrowRight') { e.preventDefault(); setValue(right.value, 'right'); } else if (mid && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) { e.preventDefault(); setValue(mid.value, 'mid'); } });
        refreshSwipe(sw);
      });
    }
    function refreshSwipe(sw) {
      const field = sw.closest('[data-swipe]'); const control = field.querySelector('select'); const radios = $$('input[type=radio]', field);
      const value = control ? control.value : (radios.find(r => r.checked) || {}).value || '';
      const card = sw.querySelector('.swipe__card'); card.classList.remove('is-left', 'is-right', 'is-mid'); card.classList.toggle('has-value', !!value);
      const chosenLabel = (() => { const b = $$('[data-value]', sw).find(b => b.dataset.value === value); return b ? b.textContent.replace(/[←→]/g, '').trim() : ''; })();
      sw.querySelector('.swipe__chosen').textContent = value ? `You chose: ${chosenLabel}` : '';
      $$('[data-value]', sw).forEach(b => b.classList.toggle('is-chosen', !!value && b.dataset.value === value));
    }

    // ---------- Validation ----------
    function clearErrors(scope) { $$('.has-error', scope).forEach(e => e.classList.remove('has-error')); $$('.field-error', scope).forEach(e => e.remove()); }
    function flag(el, msg) { const f = el.closest('.consent') || el.closest('.party-cell') || el.closest('.field') || el; if (f.classList.contains('has-error')) return; f.classList.add('has-error'); if (msg && !f.classList.contains('consent')) { const p = document.createElement('p'); p.className = 'field-error'; p.textContent = msg; f.append(p); } }
    const shown = el => !el.closest('.reveal:not(.is-shown)') && !el.closest('[hidden]');
    function validate(step) {
      // `step` is a section (staff) or one screen (card mode); cross-field checks run only when their fields are in scope
      clearErrors(step); let first = null; const mark = (el, m) => { flag(el, m); first = first || el; }; const has = el => !!el && step.contains(el);
      $$('input, select, textarea', step).filter(shown).forEach(el => {
        if (el.type === 'radio' || el.type === 'checkbox') return;
        if (el.required && !el.value.trim()) mark(el, 'Please complete this.');
        else if (el.type === 'email' && el.value && !el.checkValidity()) mark(el, 'Please enter a valid email.');
      });
      [...new Set($$('input[type=radio][required]', step).filter(shown).map(r => r.name))].forEach(n => { if (!valuesOf(n).length) mark($(`[name="${n}"]`, step), 'Please choose one.'); });
      $$('[data-required-group]', step).filter(shown).forEach(g => { if (!valuesOf(g.dataset.requiredGroup).length) mark(g, 'Please tick at least one.'); });
      $$('.consent input[required]', step).forEach(c => { if (!c.checked) mark(c); });
      if (has(form.elements.move_out) || has(form.elements.move_in)) {
        const a = form.elements.move_in.value, b = form.elements.move_out.value;
        if (a && b && b <= a && has(form.elements.move_out)) mark(form.elements.move_out, 'Move-out must be after move-in.');
      }
      if (has(form.elements.mate_max)) {
        const i = +form.elements.mate_ideal.value, m = +form.elements.mate_max.value;
        if (i && m && m < i) mark(form.elements.mate_max, 'This can\'t be fewer than your ideal number.');
      }
      if (has(form.elements.party_size)) {
        if (cond('party=With friends') && !form.elements.party_size.value) mark(form.elements.party_size, 'Please choose the number of people.');
        if (cond('party=With friends')) {  // 3.10 / I16(f)
          const ans = valuesOf('couples_answer')[0];
          if (!ans) mark($('[name="couples_answer"]', step), 'Please say whether anyone in your group is a couple.');
          else if (ans === 'Yes') {
            const used = new Set();
            $$('.couple-row', step).forEach(row => {
              const a = row.querySelector('[data-cp="a"]'), b = row.querySelector('[data-cp="b"]');
              if (!a.value || !b.value) mark(!a.value ? a : b, 'Please pick both people.');
              else if (a.value === b.value) mark(b, 'A couple is two different people.');
              else if (used.has(a.value) || used.has(b.value)) mark(a, 'Someone is listed in two couples.');
              used.add(a.value); used.add(b.value);
            });
          }
        }
      }
      if (first) { (first.closest('.field') || first).scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center' }); if (first.focus) first.focus({ preventScroll: true }); }
      return !first;
    }

    function collect() {
      const a = {};
      const multi = ['languages', 'apt_sizes', 'rooms', 'areas', 'travel_modes', 'features', 'features_top', 'age_pref'];
      new FormData(form).forEach((v, k) => { if (multi.includes(k)) { (a[k] = a[k] || []).push(v); } else if (v !== '') a[k] = v; });
      multi.forEach(k => { a[k] = a[k] || []; });
      if (a.languages.includes('Other')) {
        const extra = [];
        const sel = a.languages_other;
        if (Array.isArray(sel)) {
          sel.forEach(v => { if (v && v !== 'Other (type below)') extra.push(v); });
        } else if (sel && sel !== 'Other (type below)') {
          extra.push(sel);
        }
        if (a.languages_other_text) extra.push(...a.languages_other_text.split(/,\s*/).map(s => s.trim()).filter(Boolean));
        a.languages = a.languages.filter(l => l !== 'Other').concat(extra);
      }
      delete a.languages_other; delete a.languages_other_text;
      // 6.4 / I11(d): adults_confirm is a real boolean (never the checkbox's "on" string); the retired lead-only consent_age is not sent
      ['consent_store', 'consent_share', 'adults_confirm', 'consent_marketing', 'is_test'].forEach(k => { a[k] = !!form.elements[k] && form.elements[k].checked; });
      a.party_members = readPartyRows().map(m => { const pm = { name: m.name.trim(), email: m.email.trim(), birth_country_raw: m.birth_country_raw.trim() };
        // each person's own optional introduction-only details; blank = not given (never copied from the enquirer, never defaulted)
        if (m.intro_sex) pm.intro_sex = m.intro_sex; if (m.intro_sex === 'Another term' && m.intro_sex_other.trim()) pm.intro_sex_other = m.intro_sex_other.trim(); if (m.intro_age.trim() !== '') pm.intro_age = m.intro_age.trim(); return pm; });
      if (a.intro_sex !== 'Another term') delete a.intro_sex_other;
      if (a.party === 'Couple sharing one bed') a.party_size = 2;
      if (a.party === 'Alone') { a.party_size = 1; a.party_members = []; }
      // D1: the Staff-only section (7.x) is hidden from enquirers but its inputs still have DOM defaults (channel "Phone"); never send them from the public form
      if (!staffMode) ['channel', 'campaign', 'staff_member', 'staff_notes', 'enq_date'].forEach(k => { delete a[k]; });
      // 3.10 / I16(f): couples only as stated by the enquirer
      if (a.party === 'With friends') { a.couples = a.couples_answer === 'Yes' ? readCouples().filter(p => p[0] && p[1]) : []; }
      else { delete a.couples_answer; a.couples = []; }
      // 17 Nov 2026: age preference + avoid lists
      if (a.age_pref && a.age_pref.includes('No preference')) a.age_pref = ['No preference'];
      if (a.avoid_answer && a.avoid_answer.indexOf('happy to live with anyone') !== -1) {
        a.avoid_countries = []; a.avoid_regions = [];
      } else {
        const ct = (a.avoid_countries_text || '').split(/,\s*/).map(s => s.trim()).filter(Boolean);
        const rt = (a.avoid_regions_text || '').split(/,\s*/).map(s => s.trim()).filter(Boolean);
        a.avoid_countries = ct; a.avoid_regions = rt;
      }
      delete a.avoid_countries_text; delete a.avoid_regions_text;
      return a;
    }

    function showServerErrors(errors) {
      const all = units(); let firstStep = -1;
      Object.entries(errors).forEach(([k, msg]) => {
        let el = form.elements[k] || $(`[data-required-group="${k}"]`);
        if (!el && k.startsWith('couples.')) { const m = k.match(/^couples\.(\d+)$/); el = (m && $(`.couple-row[data-index="${m[1]}"] [data-cp="a"]`)) || R('coupleRows'); }
        if (!el && k === 'couples') el = R('coupleRows');
        if (!el && k.startsWith('party_members')) { const m = k.match(/^party_members\.(\d+)\.(\w+)$/); el = (m && $(`.party-row[data-index="${m[1]}"] [data-pm="${m[2]}"]`)) || R('partyRows'); }
        if (!el) return;
        const stepEl = (el.length && !el.tagName ? el[0] : el).closest(cardMode ? '.scr' : '.step'); const idx = all.indexOf(stepEl); if (idx >= 0 && (firstStep < 0 || idx < firstStep)) firstStep = idx;
        flag(el.length && !el.tagName ? el[0] : el, msg);
      });
      if (firstStep >= 0) go(firstStep);
      const se = R('submitError'); se.hidden = false; se.textContent = errors.mate_link ? errors.mate_link : 'Please check the highlighted answers.';
    }

    // ---------- Save and finish later (I19(f)) ----------
    let draftToken = cfg.draft && cfg.draft.token || null; let saving = null;
    async function saveDraft(show) {
      if (!cfg.onSaveDraft) return;
      if (saving) await saving;
      saving = (async () => {
        try {
          const res = await cfg.onSaveDraft(collect(), current, draftToken, { key: submitKey });  // the draft keeps the same submission key
          if (res && res.token) draftToken = res.token;
        } catch (e) { if (show) { const se = R('submitError'); se.hidden = false; se.textContent = 'Could not save right now. Please try again.'; } }
      })();
      await saving; saving = null;
      if (show && draftToken) {
        const link = `${pageBase()}#/resume?t=${encodeURIComponent(draftToken)}`;
        const box = R('saveBox'); R('saveLink').value = link;
        R('emailSave').href = `mailto:${encodeURIComponent(form.elements.email.value || '')}?subject=${encodeURIComponent('Your MateBot AI enquiry: finish later')}&body=${encodeURIComponent('Carry on with your Apartment Living Melbourne enquiry here: ' + link)}`;
        box.hidden = false; box.focus(); box.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center' });
      }
    }
    const saveBtn = R('saveLater'); if (saveBtn) saveBtn.addEventListener('click', () => saveDraft(true));
    R('copySave').addEventListener('click', () => copyText(R('saveLink').value, R('saveCopied')));
    R('saveClose').addEventListener('click', () => { R('saveBox').hidden = true; });

    // ---------- Submission reliability (31 Oct 2026) ----------
    // One opaque random key per enquiry attempt. The server keeps one enquiry per key, so a double click, a double Enter, a
    // reply lost on a bad connection, a reload while "Saving…" shows or a retry after an error can never make a second
    // enquiry. Only the key and its state are kept locally (sessionStorage; never any answer). The key is rotated only when
    // the person deliberately starts a new enquiry ("Start again"), never because an attempt's outcome is uncertain.
    const storeKey = cfg.storageKey ? 'matebot.submit.' + cfg.storageKey : null;
    const store = {
      read() { try { return storeKey && JSON.parse(sessionStorage.getItem(storeKey) || 'null'); } catch { return null; } },
      write(v) { try { if (storeKey) { if (v) sessionStorage.setItem(storeKey, JSON.stringify(v)); else sessionStorage.removeItem(storeKey); } } catch { /* storage unavailable (preview iframe): the in-page guards still apply */ } },
    };
    const newKey = () => { try { return crypto.randomUUID().replace(/-/g, ''); } catch { return Array.from({ length: 32 }, () => Math.floor(Math.random() * 36).toString(36)).join(''); } };
    const pendingLocal = store.read();
    let submitKey = (cfg.draft && cfg.draft.submit_key) || (pendingLocal && pendingLocal.s === 'pending' && pendingLocal.k) || newKey();
    if (!(pendingLocal && pendingLocal.s === 'pending')) store.write(null);
    let inFlight = false;   // an HTTP attempt is running right now
    let savedResult = null; // the server confirmed this enquiry: any further "submit" only shows the confirmation again
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const transient = e => !e.status || e.status === 502 || e.status === 503 || e.status === 504 || e.status === 408;
    async function attemptSubmit(answers) {
      for (let attempt = 0; ; attempt++) {
        try { return await cfg.onSubmit(answers, { key: submitKey, attempt }); }
        catch (e) {
          const d = e.data || {};
          if (e.status === 409 && d.already_submitted) {  // saved earlier (same key) with different answers: keep the saved enquiry, never a second one
            if (d.result && d.result.id) return Object.assign({}, d.result, { already_submitted: true });
            throw e;
          }
          if (e.status === 409 && d.status === 'in_progress' && attempt < 8) { await sleep(1500); continue; }  // another attempt of this very enquiry is being saved
          if (transient(e) && attempt < 2) { await sleep(1200 * (attempt + 1)); continue; }  // lost reply / connection blip: same key, so a retry is safe
          throw e;
        }
      }
    }
    function renderDone(answers, result) {
      if (result && result.already_submitted) answers = Object.assign({}, answers, {
        first_name: result.first_name || answers.first_name,
        move_in: result.move_in || answers.move_in,
      });  // show the enquiry that IS saved, not a later unsaved name/date
      const d = R('done'); R('doneName').textContent = answers.first_name;
      R('doneDate').textContent = new Date(answers.move_in).toLocaleDateString('en-AU', { day: 'numeric', month: 'long', year: 'numeric' });
      const ref = result && result.id ? `Reference MateBot-${String(result.id).padStart(5, '0')}` : '';
      R('doneRef').textContent = ref ? `${ref}${staffMode ? ' · saved to the data bank (channel: ' + (result.channel || 'Phone') + ')' : ''}${result.already_submitted ? ' · This enquiry had already been sent, so the later changes were not saved. Contact the team to change anything.' : ''}` : '';
      if (staffMode) {  // plain confirmation for staff, no mascot (T10)
        R('matchArt').hidden = true; R('doneKicker').hidden = true; R('timeline').hidden = true; R('mateBox').hidden = true;
        R('doneTitle').innerHTML = `Saved: <span>${esc(answers.first_name)}</span>`;
        R('doneBody').textContent = result && result.already_submitted ? 'An enquiry had already been saved from the earlier attempt, so nothing was saved twice and the later changes were not kept. Check it under Enquiries; use Start again for a new enquiry.' : 'The enquiry is in the data bank and joins the next matching run.';
      } else {
        // I19(d): "It's a match" screen; GP11(a): Bring a mate link
        const friendOf = mate && mate.valid ? mate.enquirer_first_name : null;
        R('doneKicker').textContent = friendOf ? `You're in, ${answers.first_name}!` : `It's a match, ${answers.first_name}!`;
        R('doneTitle').textContent = friendOf ? `You and ${friendOf} are now a team.` : 'Your enquiry is in.';
        R('doneBody').innerHTML = friendOf
          ? `MateBot AI keeps you and ${esc(friendOf)} in the same group and is now looking at everyone moving to Melbourne around <b>${esc(R('doneDate').textContent)}</b>. When we've found the right group and an apartment that suits you all, our team will be in touch with you both.`
          : `MateBot AI is now analysing your profile against everyone moving to Melbourne around <b>${esc(R('doneDate').textContent)}</b>. When we've found a highly compatible group and an apartment that suits you all, our team will be in touch to introduce you.`;
        if (result && result.mate_link) {
          const link = `${pageBase()}#/mate?t=${encodeURIComponent(result.mate_link)}`;
          R('mateLink').value = link; R('mateBox').hidden = false;
          const limit = meta.mate_limit; R('mateLimit').textContent = limit ? `Up to ${limit} people in total can share one link, including you, so there is always room for at least one more housemate.` : '';
          const share = R('shareMate'); const text = `Want to share an apartment in Melbourne? Fill in your own MateBot AI form with this link and we'll be matched together: ${link}`;
          share.href = `mailto:?subject=${encodeURIComponent('Live with me in Melbourne?')}&body=${encodeURIComponent(text)}`;
          if (navigator.share) share.addEventListener('click', e => { e.preventDefault(); navigator.share({ title: 'MateBot AI', text }).catch(() => {}); });
        }
        R('meterLabel').textContent = "You're 100% matched"; R('meterFill').style.width = '100%'; R('meterBar').setAttribute('aria-valuenow', 100);
        if (saveBtn) saveBtn.hidden = true;
      }
      form.hidden = true; $('.steps').hidden = true; R('tipWrap').hidden = true; d.hidden = false; d.focus(); d.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth' });
      if (!staffMode) celebrate(d, true);
    }
    function showSaved(answers, result) {
      // The server has the enquiry. Whatever goes wrong while drawing the confirmation, the person must still see success
      // (and never a "try again" that would send it twice).
      try { renderDone(answers, result); }
      catch (err) {
        console.error('Confirmation screen failed to render; the enquiry is saved', err);
        try {
          form.hidden = true; const st = $('.steps'); if (st) st.hidden = true; const tw = R('tipWrap'); if (tw) tw.hidden = true;
          const d = R('done'); d.hidden = false;
          const t = R('doneTitle'); if (t) t.textContent = `Thank you, ${answers.first_name}. Your enquiry is in.`;
          const r = R('doneRef'); if (r && result && result.id) r.textContent = `Reference MateBot-${String(result.id).padStart(5, '0')}`;
        } catch { /* nothing more we can do visually; the state guards below still prevent a resend */ }
      }
      try { if (cfg.onDone) cfg.onDone(result); } catch (err) { console.error('onDone failed; the enquiry is saved', err); }
      // I19(p): staff can now email the SAVED enquiry to the enquirer; a failure here never touches the saved state
      try { if (staffMode && cfg.onLoadSaved && result && result.id) { const b = R('emailFormDone'); if (b) b.hidden = false; if (emailWanted) { emailWanted = false; prepareEmail(); } } } catch (err) { console.error('email action failed to show; the enquiry is saved', err); }
    }
    async function submit() {
      const next = R('next');
      if (savedResult) { showSaved(savedResult.answers, savedResult.result); return; }  // already confirmed: never send again
      if (inFlight) return;  // a click, Enter or key repeat while the first attempt is still running
      inFlight = true; next.disabled = true; next.textContent = 'Saving…';
      R('submitError').hidden = true;
      store.write({ k: submitKey, s: 'pending' });  // outcome uncertain from here until the server answers
      let answers;
      try {
        answers = collect();
        const result = await attemptSubmit(answers);
        savedResult = { answers, result };
        store.write(null);  // confirmed: nothing uncertain is left; a brand-new enquiry later gets a new key
        showSaved(answers, result);
      } catch (e) {
        // Not confirmed. The key stays the same, so "try again" can only ever complete THIS enquiry, never make a second one.
        if (e.data && e.data.errors && !e.data.already_submitted) showServerErrors(e.data.errors);
        else { const se = R('submitError'); se.hidden = false; se.textContent = (e.message || 'Something went wrong.') + ' Please try again; your answers are kept and will not be sent twice.'; }
      } finally {
        inFlight = false;
        if (!savedResult) { emailWanted = false; next.disabled = false; go(current); }
      }
    }

    // ---------- I19(p): Email completed form (staff only; prepares an email, never sends one) ----------
    // The email is built from the SAVED enquiry (GET through cfg.onLoadSaved), not from the on-screen controls, so a 409
    // already_submitted replay or a later unsaved edit can never put unsaved answers in the enquirer's copy. The saved answers
    // are poured into a fresh, isolated, off-screen form instance (empty state, trusted saved values only) and read back with
    // the same reviewRows logic as the summaries; nothing is kept in storage or a URL and the instance is removed afterwards.
    const EMAIL_SUBJECT = 'Your MateBot enquiry: a copy of your answers';
    let emailGen = 0;          // bumped by Start again: a late GET reply from a previous enquiry is then ignored
    let emailWanted = false;   // "Email completed form" pressed on the summary: open the preview once the save is confirmed
    const emailEls = () => ({ box: R('emailBox'), to: R('emailTo'), subject: R('emailSubject'), body: R('emailBody'), open: R('emailOpen'), err: R('emailError'), btn: R('emailFormDone') });
    function resetEmail() {
      emailGen++; emailWanted = false;
      const e = emailEls(); if (!e.box) return;
      e.box.hidden = true; e.to.textContent = ''; e.subject.textContent = ''; e.body.value = ''; e.open.removeAttribute('href'); e.err.hidden = true; e.err.textContent = ''; e.btn.hidden = true; e.btn.disabled = false; e.btn.textContent = 'Email completed form'; const c = R('emailCopied'); if (c) c.textContent = '';
    }
    const validEmail = v => typeof v === 'string' && /^[^\s@,;:<>()\[\]\\"']+@[^\s@,;:<>()\[\]\\"']+\.[^\s@,;:<>()\[\]\\"']+$/.test(v) && v.length <= 254;
    const rowText = v => Array.isArray(v) ? v.join(', ') : (v && typeof v === 'object') ? `${v.main}${v.sub ? ' (' + v.sub + ')' : ''}` : String(v);
    function serialiseSaved(answers) {
      // a fresh instance of this same form, never shown, filled only with the saved answers
      // off-screen (not the hidden attribute: shown() treats a [hidden] ancestor as not applicable); outside this root, so no selector here can see it
      const hostEl = document.createElement('div'); hostEl.className = 'form-serialiser'; hostEl.setAttribute('aria-hidden', 'true'); hostEl.setAttribute('inert', ''); document.body.append(hostEl);
      try { const inst = window.mountForm(hostEl, { mode: 'staff', meta: cfg.meta, staffList: [], serializerOnly: true }); inst.fill(answers); return inst.customerRows(); }
      finally { hostEl.remove(); }
    }
    function emailBody(saved, groups) {
      const ref = `MateBot-${String(saved.id).padStart(5, '0')}`;
      const lines = [
        `Hi ${String(saved.first_name || '').trim()},`, '',
        'Thanks for your time on the phone. Here is a copy of the form we completed together.', '',
        "MateBot will now use its super intelligence to find housemates who suit you. We'll be in touch soon with a suggested group. You can chat with each other, then tell us if everyone is happy or if you'd like any changes.", '',
        'Once your group is confirmed, MateBot will suggest suitable apartments for your group. Our team will contact you to arrange viewings of the shortlisted options.', '',
        'If any answers below need changing, just reply to this email.', '',
        'The MateBot team', '', '', 'Your answers', `Reference: ${ref}`,
      ];
      groups.forEach(([title, rows, num]) => { lines.push('', `${num}. ${title}`); rows.forEach(([label, value]) => lines.push(`- ${label}: ${rowText(value)}`)); });
      return lines.join('\n');
    }
    async function prepareEmail() {
      const e = emailEls(); if (!e.box || !savedResult || !savedResult.result || !savedResult.result.id || !cfg.onLoadSaved) return;
      const gen = emailGen; const id = savedResult.result.id;
      e.err.hidden = true; e.btn.disabled = true; e.btn.textContent = 'Preparing…';
      const stale = () => gen !== emailGen || !root.isConnected;   // Start again, or staff moved to another screen meanwhile
      let saved;
      try { saved = await cfg.onLoadSaved(id); }   // one authenticated GET, never a POST; the enquiry stays saved whatever happens here
      catch (ex) { if (stale()) return; e.btn.disabled = false; e.btn.textContent = 'Email completed form'; e.err.hidden = false; e.err.textContent = 'Could not load the saved enquiry to prepare the email. The enquiry is saved (see the reference above); try Email completed form again.'; return; }
      if (stale()) return;  // Start again / navigated away meanwhile: never show this enquiry's copy anywhere else
      try {
        if (!saved || saved.id !== id) throw new Error('mismatch');
        const aj = saved.answers_json; if (!aj || typeof aj !== 'object' || Array.isArray(aj)) throw new Error('saved answers missing');
        const groups = serialiseSaved(aj);
        // never a header-only or defaults-only "completed form": the saved answers must really have flowed through
        const flat = groups.flatMap(([, rows]) => rows);
        if (flat.length < 8 || !flat.some(([, v]) => typeof v === 'string' && v === String(saved.first_name || '').trim())) throw new Error('saved answers did not serialise');
        const to = String(saved.email || '').trim(); const ok = validEmail(to);
        e.to.textContent = ok ? to : 'No valid email address is saved for this enquiry; copy the message and send it another way.';
        e.subject.textContent = EMAIL_SUBJECT;
        e.body.value = emailBody(saved, groups);
        if (ok) e.open.href = `mailto:${encodeURIComponent(to)}?subject=${encodeURIComponent(EMAIL_SUBJECT)}&body=${encodeURIComponent(e.body.value)}`; else e.open.removeAttribute('href');
        e.open.classList.toggle('is-disabled', !ok); e.open.setAttribute('aria-disabled', ok ? 'false' : 'true');
        e.box.hidden = false; e.box.focus(); e.box.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
      } catch (ex) {
        console.error('Email preview failed; the enquiry is saved', ex);
        e.err.hidden = false; e.err.textContent = 'Could not prepare the email from the saved enquiry. The enquiry is saved (see the reference above); try Email completed form again.';
      } finally { if (!stale()) { e.btn.disabled = false; e.btn.textContent = 'Email completed form'; } }
    }
    if (R('emailFormDone')) R('emailFormDone').addEventListener('click', prepareEmail);
    if (R('emailOpen')) R('emailOpen').addEventListener('click', ev => { if (!R('emailOpen').getAttribute('href')) ev.preventDefault(); });
    if (R('emailCopy')) R('emailCopy').addEventListener('click', () => { const b = R('emailBody'); copyText(`To: ${R('emailTo').textContent}\nSubject: ${R('emailSubject').textContent}\n\n${b.value}`, R('emailCopied')).then(() => { if (R('emailCopied').textContent !== 'Copied.') { b.focus(); b.select(); } }); });
    if (R('emailClose')) R('emailClose').addEventListener('click', () => { R('emailBox').hidden = true; const b = R('emailFormDone'); if (!b.hidden) b.focus(); });
    // On the staff summary: validate everything, save once through the normal path, then open the preview (the save itself is
    // advance() → submit(), so it shares the submission key, the in-flight guard and the saved guard: never a second enquiry).
    if (R('emailForm')) R('emailForm').addEventListener('click', () => { if (inFlight) return; emailWanted = true; advance(); });

    const scrollTop = () => { const y = root.getBoundingClientRect().top + window.scrollY - 8; window.scrollTo({ top: Math.max(0, y), behavior: reducedMotion() ? 'auto' : 'smooth' }); };
    function advance() {
      if (inFlight) return;  // the enquiry is being saved: ignore every further Next / Enter until the server answers
      const all = units(); const u = all[current]; if (!validate(u)) { emailWanted = false; return; }
      if (fromReview) { go(reviewIndex()); scrollTop(); if (draftToken) saveDraft(false); return; }
      // Summary edits can invalidate a later answer (dates, budgets or conditional fields).
      // Check all applicable screens before sending; return directly to the one needing attention.
      if (u === reviewUnit()) {
        const invalid = all.findIndex(screen => screen !== reviewUnit() && !validate(screen));
        if (invalid >= 0) { emailWanted = false; go(invalid); scrollTop(); return; }
      }
      const s = stepOf(u); const nextU = all[current + 1];
      if (!nextU || stepOf(nextU) !== s) { done.add(s.dataset.step); if (cardMode && nextU) celebrate(R('meter'), false); }
      if (current === all.length - 1) return submit();
      go(current + 1); scrollTop();
      if (cardMode && draftToken) saveDraft(false);  // keep a saved form up to date as the visitor goes
    }
    R('next').addEventListener('click', advance);
    // I19(m): "Back to check your answers" validates the screen being left and returns directly to the summary (advance()
    // already does exactly that while fromReview is set, so it shares the same in-flight guards and draft saving).
    if (R('toReview')) R('toReview').addEventListener('click', advance);
    // Enter in a text field acts like Next (and never reloads the page through the browser's own form submission). It goes
    // through the same in-flight / saved guards as the button, so a held or repeated Enter cannot send the enquiry twice.
    form.addEventListener('submit', e => { e.preventDefault(); advance(); });
    form.addEventListener('keydown', e => { if (e.key !== 'Enter' || e.target.tagName !== 'INPUT' || e.target.type === 'checkbox' || e.target.type === 'radio') return; e.preventDefault(); if (!e.repeat) advance(); });
    R('back').addEventListener('click', () => { go(current - 1); scrollTop(); });
    // I19(k): after a Change, one tap returns to the review screen (the screen being left is checked first, like Next)
    $$('[data-role="restart"]').forEach(b => b.addEventListener('click', () => { resetEmail(); submitKey = newKey(); savedResult = null; store.write(null); R('next').disabled = false; form.reset(); applyTestContext(); done.clear(); furthest = 0; draftToken = null; fromReview = false; hasReviewed = false; form.hidden = false; $('.steps').hidden = false; R('done').hidden = true; clearErrors(form); currentGuide = null; buildGuide('12m'); if (saveBtn) saveBtn.hidden = false; $$('.swipe').forEach(refreshSwipe); go(0); }));
    R('copyMate').addEventListener('click', () => copyText(R('mateLink').value, R('mateCopied')));
    buildSwipes();
    function fill(values) {
      const v0 = Object.assign({}, values);
      // Retired questions (I8 / I11(d), 8 Nov 2026) in an older saved form or stored submission are ignored. The old lead-only
      // consent_age is dropped too, so it can never pre-tick the all-occupants declaration (adults_confirm is only restored from
      // a saved copy of this form that already held the real declaration, e.g. a resumed draft or the staff email copy).
      ['dob', 'sex', 'sex_other', 'guardian_name', 'guardian_relationship', 'guardian_email', 'guardian_phone', 'consent_age'].forEach(k => { delete v0[k]; });
      // 17 Nov 2026: sex_pref, age_pref, own_region, avoid_* restored for matching
      const ft = Array.isArray(v0.features_top) ? v0.features_top : []; delete v0.features_top;   // set after 3.11 has rendered the 3.12 chips
      const pm = v0.party_members; const cp = v0.couples; delete v0.party_members; delete v0.couples;
      if (Array.isArray(v0.languages)) { const extra = v0.languages.filter(l => !languages.includes(l)); if (extra.length) { v0.languages = v0.languages.filter(l => languages.includes(l)).concat('Other'); v0.languages_other = v0.languages_other || extra.join(', '); } }
      const set = (k, v) => { const el = form.elements[k]; if (!el) return; if (el.type === 'checkbox' || el.type === 'radio' || el.length > 1 && !el.tagName) { $$(`[name="${k}"]`, form).forEach(i => { i.checked = [].concat(v).includes(i.value) || (i.type === 'checkbox' && v === true); }); } else if (el.multiple) { [...el.options].forEach(o => { o.selected = [].concat(v).includes(o.value); }); } else el.value = v; };
      ['move_in', 'move_out', 'party', 'party_size', 'rooms', 'features'].forEach(k => { if (k in v0) set(k, v0[k]); });  // these drive the price guide, party rows, first-choice list and 3.12
      applyConditions();
      Object.entries(v0).forEach(([k, v]) => set(k, v));
      applyConditions();
      if (ft.length) { set('features_top', ft); applyConditions(); }
      if (Array.isArray(pm)) { $$('.party-row').forEach((row, i) => { if (pm[i]) PM_FIELDS.forEach(k => { const el = row.querySelector(`[data-pm="${k}"]`); if (el && pm[i][k] != null) el.value = pm[i][k]; }); }); applyConditions(); }
      if (Array.isArray(cp) && cp.length) { coupleCount = cp.length; renderCoupleRows(); $$('.couple-row').forEach((r, i) => { if (cp[i]) { r.querySelector('[data-cp="a"]').value = cp[i][0] || ''; r.querySelector('[data-cp="b"]').value = cp[i][1] || ''; } }); }
      $$('.swipe').forEach(refreshSwipe);
    }
    go(0);
    if (cfg.draft && cfg.draft.answers) { fill(cfg.draft.answers); const at = Math.min(+cfg.draft.step || 0, units().length - 1); furthest = at; go(at); const b = R('banner'); if (b.hidden) { b.hidden = false; b.className = 'form-banner form-banner--resume'; b.textContent = cfg.draft.origin === 'homebot' ? 'You have come over from HomeBot. Your group-size answers are already filled in — please check them as you go; everything else is asked here.' : 'Welcome back. Your saved answers are here; carry on where you left off.'; } }
    const formApi = {
      fill, collect, go,
      reviewRows: () => steps().map(s => [s.dataset.title, reviewRows(s).map(([label, value]) => [label, value])]),
      // I19(p): the enquirer's own answers only: never the Staff only section (channel, campaign, staff member, date, notes, TEST flag)
      customerRows: () => steps().map((s, i) => s.classList.contains('staff-step') ? null : [s.dataset.title, reviewRows(s).map(([label, value]) => [label, value]), i + 1]).filter(g => g && g[1].length),
    };
    root.matebotForm = formApi;  // browser QA hook (no data; the same object the caller gets)
    return formApi;
  };
})();
