/**
 * The lifecycle half of the MJPEG player: frames reaching the <img>, and the
 * fetch being aborted when the view stops.
 *
 * The abort matters beyond tidiness — the backend keeps the upstream camera
 * connection open for exactly as long as the response body is being read, so a
 * viewer that unmounts without aborting pins a camera nobody is watching.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';
import { useMjpegStream } from '../../hooks/useMjpegStream';

const BOUNDARY = 'frameboundary';
const encoder = new TextEncoder();

function part(body: string): Uint8Array {
  return encoder.encode(`--${BOUNDARY}\r\nContent-Type: image/jpeg\r\n\r\n${body}\r\n`);
}

/**
 * A stream that keeps producing frames until its reader gives up.
 *
 * The delay is load-bearing: without it `read()` resolves in a microtask every
 * time and the player's read loop starves the event loop, so no timer — and
 * therefore no `waitFor` — ever runs.
 */
function endlessStream(onCancel: () => void): ReadableStream<Uint8Array> {
  let n = 0;
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      await new Promise((resolve) => setTimeout(resolve, 5));
      n += 1;
      controller.enqueue(part(`FRAME-${n}`));
    },
    cancel: onCancel,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useMjpegStream', () => {
  it('returns "" while stopped and never fetches', () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    const { result } = renderHook(() => useMjpegStream(null));
    expect(result.current).toBe('');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('exposes the current frame as a blob URL', async () => {
    const cancelled = vi.fn();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(endlessStream(cancelled), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      }),
    );

    const { result, unmount } = renderHook(() =>
      useMjpegStream('api/v1/printers/1/camera/stream'),
    );
    await waitFor(() => expect(result.current).toMatch(/^blob:/));
    act(() => unmount());
  });

  it('aborts the fetch when the view unmounts', async () => {
    const cancelled = vi.fn();
    let capturedSignal: AbortSignal | undefined;
    vi.spyOn(globalThis, 'fetch').mockImplementation((_url, init) => {
      capturedSignal = (init as RequestInit | undefined)?.signal ?? undefined;
      return Promise.resolve(
        new Response(endlessStream(cancelled), {
          status: 200,
          headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
        }),
      );
    });

    const { result, unmount } = renderHook(() =>
      useMjpegStream('api/v1/printers/1/camera/stream'),
    );
    await waitFor(() => expect(result.current).toMatch(/^blob:/));
    expect(capturedSignal?.aborted).toBe(false);

    act(() => unmount());

    expect(capturedSignal?.aborted).toBe(true);
    // The body is told to stop producing, which is the backend's signal that
    // nobody is watching this camera any more.
    await waitFor(() => expect(cancelled).toHaveBeenCalled());
  });

  it('reports a failed stream through onError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 403 }));
    const onError = vi.fn();
    const { unmount } = renderHook(() =>
      useMjpegStream('api/v1/printers/1/camera/stream', { onError }),
    );
    await waitFor(() => expect(onError).toHaveBeenCalled());
    act(() => unmount());
  });

  it('does not open the stream while parked (active: false)', () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch');
    renderHook(() => useMjpegStream('api/v1/printers/1/camera/stream', { active: false }));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

/**
 * The still-image fallback.
 *
 * Reading a stream ourselves assumes fetch hands the body over as it arrives.
 * iOS' WKWebView — which is what the Home Assistant Companion app renders the
 * panel in — buffers a response that never ends, and so does a reverse proxy
 * with response buffering on. In both cases the fetch succeeds and then simply
 * never yields, which no error path catches. These tests pin the way out.
 */
describe('useMjpegStream falling back to snapshots', () => {
  const STREAM = 'api/v1/printers/1/camera/stream';
  const SNAPSHOT = 'api/v1/printers/1/camera/snapshot?t=1';

  /** A response that resolves but whose body never produces a chunk. */
  function silentStream(): ReadableStream<Uint8Array> {
    return new ReadableStream<Uint8Array>({
      pull() {
        return new Promise<void>(() => {});
      },
    });
  }

  function snapshotResponse(): Response {
    return new Response(new Blob(['STILL'], { type: 'image/jpeg' }), { status: 200 });
  }

  /**
   * fetch stand-in that refuses an aborted signal, the way the real one does.
   *
   * Load-bearing: the first version of this fallback polled with the stream's
   * own AbortSignal, which had just been aborted to drop the stream. Every poll
   * failed before it was sent. A mock that ignores the signal calls that a pass.
   */
  function fetchStub(handler: (url: string) => Response) {
    return vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const signal = (init as RequestInit | undefined)?.signal;
      if (signal?.aborted) {
        return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'));
      }
      return Promise.resolve(handler(String(input)));
    });
  }

  it('polls the snapshot endpoint when no frame ever arrives', async () => {
    const calls: string[] = [];
    fetchStub((url) => {
      calls.push(url);
      if (url.startsWith(SNAPSHOT)) return snapshotResponse();
      return new Response(silentStream(), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      });
    });

    const { result, unmount } = renderHook(() =>
      useMjpegStream(STREAM, { snapshotUrl: SNAPSHOT, firstFrameTimeoutMs: 20 }),
    );

    await waitFor(() => expect(result.current).toMatch(/^blob:/), { timeout: 3000 });
    expect(calls.some((url) => url.startsWith(SNAPSHOT))).toBe(true);
    act(() => unmount());
  });

  it('keeps polling, so the picture goes on updating', async () => {
    let snapshots = 0;
    fetchStub((url) => {
      if (url.startsWith(SNAPSHOT)) {
        snapshots += 1;
        return snapshotResponse();
      }
      return new Response(silentStream(), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      });
    });

    const { unmount } = renderHook(() =>
      useMjpegStream(STREAM, {
        snapshotUrl: SNAPSHOT,
        firstFrameTimeoutMs: 20,
        fallbackIntervalMs: 10,
      }),
    );

    await waitFor(() => expect(snapshots).toBeGreaterThan(2), { timeout: 3000 });
    act(() => unmount());
  });

  it('each poll is cache-busted — a WebView would serve one still forever', async () => {
    const snapshotCalls: string[] = [];
    fetchStub((url) => {
      if (url.startsWith(SNAPSHOT)) {
        snapshotCalls.push(url);
        return snapshotResponse();
      }
      return new Response(silentStream(), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      });
    });

    const { unmount } = renderHook(() =>
      useMjpegStream(STREAM, {
        snapshotUrl: SNAPSHOT,
        firstFrameTimeoutMs: 20,
        fallbackIntervalMs: 10,
      }),
    );

    await waitFor(() => expect(snapshotCalls.length).toBeGreaterThan(1), { timeout: 3000 });
    expect(new Set(snapshotCalls).size).toBe(snapshotCalls.length);
    act(() => unmount());
  });

  it('falls back instead of reporting an error when the stream never worked', async () => {
    fetchStub((url) => (url.startsWith(SNAPSHOT) ? snapshotResponse() : new Response(null, { status: 502 })));
    const onError = vi.fn();

    const { result, unmount } = renderHook(() =>
      useMjpegStream(STREAM, { snapshotUrl: SNAPSHOT, onError, fallbackIntervalMs: 10 }),
    );

    await waitFor(() => expect(result.current).toMatch(/^blob:/), { timeout: 3000 });
    // A camera the user can see is not an error to report — the caller's
    // reconnect logic would only tear the working fallback down again.
    expect(onError).not.toHaveBeenCalled();
    act(() => unmount());
  });

  it('still reports an error when there is no snapshot URL to fall back to', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 502 }));
    const onError = vi.fn();
    const { unmount } = renderHook(() => useMjpegStream(STREAM, { onError }));
    await waitFor(() => expect(onError).toHaveBeenCalled());
    act(() => unmount());
  });

  it('a working stream never touches the snapshot endpoint', async () => {
    const calls: string[] = [];
    fetchStub((url) => {
      calls.push(url);
      return new Response(endlessStream(() => {}), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      });
    });

    const { result, unmount } = renderHook(() =>
      useMjpegStream(STREAM, { snapshotUrl: SNAPSHOT, firstFrameTimeoutMs: 50 }),
    );
    await waitFor(() => expect(result.current).toMatch(/^blob:/));
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(calls.filter((url) => url.startsWith(SNAPSHOT))).toEqual([]);
    act(() => unmount());
  });

  it('stops polling when the view goes away', async () => {
    let snapshots = 0;
    fetchStub((url) => {
      if (url.startsWith(SNAPSHOT)) {
        snapshots += 1;
        return snapshotResponse();
      }
      return new Response(silentStream(), {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      });
    });

    const { unmount } = renderHook(() =>
      useMjpegStream(STREAM, {
        snapshotUrl: SNAPSHOT,
        firstFrameTimeoutMs: 20,
        fallbackIntervalMs: 10,
      }),
    );
    await waitFor(() => expect(snapshots).toBeGreaterThan(0), { timeout: 3000 });
    act(() => unmount());

    const afterUnmount = snapshots;
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(snapshots).toBe(afterUnmount);
  });
});
