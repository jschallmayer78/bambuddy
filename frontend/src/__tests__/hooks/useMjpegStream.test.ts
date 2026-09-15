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
