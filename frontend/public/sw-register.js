// App root, as the server declared it in <base href> (see
// backend/app/core/ingress.py). '/' on direct access; a per-session
// /api/hassio_ingress/<token>/ path behind Home Assistant's ingress proxy.
// Read the tag rather than document.baseURI: with no tag, baseURI is the
// current page URL, which on a deep route would look like a prefix.
const baseEl = document.querySelector('base');
const BASE_PATH = (baseEl && baseEl.getAttribute('href')) || '/';

// Never register a service worker behind ingress. Its scope would be the
// per-session prefix, so every URL it precached and every response it cached
// would be dead the next time Home Assistant hands out a new session path --
// leaving a worker serving 404s for a prefix that no longer exists, with no
// page left under that scope to unregister it from.
if ('serviceWorker' in navigator && BASE_PATH === '/') {
  if (location.pathname.startsWith('/spoolbuddy')) {
    navigator.serviceWorker.getRegistrations().then((regs) => {
      if (regs.length > 0) {
        Promise.all([
          ...regs.map((r) => r.unregister()),
          caches.keys().then((names) => Promise.all(names.map((n) => caches.delete(n)))),
        ]).then(() => location.reload());
      }
    });
  } else {
    // Capture controller state at script-load. Used to decide whether a
    // subsequent `controllerchange` is a deploy-pickup (had a prior SW →
    // reload so the new bundle takes over) or a first install (no prior SW →
    // skip the reload; the in-flight React mount would otherwise race the
    // forced navigation, leaving the page wedged on a spinner. The previous
    // approach — `client.navigate(client.url)` from the SW's activate
    // handler — exhibited that race in Chromium and a waitUntil hang in
    // Firefox, both surfaced on every fresh demo subdomain).
    const hadController = !!navigator.serviceWorker.controller;
    let reloading = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!hadController || reloading) return;
      reloading = true;
      location.reload();
    });
    window.addEventListener('load', () => {
      navigator.serviceWorker.register('/sw.js')
        .then((registration) => {
          console.log('SW registered:', registration.scope);
        })
        .catch((error) => {
          console.log('SW registration failed:', error);
        });
    });
  }
}
