import { useEffect, useRef, useState } from 'react';
import { playMjpegStream } from '../utils/mjpegPlayer';

/** How long to wait for the first streamed frame before falling back. */
const FIRST_FRAME_TIMEOUT_MS = 6000;
/** Poll interval once fallen back. Roughly one frame a second is plenty for a tile. */
const FALLBACK_INTERVAL_MS = 1000;
/** Consecutive failed polls before the caller is told the camera is broken. */
const FALLBACK_FAILURES_BEFORE_ERROR = 3;

/**
 * Render an MJPEG camera stream into an ``<img>`` one blob frame at a time,
 * falling back to still images where streaming does not work.
 *
 * See ``utils/mjpegPlayer.ts`` for why the browser can't be trusted to decode
 * the stream itself behind Home Assistant's ingress proxy. Every live-camera
 * view in the app goes through this hook so there is exactly one copy of the
 * lifecycle rules below.
 *
 * **Why the fallback exists.** Reading the stream ourselves means relying on
 * ``fetch`` delivering the body incrementally, and two things in the wild do
 * not: iOS' WKWebView — which is what the Home Assistant Companion app renders
 * the panel in — buffers a response that never ends, so not a single chunk
 * arrives; and a reverse proxy with response buffering switched on does the
 * same to every client behind it. Both look identical from here: the fetch
 * succeeds and then nothing happens, forever. So if no frame has arrived after
 * :data:`FIRST_FRAME_TIMEOUT_MS`, the stream is dropped and the snapshot
 * endpoint is polled instead. Those are ordinary one-shot image requests, which
 * work everywhere; the cost is a frame a second instead of a smooth feed.
 *
 * The fallback is per-view and sticky: once a view has switched, it keeps
 * polling until it is rebuilt (the tile is reopened, the URL changes, the tab
 * becomes visible again). Retrying the stream every few seconds on a client
 * that structurally cannot do it would just stall the picture each time.
 *
 * Returns the object URL of the newest frame, or ``''`` before the first one
 * arrives (and while the stream is stopped). Callers should not render the
 * ``<img>`` at all with an empty src — an empty ``src`` attribute makes the
 * browser re-request the page itself and fire ``error``.
 */
