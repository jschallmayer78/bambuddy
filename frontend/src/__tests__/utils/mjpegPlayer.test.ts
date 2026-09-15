/**
 * The MJPEG reader that replaced `<img src=".../camera/stream">`.
 *
 * The case that forced it exists is the first one below: Home Assistant's
 * ingress proxy rebuilds Content-Type from its base type and drops the
 * `boundary=` parameter, so the boundary has to come from the
 * X-Bambuddy-Boundary header instead.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { boundaryFromResponse, playMjpegStream, BOUNDARY_HEADER } from '../../utils/mjpegPlayer';

const BOUNDARY = 'frameboundary';
const encoder = new TextEncoder();

function part(body: string): Uint8Array {
  return encoder.encode(
    `--${BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: ${body.length}\r\n\r\n${body}\r\n`,
  );
}

/** A response whose body yields `chunks` one read at a time. */
function streamResponse(chunks: Uint8Array[], headers: Record<string, string>): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(chunk));
      controller.close();
    },
  });
  return new Response(body, { status: 200, headers });
}

/** jsdom's Blob has no .text(), so read it the long way. */
function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('boundaryFromResponse', () => {
  it('reads the boundary from Content-Type when it survived the proxy', () => {
    const response = new Response(null, {
      headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
    });
    expect(boundaryFromResponse(response)).toBe(BOUNDARY);
  });

  it('unquotes a quoted boundary', () => {
    const response = new Response(null, {
      headers: { 'Content-Type': `multipart/x-mixed-replace; boundary="${BOUNDARY}"` },
    });
    expect(boundaryFromResponse(response)).toBe(BOUNDARY);
  });

  it('falls back to the header when ingress stripped the parameter', () => {
    // Exactly what HA sends through: content_type.partition(";")[0].
    const response = new Response(null, {
      headers: {
        'Content-Type': 'multipart/x-mixed-replace',
        [BOUNDARY_HEADER]: BOUNDARY,
      },
    });
    expect(boundaryFromResponse(response)).toBe(BOUNDARY);
  });

  it('tolerates a header value written with the leading dashes', () => {
    const response = new Response(null, {
      headers: { 'Content-Type': 'multipart/x-mixed-replace', [BOUNDARY_HEADER]: `--${BOUNDARY}` },
    });
    expect(boundaryFromResponse(response)).toBe(BOUNDARY);
  });

  it('is null when there is no boundary anywhere', () => {
    expect(boundaryFromResponse(new Response(null))).toBeNull();
  });
});

describe('playMjpegStream', () => {
  it('delivers one blob per frame', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([part('JPEG-ONE'), part('JPEG-TWO'), encoder.encode(`--${BOUNDARY}--\r\n`)], {
        'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      }),
    );

    const frames: Blob[] = [];
    const onError = vi.fn();
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: new AbortController().signal,
      onFrame: (frame) => frames.push(frame),
      onError,
    });

    expect(onError).not.toHaveBeenCalled();
    expect(frames).toHaveLength(2);
    expect(frames[0].type).toBe('image/jpeg');
    await expect(blobText(frames[0])).resolves.toBe('JPEG-ONE');
    await expect(blobText(frames[1])).resolves.toBe('JPEG-TWO');
  });

  it('works when only the X-Bambuddy-Boundary header carries the boundary', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([part('VIA-HEADER'), encoder.encode(`--${BOUNDARY}--\r\n`)], {
        'Content-Type': 'multipart/x-mixed-replace',
        [BOUNDARY_HEADER]: BOUNDARY,
      }),
    );

    const frames: Blob[] = [];
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: new AbortController().signal,
      onFrame: (frame) => frames.push(frame),
    });

    expect(frames).toHaveLength(1);
    await expect(blobText(frames[0])).resolves.toBe('VIA-HEADER');
  });

  it('reassembles a frame split across chunk boundaries', async () => {
    const whole = part('SPLIT-FRAME');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([whole.slice(0, 12), whole.slice(12), encoder.encode(`--${BOUNDARY}--\r\n`)], {
        'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}`,
      }),
    );

    const frames: Blob[] = [];
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: new AbortController().signal,
      onFrame: (frame) => frames.push(frame),
    });

    expect(frames).toHaveLength(1);
    await expect(blobText(frames[0])).resolves.toBe('SPLIT-FRAME');
  });

  it('reports a stream with no boundary at all rather than hanging', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      streamResponse([part('IGNORED')], { 'Content-Type': 'multipart/x-mixed-replace' }),
    );

    const onError = vi.fn();
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: new AbortController().signal,
      onFrame: () => {},
      onError,
    });

    expect(onError).toHaveBeenCalledTimes(1);
    expect(String(onError.mock.calls[0][0])).toContain('boundary');
  });

  it('reports a non-OK response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 401 }));
    const onError = vi.fn();
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: new AbortController().signal,
      onFrame: () => {},
      onError,
    });
    expect(String(onError.mock.calls[0][0])).toContain('401');
  });

  it('stops reading when aborted, and does not report the abort as an error', async () => {
    const controller = new AbortController();
    let pulls = 0;
    // Endless stream: only the abort can end this read.
    const body = new ReadableStream<Uint8Array>({
      pull(streamController) {
        pulls += 1;
        streamController.enqueue(part(`FRAME-${pulls}`));
        if (pulls >= 2) controller.abort();
      },
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body, {
        status: 200,
        headers: { 'Content-Type': `multipart/x-mixed-replace; boundary=${BOUNDARY}` },
      }),
    );

    const onError = vi.fn();
    const frames: Blob[] = [];
    await playMjpegStream('api/v1/printers/1/camera/stream', {
      signal: controller.signal,
      onFrame: (frame) => frames.push(frame),
      onError,
    });

    expect(onError).not.toHaveBeenCalled();
    // The read stopped: an unbounded source would otherwise still be pulling.
    const pullsAtAbort = pulls;
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(pulls).toBe(pullsAtAbort);
  });
});
