/* Google Tag Manager for the public pages (MateBot at / and HomeBot at /homebot/). It is an external file so that no page needs an
   inline script (Content-Security-Policy: script-src allows 'self' and https://www.googletagmanager.com only), and there is no
   <noscript> frame. The container's tags run only inside Google Tag Manager; the forms push one event to window.dataLayer when an
   enquiry is saved (alm_bot_complete, no personal data).
   The staff workspace shares MateBot's index.html and is picked by the first part of the hash route, so the route is read exactly
   as app.js reads it, once, when the page loads: #/staff… never loads Google Tag Manager. */
(() => {
  const CONTAINER = 'GTM-K729FCSB';
  const [route] = (location.hash || '#/').slice(1).split('?');
  if (route.split('/').filter(Boolean)[0] === 'staff') return;
  window.dataLayer = window.dataLayer || [];
  window.dataLayer.push({ 'gtm.start': Date.now(), event: 'gtm.js' });
  const tag = document.createElement('script');
  tag.async = true;
  tag.src = 'https://www.googletagmanager.com/gtm.js?id=' + CONTAINER;
  const first = document.getElementsByTagName('script')[0];
  if (first && first.parentNode) first.parentNode.insertBefore(tag, first); else document.head.appendChild(tag);
})();
