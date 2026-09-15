/**
 * No new absolute same-origin URLs.
 *
 * Bambuddy ships as a Home Assistant add-on, which serves it under a
 * per-session ingress prefix (``/api/hassio_ingress/<token>/``) that is unknown
 * at build time and changes between sessions. A hard-coded ``/api/v1/...`` or
 * ``/img/...`` is proxied to nothing there, and the same build has to keep
 * working on its own port — so every same-origin URL the app builds goes
 * through ``utils/basePath.ts`` (``appPath`` / ``appUrl`` / ``appWsUrl``), and
 * every URL in a document or stylesheet is written relative.
 *
 * The failure this prevents is quiet: on direct access an absolute path is
 * indistinguishable from a correct one, so a regression only shows up in the HA
 * sidebar, usually as an image that doesn't load or a request that 404s.
 *
 * *Router* paths are deliberately not covered: ``<Link to="/queue">`` and
 * ``navigate('/queue')`` are correct as written, because BrowserRouter's
 * basename adds and strips the prefix. Only URLs that leave the router — fetch,
 * <img>, window.open, window.location — have to carry it themselves.
 *
 * If you add a genuinely-absolute URL, add it to ALLOWLIST with the reason.
 */

import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(__dirname, '..');
const FRONTEND = path.resolve(__dirname, '../..');

/** Same-origin resource roots the browser will fetch. */
const RESOURCE_ROOTS =
  '(?:api|assets|img|icons|fonts|static|bed-icons|health|manifest\\.json|sw\\.js|sw-register\\.js)';

/**
 * A quoted string literal (', " or `) that starts with one of the roots above.
 * Deliberately narrow: it should be obvious from the match alone why a line is
 * flagged.
 */
const ABSOLUTE_RESOURCE = new RegExp(`['"\`]/${RESOURCE_ROOTS}(?:[/?'"\`]|$)`);

/**
 * Navigation that bypasses the router and therefore needs the prefix applied by
 * hand — `window.open('/camera/1')`, `location.href = '/login'`.
 */
const RAW_NAVIGATION = /(?:window\.open\(|location\.(?:href\s*=|assign\(|replace\())\s*['"`]\//;

/** CSS and HTML attributes, which the `<base>` tag governs only when relative. */
const ABSOLUTE_MARKUP = /(?:src|href)="\/|url\(['"]?\//;

interface Allowed {
  file: string;
  why: string;
}

const ALLOWLIST: Allowed[] = [
  {
    file: 'public/sw.js',
    why:
      'The service worker is only ever registered when BASE_PATH is "/" ' +
      '(see public/sw-register.js), so its precache list is absolute by ' +
      'definition. Under ingress no worker is registered at all.',
  },
  {
    file: 'public/sw-register.js',
    why:
      "register('/sw.js') sits inside the BASE_PATH === '/' branch — the whole " +
      'point of which is that no worker is registered under an ingress prefix.',
  },
];

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue;
      walk(full, out);
    } else if (/\.(ts|tsx|css)$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

function isAllowed(relative: string): boolean {
  return ALLOWLIST.some((entry) => entry.file === relative);
}

/** `file:line: text` for every line matching one of the patterns above. */
function offenders(files: string[], patterns: RegExp[], root: string): string[] {
  const hits: string[] = [];
  for (const file of files) {
    const relative = path.relative(root, file).replaceAll(path.sep, '/');
    if (isAllowed(relative)) continue;
    const lines = readFileSync(file, 'utf8').split('\n');
    lines.forEach((line, index) => {
      // Skip comments: prose in this repo quotes paths constantly, and a
      // comment is not a URL the browser will ever request.
      const trimmed = line.trim();
      if (trimmed.startsWith('//') || trimmed.startsWith('*') || trimmed.startsWith('/*')) return;
      if (patterns.some((pattern) => pattern.test(line))) {
        hits.push(`${relative}:${index + 1}: ${trimmed}`);
      }
    });
  }
  return hits;
}

describe('no absolute same-origin URLs', () => {
  it('builds every same-origin URL in src/ through utils/basePath', () => {
    // src/__tests__ is excluded: those files assert on the *output* of
    // appPath, which is '/img/printers/x1c.png' in the direct-access mode the
    // suite runs in. Flagging expected values would be noise.
    const files = walk(SRC).filter((file) => !file.includes(`${path.sep}__tests__${path.sep}`));
    expect(offenders(files, [ABSOLUTE_RESOURCE, RAW_NAVIGATION, ABSOLUTE_MARKUP], FRONTEND)).toEqual(
      [],
    );
  });

  it('keeps index.html and the public/ assets relative', () => {
    const files = ['index.html', 'public/manifest.json', 'public/sw-register.js'].map((file) =>
      path.join(FRONTEND, file),
    );
    const hits: string[] = [];
    for (const file of files) {
      const relative = path.relative(FRONTEND, file).replaceAll(path.sep, '/');
      if (isAllowed(relative)) continue;
      readFileSync(file, 'utf8')
        .split('\n')
        .forEach((line, index) => {
          const trimmed = line.trim();
          // <base href="/"> is the one absolute URL that must stay: it is what
          // the server rewrites per request, and what everything else resolves
          // against.
          if (trimmed.startsWith('<base ')) return;
          if (trimmed.startsWith('//') || trimmed.startsWith('<!--') || trimmed.startsWith('*')) {
            return;
          }
          if (ABSOLUTE_MARKUP.test(line) || ABSOLUTE_RESOURCE.test(line)) {
            hits.push(`${relative}:${index + 1}: ${trimmed}`);
          }
        });
    }
    expect(hits).toEqual([]);
  });

  // Only meaningful after `npm run build`; a bare checkout has no static/.
  const BUILT_INDEX = path.resolve(FRONTEND, '../static/index.html');
  it.skipIf(!existsSync(BUILT_INDEX))('emits relative asset URLs in the built index.html', () => {
    const html = readFileSync(BUILT_INDEX, 'utf8');
    // The whole point of vite's `base: './'`. An absolute /assets/... here is
    // a blank page behind ingress.
    expect(html).toMatch(/src="\.\/assets\//);
    expect(html).toMatch(/href="\.\/assets\//);
    expect(html).not.toMatch(/(?:src|href)="\/assets\//);
    // And the tag everything else resolves against must still be there for the
    // server to rewrite.
    expect(html).toMatch(/<base href="\/"/);
  });

  it('is actually looking for something (guards against a dead regex)', () => {
    expect(ABSOLUTE_RESOURCE.test("fetch('/api/v1/printers')")).toBe(true);
    expect(ABSOLUTE_RESOURCE.test("src={'/img/logo.png'}")).toBe(true);
    expect(ABSOLUTE_RESOURCE.test("fetch(appPath('api/v1/printers'))")).toBe(false);
    expect(RAW_NAVIGATION.test("window.open('/camera/1')")).toBe(true);
    expect(RAW_NAVIGATION.test('window.open(appPath(`camera/${id}`))')).toBe(false);
    expect(ABSOLUTE_MARKUP.test('<link rel="manifest" href="/manifest.json" />')).toBe(true);
    expect(ABSOLUTE_MARKUP.test("src: url('/fonts/inter.woff2')")).toBe(true);
    expect(ABSOLUTE_MARKUP.test("src: url('../fonts/inter.woff2')")).toBe(false);
    // Router paths are out of scope — the basename handles them.
    expect(ABSOLUTE_RESOURCE.test("navigate('/queue')")).toBe(false);
  });
});
