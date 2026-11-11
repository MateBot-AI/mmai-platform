/* API client. The staff session lives in an HttpOnly cookie set by the server (T7); the token is held in memory for the
   current page only (browser storage is unavailable in the preview iframe). If the cookie is dropped, the server can still
   resume the sign-in from the hosting proxy's per-browser visitor id. */
(() => {
  const localHosts = new Set(['localhost', '127.0.0.1', '[::1]']);
  // the deploy-time placeholder lives in a <meta> tag so index.html needs no inline script (Content-Security-Policy, review L1)
  const metaBase = document.querySelector('meta[name="mmai-api-base"]');
  const raw = (metaBase && metaBase.content) || window.__API_BASE__ || '';
  const unresolved = raw.startsWith('__PORT_');
  if (unresolved && !localHosts.has(location.hostname) && location.protocol !== 'file:') {
    throw new Error('Backend proxy placeholder was not rewritten');
  }
  const API = unresolved ? '' : raw.replace(/\/$/, '');
  let token = null;

  async function request(method, path, body, opts = {}) {
    const headers = Object.assign({ Accept: 'application/json' }, opts.headers || {});  // e.g. X-Submission-Key on a form submission
    if (token) headers.Authorization = 'Bearer ' + token;
    let payload;
    if (body instanceof ArrayBuffer || body instanceof Uint8Array) { headers['Content-Type'] = 'application/octet-stream'; payload = body; }
    else if (body !== undefined) { headers['Content-Type'] = 'application/json'; payload = JSON.stringify(body); }
    // Same-origin API (self-hosted, or the preview proxy path): 'same-origin' sends the session cookie where the browser allows it.
    // Only a split self-hosted deployment (absolute API URL on another host) needs 'include'; the preview proxy answers with
    // Access-Control-Allow-Origin: * from a sandboxed (opaque) origin, which would reject 'include' outright.
    const res = await fetch(API + path, { method, headers, body: payload, credentials: /^https?:\/\//.test(API) ? 'include' : 'same-origin' });
    let data = null;
    const text = await res.text();
    try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }
    if (!res.ok) {
      const errs = data && data.errors && typeof data.errors === 'object' ? Object.values(data.errors).join(' ') : null;
      let detail = data && (data.detail || data.message);
      if (Array.isArray(detail)) detail = detail.map(d => (d && d.msg) ? `${(d.loc || []).slice(-1)[0] || ''}: ${d.msg}`.replace(/^: /, '') : String(d)).join('; ');
      const err = new Error(detail || errs || `Request failed (${res.status})`);
      err.status = res.status; err.data = data;
      if (res.status === 401 && token && opts.onUnauthorized) opts.onUnauthorized();
      throw err;
    }
    return data;
  }

  window.MMAI_API = {
    base: API,
    /* HTML-escape a value before it goes into innerHTML (security review H1). */
    esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    setToken: t => { token = t; },
    getToken: () => token,
    /* T7: ask the server whether this device is already signed in (cookie, kept token or proxy visitor id). */
    async resumeSession() {
      try { const r = await request('GET', '/api/auth/session'); if (r && r.token) token = r.token; return r; }
      catch (e) { if (e.status === 401) { token = null; return null; } throw e; }
    },
    get: (p, o) => request('GET', p, undefined, o),
    post: (p, b, o) => request('POST', p, b, o),
    patch: (p, b, o) => request('PATCH', p, b, o),
    put: (p, b, o) => request('PUT', p, b, o),
    del: (p, o) => request('DELETE', p, undefined, o),
    /* Authenticated file download (e.g. the apartment list as .xlsx): fetched with the session, then saved through a temporary link. */
    async download(path, filename) {
      const headers = {}; if (token) headers.Authorization = 'Bearer ' + token;
      const res = await fetch(API + path, { headers, credentials: /^https?:\/\//.test(API) ? 'include' : 'same-origin' });
      if (!res.ok) throw new Error(`Download failed (${res.status})`);
      const blob = await res.blob(); const url = URL.createObjectURL(blob);
      const a = document.createElement('a'); a.href = url; a.download = filename || (res.headers.get('Content-Disposition') || '').replace(/^.*filename="?([^";]+)"?.*$/, '$1') || 'download';
      document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 2000);
    },
  };
})();
