/* Staff records an exact booking decision, never a rule override or readiness shortcut.
   Shared by both detail pages; every write is judged again by the server. */
(() => {
  const api = window.MMAI_API;
  const esc = api.esc;
  const exactMoney = value => {
    const s = String(value ?? '');
    if (!/^\d+(\.\d{1,2})?$/.test(s)) return esc(s || 'Not recorded');
    const [whole, fraction = ''] = s.split('.');
    return '$' + whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',') + '.' + (fraction + '00').slice(0, 2);
  };
  const stateText = r => {
    const state = (r.readiness || {}).state;
    if (state === 'authorised_now') return 'Exception recorded for this booking';
    if (state === 'withdrawn') return 'Withdrawn: not usable';
    if (state === 'stale') return 'Out of date: a new booking decision is needed';
    if (state === 'held' && r.readiness.reason === 'customer_agreement_needed') return 'Held: customer agreement is needed';
    return 'Not usable for the current booking';
  };
  const dateTime = value => value ? new Date(value).toLocaleString('en-AU') : 'Not recorded';

  async function mount(box, config) {
    const { bot, id, owner, canWrite, onChanged } = config;
    const homebot = bot === 'homebot';
    const base = homebot ? `/api/homebot/enquiries/${id}` : `/api/groups/${id}`;
    box.innerHTML = '<p class="small" role="status">Loading exact-booking exceptions…</p>';
    let records, apartments;
    try {
      const [history, stock] = await Promise.all([
        api.get(base + '/exit-exceptions'),
        canWrite ? api.get('/api/apartments') : Promise.resolve({ items: [] })
      ]);
      records = history.items || []; apartments = stock.items || [];
    } catch (ex) {
      box.innerHTML = `<p class="msg err" role="alert">Could not load the booking decisions. ${esc(ex.message)}</p><button class="btn btn--ghost btn--sm" type="button" data-ex-load>Try loading again</button>`;
      box.querySelector('[data-ex-load]').addEventListener('click', () => mount(box, config));
      return;
    }
    if (!box.isConnected) return;
    const current = (owner.apartments || []).find(a => a.is_current);
    const start = homebot ? owner.move_in : current?.lease_start;
    const end = homebot ? owner.move_out : current?.lease_end;
    box.innerHTML = `<section class="card card--tight mt">
      <h3>Exact-booking exit exceptions</h3>
      <p class="small">Record a staff decision for one apartment, exact dates and the whole-apartment weekly rent, including any extra-bed charge. This does not reserve an apartment, change the people, or make an offer ready to share. Availability, the other requirements and a current AI review still apply.</p>
      <p class="msg" data-ex-message role="status" aria-live="polite"></p>
      ${canWrite ? `<details class="more"><summary>Record a new booking exception</summary>
        <form data-ex-propose class="mt">
          <div class="field"><label>Apartment<select name="apartment_id" required><option value="">Choose the apartment</option>${apartments.map(a => `<option value="${a.id}" ${Number(a.id) === Number(owner.apartment_id) ? 'selected' : ''}>${esc(a.address)}${a.is_leased ? ' (leased)' : !a.available_from ? ' (availability not recorded)' : ''}</option>`).join('')}</select></label></div>
          ${homebot ? `<div class="field"><label>Booking scenario<select name="scenario"><option value="current">People in the party now</option>${owner.party_mode !== 'complete' ? '<option value="final_size">Intended final party size (provisional)</option>' : ''}</select></label></div>` : ''}
          <div class="field"><label>Exact move-in date<input name="move_in" type="date" required value="${esc(start || '')}"></label></div>
          <div class="field"><label>Exact move-out date<input name="move_out" type="date" required value="${esc(end || '')}"></label></div>
          <div class="field"><label>Whole-apartment weekly total, including any extra-bed charge<input name="exception_total_weekly_rent" inputmode="decimal" type="text" pattern="[0-9]+([.][0-9]{1,2})?" required placeholder="For example, 1650.00"></label></div>
          <label class="small"><input type="checkbox" name="staff_decision" required> I am recording the staff decision for these exact booking terms, not the customer's agreement.</label>
          <div class="actions mt"><button class="btn btn--sm" type="submit">Record staff booking decision</button></div>
        </form></details>` : '<p class="small muted">Booking decisions are read-only for this record.</p>'}
      <div class="mt">${records.length ? records.map(r => {
        const state = (r.readiness || {}).state;
        const active = state !== 'withdrawn';
        return `<article class="card card--tight mt" data-ex-record="${r.id}">
          <h4>${esc(r.address || 'Apartment no longer listed')}</h4>
          <p><span class="badge ${state === 'authorised_now' ? 'badge--ok' : 'badge--warn'}">${stateText(r)}</span></p>
          <p class="small">${esc(r.move_in)} → ${esc(r.move_out)} · <b>${exactMoney(r.exception_total_weekly_rent)}/week</b> for the whole apartment${homebot ? ` · ${r.scenario === 'final_size' ? 'intended final party size (provisional)' : 'people in the party now'}` : ''}</p>
          <p class="small">Staff decision recorded by ${esc(r.approved_by)} on ${esc(dateTime(r.approved_at))}.</p>
          ${state === 'stale' ? '<p class="small">The people or booking facts no longer match this decision. Withdraw it and record a new decision for the current booking.</p>' : ''}
          ${r.agreement_recorded_at ? `<p class="small">Customer agreement: ${esc(r.agreement_scope || 'Personal details removed')} · ${exactMoney(r.agreement_total)}/week. Recorded by ${esc(r.agreement_recorded_by)} on ${esc(dateTime(r.agreement_recorded_at))}.</p>` : '<p class="small muted">No customer agreement is recorded on this decision.</p>'}
          ${canWrite && active && state !== 'stale' && !r.agreement_recorded_at ? `<details class="more"><summary>Record customer agreement, if received</summary>
            <form data-ex-agreement="${r.id}" class="mt">
              <div class="field"><label>Whose agreement was received, how and when<input name="agreement_scope" maxlength="200" required placeholder="For example, customer by phone on 11 November"></label></div>
              <div class="field"><label>Exact whole-apartment weekly total agreed<input name="total" inputmode="decimal" type="text" pattern="[0-9]+([.][0-9]{1,2})?" required></label></div>
              <p class="small">This must equal ${exactMoney(r.exception_total_weekly_rent)}. It records an agreement already received; it does not ask the customer or send anything.</p>
              <button class="btn btn--sm" type="submit">Record customer agreement</button>
            </form></details>` : ''}
          ${canWrite && active ? `<details class="more mt" data-ex-withdraw-box><summary>Withdraw this decision</summary>
            <p class="small">Withdraw the booking exception for these exact terms? Its history stays on file, but it can no longer authorise an offer.</p>
            <div class="actions"><button class="btn btn--danger btn--sm" type="button" data-ex-withdraw="${r.id}">Confirm withdrawal</button><button class="btn btn--ghost btn--sm" type="button" data-ex-cancel>Cancel</button></div>
          </details>` : ''}
        </article>`;
      }).join('') : '<p class="small muted">No booking exceptions recorded.</p>'}</div>
    </section>`;
    let busy = false;
    const message = box.querySelector('[data-ex-message]');
    const write = async (path, body) => {
      if (busy) return;
      busy = true;
      if (config.onBeforeWrite) config.onBeforeWrite();
      const buttons = [...box.querySelectorAll('button')];
      buttons.forEach(b => { b.disabled = true; });
      message.className = 'msg'; message.textContent = 'Saving the booking decision…';
      try {
        await api.post(path, body);
        onChanged();
      } catch (ex) {
        message.className = 'msg err';
        message.textContent = ex.message;
        if (!ex.status || ex.status >= 500) {
          message.textContent += ' Reload the record to check what was saved before trying again.';
          const reload = document.createElement('button');
          reload.type = 'button'; reload.className = 'btn btn--ghost btn--sm'; reload.textContent = 'Reload this record';
          reload.addEventListener('click', onChanged); message.append(' ', reload);
          // Keep mutations disabled after an uncertain result; never blindly retry.
        } else {
          busy = false; buttons.forEach(b => { b.disabled = false; });
        }
        message.scrollIntoView({ block: 'nearest' });
      }
    };
    box.addEventListener('submit', ev => {
      const form = ev.target.closest('form'); if (!form) return;
      ev.preventDefault(); ev.stopPropagation();
      if (busy || !form.reportValidity()) return;
      const values = Object.fromEntries(new FormData(form));
      if (form.hasAttribute('data-ex-propose')) {
        delete values.staff_decision;
        write(base + '/exit-exception', values);
      } else if (form.dataset.exAgreement) {
        write(`/api/exit-exceptions/${form.dataset.exAgreement}/agreement`, values);
      }
    });
    box.addEventListener('click', ev => {
      const b = ev.target.closest('button'); if (!b) return;
      if (b.hasAttribute('data-ex-cancel')) b.closest('[data-ex-withdraw-box]').open = false;
      if (b.dataset.exWithdraw) write(`/api/exit-exceptions/${b.dataset.exWithdraw}/withdraw`, {});
    });
  }
  window.MMAI_STAFF_EXCEPTIONS = { mount };
})();
