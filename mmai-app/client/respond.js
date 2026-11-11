/* GP13: a person's own answer page for a proposed group (#/respond?t=TOKEN).
   The token lives in the URL fragment only (never sent to the server as a query string or logged by it) and travels in the POST
   body. Every refusal from the server is one fixed sentence; nothing about the group is shown before the server has verified the
   person and the group. Plain words only; nothing here sends a message to anyone. */
(function () {
  const api = window.MMAI_API;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = v => (v == null || v === '') ? '—' : `$${Number(v).toFixed(0)}/wk`;
  const fmtD = s => { if (!s) return '—'; const d = new Date(s + (/^\d{4}-\d{2}-\d{2}$/.test(s) ? 'T00:00:00' : '')); return isNaN(d) ? esc(s) : d.toLocaleDateString('en-AU', { day: 'numeric', month: 'short', year: 'numeric' }); };
  const fmtDT = s => { if (!s) return ''; const d = new Date(s); return isNaN(d) ? esc(s) : d.toLocaleString('en-AU', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' }); };

  function invalid(host, text) {
    host.innerHTML = `<div class="card"><h2>This link is not valid</h2><p>${esc(text)}</p><p class="small muted">Links stop working when a group changes or after a while for safety. Nothing has been recorded against you.</p></div>`;
  }

  function page(host, token, d, flash) {
    const g = d.group || {};
    const cur = d.response;
    host.innerHTML = `
      <div class="card">
        ${d.is_test ? '<p class="small muted">Test data</p>' : ''}
        <h2>Your proposed housemates</h2>
        <p>Hi ${esc(g.your_name || '')}. ${g.empty_space ? 'The Apartment Living Melbourne team has gone through the remaining group and the empty space with you. Here is the group for this apartment as it stands:' : 'The Apartment Living Melbourne team has introduced you to a group for this apartment:'}</p>
        ${g.apartment_changed ? `<p class="msg small"><b>${esc(g.apartment_changed_text || 'Apartment details have changed. Please contact the ALM team about the current apartment options. Your response here is about these housemates, not an agreement to rent.')}</b></p>` : ''}
        <ul class="small">
          ${g.apartment_changed ? '' : `<li><b>${esc(g.address || '')}</b>${g.suburb ? `, ${esc(g.suburb)}` : ''}${g.type_label ? ` · ${esc(g.type_label)}` : ''}</li>
          <li>Lease: ${fmtD(g.lease_start)} to ${fmtD(g.lease_end)}</li>`}
          <li>Housemates: ${(g.housemates || []).map(esc).join(', ') || '—'}</li>
          ${g.empty_space ? `<li><b>One space in this apartment is empty${g.empty_space_count > 1 ? ` (${g.empty_space_count} spaces are empty)` : ''}.</b> The group would lease the whole apartment with ${g.empty_space_count > 1 ? 'those spaces' : 'that space'} unfilled until another person is found. ${g.uncovered_weekly == null ? 'The team will confirm with you how much of the whole rent is not covered by the current proposed shares.' : `About ${money(g.uncovered_weekly)} of the whole rent is not covered by the current proposed shares.`}${g.discount_note ? ` The team has noted: “${esc(g.discount_note)}”.` : ''}</li>` : ''}
          ${g.apartment_changed ? '' : `<li>Your proposed share: ${g.proposed_weekly_share == null ? '<span class="muted">not set yet — the team will go through the figures with you</span>' : `<b>${money(g.proposed_weekly_share)}</b>${g.proposed_total_weekly != null ? ` of ${money(g.proposed_total_weekly)} for the whole apartment` : ''} <span class="muted">(a proposal, not an agreed amount)</span>`}</li>`}
        </ul>
        <p class="small">${esc(g.meaning || '')}</p>
        ${flash ? `<p class="msg ok">${esc(flash)}</p>` : ''}
        ${cur ? `<p class="msg small"><b>Your current answer:</b> ${esc(cur.label)}${cur.note ? ` — “${esc(cur.note)}”` : ''} <span class="muted">(${fmtDT(cur.responded_at)})</span>. You can change it below.</p>` : ''}
        <form data-role="form">
          <fieldset class="field"><legend><b>Are you happy to share this apartment with these housemates?</b></legend>
            <div class="options">
              ${(d.choices || []).map(c => `<label class="pill"><input type="radio" name="response" value="${esc(c.value)}" ${cur && cur.response === c.value ? 'checked' : ''}> ${esc(c.label)}</label>`).join('')}
            </div>
          </fieldset>
          <div class="field"><label>Anything you would like the team to know? (optional)<textarea name="note" rows="3" maxlength="1000" placeholder="For example: I'd prefer the smaller room, or a question about dates">${esc(cur && cur.note || '')}</textarea></label></div>
          <p class="msg err" data-role="err" hidden></p>
          <div class="actions"><button class="btn btn--primary" type="submit">Send my answer</button></div>
          <p class="small muted">Your answer goes to the ALM team only. The other housemates do not see it. If you say no or ask for a change, the team will be in touch; nothing happens automatically.</p>
        </form>
      </div>`;
    const form = host.querySelector('[data-role="form"]'), err = host.querySelector('[data-role="err"]');
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      const choice = form.querySelector('input[name="response"]:checked');
      if (!choice) { err.hidden = false; err.textContent = 'Please choose one of the answers.'; return; }
      const btn = form.querySelector('button[type="submit"]'); btn.disabled = true; err.hidden = true;
      try {
        const r = await api.post('/api/public/group-responses/respond', { token, response: choice.value, note: form.querySelector('textarea[name="note"]').value.trim() });
        const d2 = { ...d, response: r.response, group: r.group || d.group };
        page(host, token, d2, r.recorded ? 'Thank you — your answer has been recorded.' : 'That is already your recorded answer; nothing changed.');
        window.scrollTo(0, 0);
      } catch (ex) {
        if (ex.status === 410) { invalid(host, ex.message); return; }
        err.hidden = false; err.textContent = ex.message || 'Your answer could not be saved. Please try again.'; btn.disabled = false;
      }
    });
  }

  async function mount(host, token) {
    host.innerHTML = '<p class="loading">Checking your link…</p>';
    if (!token) { invalid(host, 'This link is no longer valid. Ask the ALM team for a fresh link.'); return; }
    try {
      const d = await api.post('/api/public/group-responses/lookup', { token });
      page(host, token, d, null);
    } catch (ex) {
      invalid(host, ex.status === 410 ? ex.message : (ex.status === 503 ? 'We are busy for a moment. Please try again shortly.' : 'The page could not be loaded. Please try again shortly.'));
    }
  }

  window.MMAI_RESPOND = { mount };
})();
