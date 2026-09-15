/**
 * Read an MJPEG (``multipart/x-mixed-replace``) stream ourselves instead of
 * handing its URL to ``<img src>``.
 *
 * Why this exists: Home Assistant's ingress proxy rebuilds the response
 * ``Content-Type`` from its base type only — ``content_type.partition(";")[0]``
 * — which throws away the ``boundary=`` parameter. A browser handed a
 * ``multipart/x-mixed-replace`` response with no boundary cannot split it, so a
 * plain ``<img>`` shows nothing at all in the HA sidebar panel while the exact
 * same URL works fine on direct access. Reading the stream here means we can
 * recover the boundary from the ``X-Bambuddy-Boundary`` header the backend
 * sends alongside it, and hand the ``<img>`` one decoded JPEG at a time.
 *
 * The read must keep running for the stream to stay alive: the backend holds
 * the upstream camera connection open only while the response body is being
 * consumed. Aborting the fetch is therefore the way a viewer says "stop".
 */

/** Header the backend duplicates the multipart boundary into (see above). */
export const BOUNDARY_HEADER = 'X-Bambuddy-Boundary';

/**
 * The multipart boundary for a stream response, or null if it has none.
 *
 * ``Content-Type`` first because that is the standard place and the only one
 * that survives a direct (non-proxied) request unchanged; the header is the
 * fallback for proxies that strip parameters.
 */
export function boundaryFromResponse(response: Response): string | null {
  const contentType = response.headers.get('Content-Type') ?? '';
  const match = /boundary=(?:"([^"]+)"|([^;\s]+))/i.exec(contentType);
  const fromContentType = match?.[1] ?? match?.[2];
  if (fromContentType) return fromContentType;
  const fromHeader = response.headers.get(BOUNDARY_HEADER);
  return fromHeader ? fromHeader.trim().replace(/^--/, '') || null : null;
}

export interface MjpegStreamOptions {
  /** Aborts the fetch, which is what releases the upstream camera. */
  signal: AbortSignal;
  /** One decoded JPEG frame. Ownership of the blob passes to the caller. */
  onFrame: (frame: Blob) => void;
  /** Called once when the stream ends for any reason other than an abort. */
  onError?: (error: unknown) => void;
  /** Extra request headers, e.g. a bearer token. */
  headers?: Record<string, string>;
}

const encoder = new TextEncoder();

// ArrayBufferLike, not the ArrayBuffer default: the chunks a ReadableStream
// reader hands back are not guaranteed to be backed by a plain ArrayBuffer.
type Bytes = Uint8Array<ArrayBufferLike>;

/** Index of `needle` in `haystack` at or after `from`, or -1. */
function indexOfBytes(haystack: Bytes, needle: Bytes, from: number): number {
  outer: for (let i = from; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

function concat(a: Bytes, b: Bytes): Bytes {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/**
 * Fetch an MJPEG stream and deliver its frames until it ends or is aborted.
 *
 * Resolves when the stream is done. An abort resolves quietly — it is the
 * normal way a viewer stops — anything else goes to ``onError`` first.
 */
export async function playMjpegStream(url: string, options: MjpegStreamOptions): Promise<void> {
  const { signal, onFrame, onError, headers } = options;
  try {
    const response = await fetch(url, { signal, headers, credentials: 'same-origin' });
    if (!response.ok) {
      throw new Error(`camera stream failed: HTTP ${response.status}`);
    }
    const boundary = boundaryFromResponse(response);
    if (!boundary) {
      // Without a boundary there is nothing to split on. Failing loudly beats
      // showing a frozen frame: the caller's error path retries/reconnects.
      throw new Error('camera stream has no multipart boundary');
    }
    if (!response.body) {
      throw new Error('camera stream has no body');
    }

    const delimiter = encoder.encode(`--${boundary}`);
    const headerEnd = encoder.encode('\r\n\r\n');
    const reader = response.body.getReader();
    let buffer: Bytes = new Uint8Array(0);

    try {
      for (;;) {
        // Checked before every read as well as relying on fetch to reject the
        // pending one: an abort must stop frames reaching a view that has gone
        // away, even if the body itself is slow to notice.
        if (signal.aborted) break;
        const { done, value } = await reader.read();
        if (done || signal.aborted) break;
        if (value) buffer = concat(buffer, value);

        // Drain every complete part currently in the buffer. A part is
        // complete once the *next* delimiter has arrived, which is also what
        // tells us where its body ends — Content-Length is optional on these
        // and several camera firmwares omit it.
        for (;;) {
          const start = indexOfBytes(buffer, delimiter, 0);
          if (start < 0) break;
          const bodyStart = indexOfBytes(buffer, headerEnd, start + delimiter.length);
          if (bodyStart < 0) break;
          const next = indexOfBytes(buffer, delimiter, bodyStart + headerEnd.length);
          if (next < 0) break;

          const frameStart = bodyStart + headerEnd.length;
          // The CRLF before the delimiter belongs to the delimiter, not the
          // JPEG. Leaving it on corrupts nothing visually but does mean the
          // blob is not a byte-exact image.
          let frameEnd = next;
          if (frameEnd >= frameStart + 2 && buffer[frameEnd - 2] === 13 && buffer[frameEnd - 1] === 10) {
            frameEnd -= 2;
          }
          if (frameEnd > frameStart) {
            onFrame(new Blob([buffer.slice(frameStart, frameEnd)], { type: 'image/jpeg' }));
          }
          buffer = buffer.slice(next);
        }
      }
    } finally {
      // Cancel, not just releaseLock: this is what tells the body to stop
      // producing, which is how the backend learns nobody is watching and
      // lets go of the upstream camera.
      try {
        await reader.cancel();
      } catch {
        // Already torn down by the abort; nothing left to cancel.
      }
    }
  } catch (error) {
    if (signal.aborted) return;
    onError?.(error);
  }
}
