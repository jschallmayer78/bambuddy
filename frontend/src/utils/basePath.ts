/**
 * Where the app is mounted, as the browser currently sees it.
 *
 * Bambuddy ships as a Home Assistant add-on, and an add-on with a sidebar panel
 * is reached through the Supervisor's ingress proxy: the whole app hangs off a
 * per-session path like ``/api/hassio_ingress/aBc123…/``. That prefix is not
 * known at build time, it changes between sessions, and the same build is also
 * served directly on its own port. So nothing may hard-code either shape.
 *
 * The server side of the contract (``backend/app/core/ingress.py``) injects
 * ``<base href="…">`` as the first element in ``<head>`` of every HTML
 * response — ``/`` on direct access, the validated ingress prefix behind the
 * proxy. That tag is the single authority for the app root, and this module is
 * the only place that reads it.
 *
 * It is read from the DOM rather than from a server-injected global on purpose:
 * the CSP is a strict ``script-src 'self'``, so there is no inline script to
 * carry one, and there must not be.
 *
 * Why ``appPath()`` returns a rooted path (``/prefix/api/v1``) instead of a
 * bare relative one (``api/v1``): a bare relative URL resolves against
 * ``document.baseURI``, which is only the app root while the ``<base>`` tag is
 * present. Resolving once here, against the *pathname* of that base, gives the
 * same answer in both modes and keeps working at any SPA route depth even if a
 * document ever reaches the browser without the injected tag — the failure
 * mode #1221 was about. Assets emitted by Vite still use plain relative URLs;
 * those are resolved by the browser against ``<base>`` before any of our code
 * runs.
 */

/**
 * The app-root pathname for a given document base URI, always with a trailing
 * slash. Pure and exported so the ingress and direct cases are both testable
 * without a real document.
 */
export function basePathFrom(baseURI: string): string {
  let pathname: string;
  try {
    pathname = new URL(baseURI, 'http://localhost/').pathname;
  } catch {
    // An unparseable base is not worth crashing the app over: '/' is what a
    // document with no base tag resolves to anyway, so direct access still
    // works and ingress degrades visibly rather than silently.
    return '/';
  }
  if (!pathname.startsWith('/')) pathname = `/${pathname}`;
  return pathname.endsWith('/') ? pathname : `${pathname}/`;
}

/**
 * App root, e.g. ``/`` or ``/api/hassio_ingress/aBc123.../``.
 *
 * Read once at module load: the base tag is server-rendered into the document
 * head, so it is in place before any module script executes and cannot change
 * for the lifetime of the document.
 */
export const BASE_PATH: string = basePathFrom(
  // The <base> tag, not document.baseURI: with no base tag in the document,
  // baseURI is the *current page URL*, so a hard refresh on /projects/5 would
  // report an app root of "/projects/5/". index.html ships a literal
  // <base href="/"> which the server rewrites per request, so the tag is
  // always there in dev and in production; '/' is the correct answer for any
  // document that somehow lacks one, since only the server adds a prefix.
  (typeof document !== 'undefined' && document.querySelector('base')?.getAttribute('href')) || '/',
);

/**
 * True when the app is served under a proxy prefix (Home Assistant ingress).
 *
 * Used for the handful of decisions that are not just "prepend the prefix" —
 * notably service-worker registration, which must not happen under a
 * session-scoped path.
 */
export const IS_INGRESS: boolean = BASE_PATH !== '/';

/**
 * Build a same-origin path from one relative to the app root.
 *
 * ``appPath('api/v1/printers')`` → ``/api/v1/printers`` direct,
 * ``/api/hassio_ingress/aBc.../api/v1/printers`` behind ingress.
 */
export function appPath(path: string): string {
  return `${BASE_PATH}${path.replace(/^\/+/, '')}`;
}

/** Same as {@link appPath}, as an absolute URL. For links handed to other apps. */
export function appUrl(path: string): string {
  return `${window.location.origin}${appPath(path)}`;
}

/**
 * A ``ws://`` / ``wss://`` URL for a path relative to the app root.
 *
 * Both the host and the path prefix come from the base, which is what makes
 * this work behind ingress — HA proxies WebSockets through the same per-session
 * prefix as everything else.
 */
export function appWsUrl(path: string): string {
  const url = new URL(appPath(path), window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}
