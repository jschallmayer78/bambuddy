/**
 * The app-root helper, in both modes it has to serve: directly on Bambuddy's
 * own port, and under a Home Assistant ingress prefix.
 */

import { describe, it, expect, vi } from 'vitest';
import { basePathFrom, BASE_PATH, IS_INGRESS, appPath, appUrl, appWsUrl } from '../../utils/basePath';

const INGRESS = '/api/hassio_ingress/aBc123XyZ/';

describe('basePathFrom', () => {
  it('returns "/" for a direct-access base', () => {
    expect(basePathFrom('/')).toBe('/');
    expect(basePathFrom('http://printers.local:8000/')).toBe('/');
  });

  it('returns the ingress prefix with a trailing slash', () => {
    expect(basePathFrom(INGRESS)).toBe(INGRESS);
    expect(basePathFrom(`http://homeassistant.local:8123${INGRESS}`)).toBe(INGRESS);
  });

  it('adds the trailing slash a base tag may be missing', () => {
    expect(basePathFrom('/api/hassio_ingress/aBc123XyZ')).toBe(INGRESS);
  });

  it('falls back to "/" rather than throwing on nonsense', () => {
    expect(basePathFrom('')).toBe('/');
    expect(basePathFrom('http://')).toBe('/');
  });
});

describe('under direct access (the environment these tests run in)', () => {
  it('reports the root as the app base', () => {
    expect(BASE_PATH).toBe('/');
    expect(IS_INGRESS).toBe(false);
  });

  it('builds rooted paths', () => {
    expect(appPath('api/v1/printers')).toBe('/api/v1/printers');
    // A caller that writes the leading slash anyway must not get a doubled one.
    expect(appPath('/api/v1/printers')).toBe('/api/v1/printers');
    expect(appPath('')).toBe('/');
  });

  it('builds absolute URLs on the current origin', () => {
    expect(appUrl('overlay/3')).toBe(`${window.location.origin}/overlay/3`);
  });

  it('builds ws:// URLs on the current host', () => {
    expect(appWsUrl('api/v1/ws')).toBe('ws://localhost:3000/api/v1/ws');
  });
});

describe('under an ingress prefix', () => {
  // The module reads the <base> tag once at import, so a second mode means a
  // second import. resetModules + a tag in the document is how the ingress
  // case gets exercised without a second jsdom environment.
  async function loadUnderIngress() {
    const base = document.createElement('base');
    base.setAttribute('href', INGRESS);
    document.head.appendChild(base);
    try {
      vi.resetModules();
      return await import('../../utils/basePath');
    } finally {
      base.remove();
      vi.resetModules();
    }
  }

  it('prefixes every path it builds', async () => {
    const mod = await loadUnderIngress();
    expect(mod.BASE_PATH).toBe(INGRESS);
    expect(mod.IS_INGRESS).toBe(true);
    expect(mod.appPath('api/v1/printers')).toBe(`${INGRESS}api/v1/printers`);
    expect(mod.appUrl('camwall')).toBe(`${window.location.origin}${INGRESS}camwall`);
  });

  it('carries the prefix into the WebSocket URL', async () => {
    const mod = await loadUnderIngress();
    // Host *and* prefix: HA proxies the socket through the same session path.
    expect(mod.appWsUrl('api/v1/ws')).toBe(`ws://localhost:3000${INGRESS}api/v1/ws`);
  });
});
