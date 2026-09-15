import { useEffect, useRef, useState } from 'react';
import { playMjpegStream } from '../utils/mjpegPlayer';

/**
 * Render an MJPEG camera stream into an ``<img>`` one blob frame at a time.
 *
 * See ``utils/mjpegPlayer.ts`` for why the browser can't be trusted to do this
 * itself behind Home Assistant's ingress proxy. Every live-camera view in the
 * app goes through this hook so there is exactly one copy of the lifecycle
 * rules below.
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
  } = {},
): string {
  const { active = true, onError } = options;
  const [frameUrl, setFrameUrl] = useState('');

  // Held in a ref so an inline arrow from the caller doesn't restart the stream
  // on every render — restarting means dropping and re-opening the camera.
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

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

    const controller = new AbortController();
    // Blob URLs are revoked one frame behind: revoking the one the <img> is
    // currently displaying makes the image blink to nothing before the next
    // frame decodes.
    let displayed: string | null = null;
    let previous: string | null = null;

    void playMjpegStream(url, {
      signal: controller.signal,
      onFrame: (blob) => {
        const next = URL.createObjectURL(blob);
        if (previous) URL.revokeObjectURL(previous);
        previous = displayed;
        displayed = next;
        setFrameUrl(next);
      },
      onError: (error) => onErrorRef.current?.(error),
    });

    return () => {
      // Aborting is what tells the backend to let go of the camera; it stops
      // reading the body, which is the only signal it has.
      controller.abort();
      if (previous) URL.revokeObjectURL(previous);
      if (displayed) URL.revokeObjectURL(displayed);
      setFrameUrl('');
    };
  }, [url, active, visible]);

  return frameUrl;
}
