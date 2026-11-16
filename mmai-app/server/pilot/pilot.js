/* Sign-in page for private testing. It only talks to this site's own sign-in addresses; the browser keeps the sign-in in a
   cookie that scripts cannot read. Nothing is stored by this script. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const signedOut = $('signedOut'), signedIn = $('signedIn'), form = $('signIn'), msg = $('msg'), btn = $('signInBtn');

  // Where to go after signing in: only a plain path on this same site is accepted; anything else is ignored. The path must
  // already be in its plain form (no empty, "." or ".." parts, no percent codes, no backslash), because a browser tidies
  // those before it navigates and "/.//other-site" would become "//other-site". The address is always built from this
  // site's own origin, so the browser can never be sent anywhere else.
  function nextTarget() {
    const raw = new URLSearchParams(location.search).get('next') || '';
    const cut = raw.search(/[?#]/);
    const path = cut < 0 ? raw : raw.slice(0, cut);
    const query = cut < 0 || raw[cut] !== '?' ? '' : raw.slice(cut).split('#')[0];
    if (!path.startsWith('/') || path.includes('//') || /[\\%\u0000-\u0020\u007f]/.test(path) || /[\\\u0000-\u001f\u007f]/.test(query)) return '';
    if (path.split('/').some((part) => part === '.' || part === '..')) return '';
    if (path === '/pilot' || path.startsWith('/pilot/')) return '';
    let url;
    try { url = new URL(location.origin + path + query); } catch (e) { return ''; }
    if (url.origin !== location.origin || url.pathname !== path) return '';
    return location.origin + path + query + (location.hash || '');
  }

  function show(session) {
    signedOut.hidden = !!session;
    signedIn.hidden = !session;
    if (session) $('who').textContent = 'Signed in as ' + (session.display_name || session.username || 'tester') + '.';
    else $('username').focus();
  }

  async function call(method, path, body) {
    const res = await fetch(path, {
      method, credentials: 'same-origin', cache: 'no-store',
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { /* no JSON body */ }
    return { ok: res.ok, status: res.status, data };
  }

  async function check() {
    try {
      const r = await call('GET', '/api/auth/session');
      show(r.ok ? r.data : null);
    } catch (e) { show(null); }
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    msg.textContent = '';
    const username = $('username').value.trim(), password = $('password').value;
    if (!username || !password) { msg.textContent = 'Please enter your username and password.'; return; }
    btn.disabled = true;
    try {
      const r = await call('POST', '/api/auth/login', { username, password });
      $('password').value = '';
      if (r.ok) {
        const next = nextTarget();
        if (next) { location.assign(next); return; }
        show(r.data);
      } else {
        msg.textContent = (r.data && typeof r.data.detail === 'string' && r.data.detail) || 'Sign-in did not work. Please try again.';
      }
    } catch (e) {
      msg.textContent = 'The site could not be reached. Please check your connection and try again.';
    } finally { btn.disabled = false; }
  });

  $('signOutBtn').addEventListener('click', async () => {
    try { await call('POST', '/api/auth/logout'); } catch (e) { /* shown as signed out either way after the check below */ }
    await check();
  });

  check();
})();
