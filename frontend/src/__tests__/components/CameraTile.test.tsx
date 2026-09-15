import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, screen } from '@testing-library/react';
import { render } from '../utils';
import { CameraTile } from '../../components/CameraTile';

// The shared render() util mounts AuthProvider, which fires an async
// /auth/me probe on mount. Each test absorbs that settle with a single
// `await act(async () => {})` after render so the AuthProvider state
// update doesn't bleed into the assertion phase as an act() warning.
async function flushMicrotasks() {
  await act(async () => {
    await Promise.resolve();
  });
}

describe('CameraTile', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(global, 'fetch').mockResolvedValue(new Response(null, { status: 200 }));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  // Live mode no longer hands the stream URL to <img src>: the MJPEG player
  // fetches it and paints blob frames (see utils/mjpegPlayer.ts), so the
  // request the player makes is what identifies the stream.
  it('requests the live stream in live mode', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    render(
      <CameraTile
        printerId={42}
        printerName="X1C-Lab"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    const streamCalls = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes('/api/v1/printers/42/camera/stream'));
    expect(streamCalls.length).toBeGreaterThan(0);
    expect(streamCalls[0]).toContain('fps=8');
  });

  it('renders the snapshot URL and refreshes on the interval', async () => {
    render(
      <CameraTile
        printerId={7}
        printerName="P1S-Garage"
        mode="snapshot"
        snapshotIntervalMs={1000}
        connected
      />,
    );
    await flushMicrotasks();
    const initial = (screen.getByAltText('P1S-Garage') as HTMLImageElement).src;
    expect(initial).toContain('/api/v1/printers/7/camera/snapshot');

    await act(async () => {
      vi.advanceTimersByTime(1500);
    });
    const refreshed = (screen.getByAltText('P1S-Garage') as HTMLImageElement).src;
    expect(refreshed).toContain('/api/v1/printers/7/camera/snapshot');
    expect(refreshed).not.toBe(initial);
  });

  it('shows an offline placeholder when not connected', async () => {
    render(
      <CameraTile
        printerId={1}
        printerName="A1-Offline"
        mode="live"
        snapshotIntervalMs={5000}
        connected={false}
      />,
    );
    await flushMicrotasks();
    expect(screen.queryByAltText('A1-Offline')).toBeNull();
  });

  it('shows the paused placeholder in paused mode', async () => {
    render(
      <CameraTile
        printerId={9}
        printerName="H2D-Booth"
        mode="paused"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    expect(screen.queryByAltText('H2D-Booth')).toBeNull();
  });

  it('POSTs /camera/stop when leaving live mode', async () => {
    const fetchMock = vi.spyOn(global, 'fetch').mockResolvedValue(
      new Response(null, { status: 200 }),
    );
    const { rerender } = render(
      <CameraTile
        printerId={11}
        printerName="X1C-Stop"
        mode="live"
        snapshotIntervalMs={5000}
        connected
      />,
    );
    await flushMicrotasks();
    fetchMock.mockClear();

    await act(async () => {
      rerender(
        <CameraTile
          printerId={11}
          printerName="X1C-Stop"
          mode="snapshot"
          snapshotIntervalMs={5000}
          connected
        />,
      );
    });

    const stopCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes('/api/v1/printers/11/camera/stop'),
    );
    expect(stopCalls.length).toBeGreaterThan(0);
  });
});