export function useMjpegStream(
  /** Stream URL, or null to stop. */
  url: string | null,
  options: {
    /** False parks the stream without unmounting — modal closed, tile paused. */
    active?: boolean;
    /** Called when the stream ends for any reason other than a deliberate stop. */
    onError?: (error: unknown) => void;
    /**
     * Single-frame endpoint to poll when streaming produces nothing. Without
     * it a client that cannot stream simply shows no picture, which is the
     * behaviour this option exists to avoid — pass it wherever you can.
     */
    snapshotUrl?: string | null;
    /** Poll interval for the fallback. */
    fallbackIntervalMs?: number;
    /** How long to wait for the first streamed frame. */
    firstFrameTimeoutMs?: number;
  } = {},
): string {
  const {
    active = true,
    onError,
    snapshotUrl = null,
    fallbackIntervalMs = FALLBACK_INTERVAL_MS,
    firstFrameTimeoutMs = FIRST_FRAME_TIMEOUT_MS,
  } = options;
  const [frameUrl, setFrameUrl] = useState('');

  // Held in a ref so an inline arrow from the caller doesn't restart the stream
  // on every render — restarting means dropping and re-opening the camera.
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  // Same reason: a caller that rebuilds the snapshot URL string on every render
  // (they all do — it carries a cache-buster) must not tear the view down.
  const snapshotUrlRef = useRef(snapshotUrl);
  snapshotUrlRef.current = snapshotUrl;

  // A hidden tab keeps the fetch — and therefore the upstream camera
  // connection on the backend — alive for nothing. Track visibility as state so
  // the stream effect below tears down and rebuilds with it.
  const [visible, setVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  );
  useEffect(() => {
    const onVisibilityChange = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => document.removeEventListener('visibilitychange', onVisibilityChange);
  }, []);

  useEffect(() => {
    if (!url || !active || !visible) {
      setFrameUrl('');
      return;
    }

    // Two controllers, not one. Dropping the stream to fall back means
    // aborting it — and a fallback poll that shared that signal would be
    // aborted before it was ever sent, which looks exactly like a camera that
    // produces nothing at all.
    const streamController = new AbortController();
    const pollController = new AbortController();
    // Blob URLs are revoked one frame behind: revoking the one the <img> is
    // currently displaying makes the image blink to nothing before the next
    // frame decodes.
    let displayed: string | null = null;
    let previous: string | null = null;
    let frames = 0;
    let failures = 0;
    let stopped = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let firstFrameTimer: ReturnType<typeof setTimeout> | null = null;

    const show = (blob: Blob) => {
      if (stopped) return;
      frames += 1;
      const next = URL.createObjectURL(blob);
      if (previous) URL.revokeObjectURL(previous);
      previous = displayed;
      displayed = next;
      setFrameUrl(next);
    };

    const pollSnapshots = (reason: string) => {
      if (stopped || pollTimer !== null) return;
      const snapshot = snapshotUrlRef.current;
      if (!snapshot) return;
      // One line, once per view, so a support log says which path a picture
      // came from. Not a user-facing warning: the picture is fine, just slower.
      console.info(`camera: streaming unavailable (${reason}); falling back to snapshots`);

      const tick = async () => {
        pollTimer = null;
        if (stopped) return;
        try {
          // The cache-buster matters: without it a WebView happily serves the
          // same still from its cache for as long as the view is open.
          const separator = snapshot.includes('?') ? '&' : '?';
          const response = await fetch(`${snapshot}${separator}_=${Date.now()}`, {
            signal: pollController.signal,
            credentials: 'same-origin',
          });
          if (response.ok) {
            failures = 0;
            show(await response.blob());
          } else {
            // A camera that cannot produce a still answers 503 here, every
            // second, and dropping that on the floor is how this path used to
            // fail: an empty tile, no error, nothing in the console to say the
            // fallback had even engaged. Report the first one, then let the
            // caller show its own error state once it is clearly not a blip —
            // a single failed grab while the printer wakes its camera is.
            failures += 1;
            if (failures === 1) {
              console.warn(`camera: snapshot fallback got HTTP ${response.status}`);
            }
            if (failures === FALLBACK_FAILURES_BEFORE_ERROR) {
              onErrorRef.current?.(new Error(`Snapshot request failed (HTTP ${response.status})`));
            }
          }
        } catch (error) {
          if (!stopped && !pollController.signal.aborted) onErrorRef.current?.(error);
        }
        if (!stopped) pollTimer = setTimeout(() => void tick(), fallbackIntervalMs);
      };
      void tick();
    };

    // The stream may succeed and then deliver nothing at all — see the note on
    // WKWebView above. Nothing reports that as an error, so it is timed.
    firstFrameTimer = setTimeout(() => {
      firstFrameTimer = null;
      if (frames === 0 && !stopped) {
        streamController.abort();
        pollSnapshots('no frame arrived');
      }
    }, firstFrameTimeoutMs);

    void playMjpegStream(url, {
      signal: streamController.signal,
      onFrame: show,
      onError: (error) => {
        if (stopped) return;
        if (frames === 0 && snapshotUrlRef.current) {
          // Never showed anything, so there is nothing for the caller to
          // reconnect around: take the still-image path instead of reporting
          // a broken camera.
          pollSnapshots(error instanceof Error ? error.message : 'stream failed');
          return;
        }
        onErrorRef.current?.(error);
      },
    });

    return () => {
      stopped = true;
      if (firstFrameTimer !== null) clearTimeout(firstFrameTimer);
      if (pollTimer !== null) clearTimeout(pollTimer);
      // Aborting is what tells the backend to let go of the camera; it stops
      // reading the body, which is the only signal it has.
      streamController.abort();
      pollController.abort();
      if (previous) URL.revokeObjectURL(previous);
      if (displayed) URL.revokeObjectURL(displayed);
      setFrameUrl('');
    };
  }, [url, active, visible, fallbackIntervalMs, firstFrameTimeoutMs]);

  return frameUrl;
}
