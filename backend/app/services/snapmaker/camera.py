"""Snapmaker U1 camera.

The U1's stock firmware serves no video stream. Its camera plugin writes a
single JPEG to ``/server/files/camera/monitor.jpg`` and only keeps refreshing
it while something keeps asking: ``camera.start_monitor`` over Moonraker's
WebSocket starts the capture, and the plugin stops on its own once nobody has
asked for a while. So a live view here is a poll loop with a keepalive
attached, not a stream that is opened and read.

Three behaviours of the plugin drive the shape of this module, all of them
observed on real hardware rather than inferred:

* ``start_monitor`` must not be hammered — the plugin misbehaves when it is —
  hence :data:`START_COOLDOWN`.
* After a *cold* start there is no file yet for about a second, so the first
  frame has to be waited for. When frames are already flowing the keepalive
  must NOT wait, or every few frames of a live view stall for over a second.
* ``monitor.jpg`` is briefly absent while the plugin rewrites it, so a 404 is
  a retry, not an error.

Frames come out fresh every 40-180 ms on a healthy printer, which is enough
for a usable several-frames-per-second view.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncGenerator, Callable

from backend.app.services.snapmaker.moonraker import MoonrakerClient, MoonrakerError

logger = logging.getLogger(__name__)

START_COOLDOWN = 5.0  # seconds between repeated start_monitor calls
IDLE_STOP = 60.0  # seconds without a request before stop_monitor
FIRST_FRAME_DELAY = 1.2  # seconds to wait after a COLD start for the first frame
WARM_WINDOW = 10.0  # a frame this recent means the camera is already running
SNAPSHOT_PATH = "/server/files/camera/monitor.jpg"


class _CameraState:
    __slots__ = ("last_start", "last_frame", "stop_task")

    def __init__(self):
        self.last_start = 0.0
        self.last_frame = 0.0
        self.stop_task: asyncio.Task | None = None


# Keyed by base URL rather than by printer id: the keepalive is a property of
# the physical machine, and two Bambuddy code paths looking at the same printer
# (a live tile and a finish photo, say) must share one cooldown.
_states: dict[str, _CameraState] = {}


def _state_for(base_url: str) -> _CameraState:
    state = _states.get(base_url)
    if state is None:
        state = _CameraState()
        _states[base_url] = state
    return state


async def _stop_monitor(client: MoonrakerClient, domain: str) -> None:
    with contextlib.suppress(Exception):
        await client.ws_rpc([(1, "camera.stop_monitor", {"domain": domain})], timeout=5.0)


async def ensure_running(client: MoonrakerClient, *, domain: str = "lan") -> None:
    """Start the camera, or refresh the keepalive if it is already running."""
    state = _state_for(client.base_url)
    now = time.monotonic()

    if now - state.last_start >= START_COOLDOWN:
        state.last_start = now
        warm = (now - state.last_frame) < WARM_WINDOW
        with contextlib.suppress(MoonrakerError, Exception):
            await client.ws_rpc([(1, "camera.start_monitor", {"domain": domain, "interval": 0})], timeout=5.0)
        if not warm:
            await asyncio.sleep(FIRST_FRAME_DELAY)

    if state.stop_task is not None:
        state.stop_task.cancel()

    async def _idle_stop():
        try:
            await asyncio.sleep(IDLE_STOP)
        except asyncio.CancelledError:
            return
        await _stop_monitor(client, domain)

    state.stop_task = asyncio.ensure_future(_idle_stop())


async def release(client: MoonrakerClient, *, domain: str = "lan") -> None:
    """Drop the keepalive and tell the camera to stop now.

    Called when a printer is removed or disconnected, so a machine nobody is
    watching is not left capturing for another idle period.
    """
    state = _states.pop(client.base_url, None)
    if state and state.stop_task is not None:
        state.stop_task.cancel()
    await _stop_monitor(client, domain)


async def capture_frame(client: MoonrakerClient, *, domain: str = "lan") -> bytes:
    """Fetch one JPEG. Raises :class:`MoonrakerError` with a showable message."""
    await ensure_running(client, domain=domain)

    last_error: MoonrakerError | None = None
    for attempt in range(3):
        try:
            frame = await client.request("GET", SNAPSHOT_PATH, timeout=6.0, expect_json=False)
        except MoonrakerError as exc:
            last_error = exc
            # 404 means the plugin is mid-rewrite. Retry briefly and twice —
            # a full second of waiting would drop several frames of a live
            # view, and this also covers a camera that has only just started.
            if exc.status == 404 and attempt < 2:
                await asyncio.sleep(0.4)
                continue
            raise MoonrakerError(f"Camera: {exc}") from exc
        if frame:
            _state_for(client.base_url).last_frame = time.monotonic()
            return frame
        await asyncio.sleep(0.4)

    raise last_error or MoonrakerError("Camera returned no image — is it connected?")


async def generate_mjpeg_stream(
    client: MoonrakerClient,
    fps: int = 5,
    *,
    domain: str = "lan",
    on_frame: Callable[[bytes], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> AsyncGenerator[bytes, None]:
    """Poll ``monitor.jpg`` and yield multipart MJPEG parts.

    Signature and output match ``external_camera.generate_mjpeg_stream`` so
    every consumer Bambuddy already has — the camera tile, the wall, finish
    photos, plate detection — works on a U1 without knowing it is one.

    Eight consecutive failures end the feed. Fewer would drop a view over one
    rewrite window; more would keep a dead camera "connected" for a minute.
    """
    fps = max(1, min(int(fps or 5), 10))
    interval = 1.0 / fps
    failures = 0

    try:
        while not (stop_event and stop_event.is_set()):
            started = time.monotonic()
            try:
                frame = await capture_frame(client, domain=domain)
                failures = 0
            except Exception as exc:
                failures += 1
                if failures >= 8:
                    logger.warning("Snapmaker camera %s: giving up after 8 failures (%s)", client.base_url, exc)
                    return
                await asyncio.sleep(min(interval * failures, 2.0))
                continue

            if on_frame is not None:
                with contextlib.suppress(Exception):
                    on_frame(frame)

            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
            )

            elapsed = time.monotonic() - started
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
    finally:
        # The generator ending is the last viewer leaving. The keepalive timer
        # takes it from here: it stops the camera after the idle window rather
        # than immediately, so flipping between two tiles does not cold-start
        # the camera each time.
        pass
