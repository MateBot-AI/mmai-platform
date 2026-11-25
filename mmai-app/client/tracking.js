/* Google Ads tracking details. A visitor who arrives from a Google ad carries a click id and campaign names in the web address
   (?gclid=…&utm_campaign=…). They are read once, when the page loads (before any routing), kept for the visit and for 90 days on
   this device, and handed to the public forms as window.ALM_TRACKING: a plain object with only the keys below, as trimmed strings of
   at most 255 characters ({} when there is nothing). The forms send it with the enquiry; staff phone entry never does. Nothing is
   sent from this file, and the same file serves MateBot (/) and HomeBot (/homebot/).
   - If the address carries any of the keys, those values REPLACE what was kept before (the newest ad click wins). Otherwise the kept
     values that are still in date are used, so the details survive a move between MateBot and HomeBot, a saved form opened later,
     and any address that has lost its query string (hash routing).
   - Kept in sessionStorage and localStorage as alm_gads_tracking, with savedAt (milliseconds). Storage can be blocked (private
     windows, the embedded preview), so every access is guarded and the page works the same without it.
   - The keys are also read from a query inside the hash (#/…?gclid=…). utm_gclid counts as gclid when there is no gclid.
   - Value rules mirror server/tracking.py: control characters removed, ends trimmed, a value longer than 255 characters is left out
     (never shortened), and case is never changed. */
(() => {
  const KEYS = ['gclid', 'gbraid', 'wbraid', 'utm_campaign', 'utm_ad_group', 'utm_keyword'];
  const STORE_KEY = 'alm_gads_tracking';
  const MAX_LEN = 255;
  const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
  const CONTROL = /[\p{Cc}\p{Zl}\p{Zp}]/gu;   // control characters (C0, DEL, C1) and the two Unicode line separators
  const tidy = v => { if (typeof v !== 'string') return ''; const t = v.replace(CONTROL, '').trim(); return Array.from(t).length <= MAX_LEN ? t : ''; };
  const pick = get => { const out = {}; KEYS.forEach(k => { const v = tidy(get(k)); if (v) out[k] = v; }); return out; };

  function fromAddress() {
    const search = new URLSearchParams(location.search);
    const inHash = new URLSearchParams(String(location.hash || '').split('?')[1] || '');
    const get = k => search.get(k) || inHash.get(k);
    const out = pick(get);
    if (!out.gclid) { const g = tidy(get('utm_gclid')); if (g) out.gclid = g; }
    return out;
  }

  function fromStore(name) {
    try {
      const store = window[name];
      const rec = JSON.parse(store.getItem(STORE_KEY) || 'null');
      if (!rec || typeof rec !== 'object' || typeof rec.savedAt !== 'number') return null;
      if (!(Date.now() - rec.savedAt <= MAX_AGE_MS)) { store.removeItem(STORE_KEY); return null; }   // past 90 days: gone
      const out = pick(k => rec[k]);
      return Object.keys(out).length ? out : null;
    } catch { return null; }
  }

  function save(values) {
    const text = JSON.stringify(Object.assign({ savedAt: Date.now() }, values));
    ['sessionStorage', 'localStorage'].forEach(name => {
      try { window[name].setItem(STORE_KEY, text); } catch { /* blocked or full: the values still reach this page */ }
    });
  }

  let tracking = {};
  try {
    tracking = fromAddress();
    if (Object.keys(tracking).length) save(tracking);
    else tracking = fromStore('sessionStorage') || fromStore('localStorage') || {};
  } catch { tracking = {}; }
  window.ALM_TRACKING = tracking;
})();
