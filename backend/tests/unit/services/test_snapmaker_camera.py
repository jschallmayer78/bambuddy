"""The U1 camera's start/stop dance.

Every rule here comes from how Snapmaker's camera plugin actually behaves:
it stops capturing when nobody asks, misbehaves when start_monitor is
hammered, and briefly 404s while it rewrites monitor.jpg.
"""

import asyncio

import pytest

from backend.app.services.snapmaker import camera as u1_camera
from backend.app.services.snapmaker.moonraker import MoonrakerError


class FakeClient:
    def __init__(self, frames=None, statuses=None):
        self.base_url = "http://192.168.1.9"
        self.rpc_calls: list[str] = []
        self.frame_requests = 0
        self._frames = frames if frames is not None else [b"\xff\xd8jpeg"] * 50
        self._statuses = list(statuses or [])

    async def ws_rpc(self, calls, *, collect=None, timeout=15.0):
        for _, method, _params in calls:
            self.rpc_calls.append(method)
        return []

    async def request(self, method, path, *, timeout=None, expect_json=True, **_kwargs):
        self.frame_requests += 1
        if self._statuses:
            status = self._statuses.pop(0)
            if status is not None:
                raise MoonrakerError(f"HTTP {status}", status=status)
        return self._frames.pop(0) if self._frames else b"\xff\xd8jpeg"

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def _clean_camera_state():
    u1_camera._states.clear()
    yield
    for state in u1_camera._states.values():
        if state.stop_task is not None:
            state.stop_task.cancel()
    u1_camera._states.clear()


@pytest.fixture(autouse=True)
def _no_cold_start_sleep(monkeypatch):
    """Skip the real first-frame wait; its timing is asserted separately."""
    monkeypatch.setattr(u1_camera, "FIRST_FRAME_DELAY", 0.0)


@pytest.mark.asyncio
async def test_a_frame_starts_the_camera_first():
    client = FakeClient()
    frame = await u1_camera.capture_frame(client)

    assert frame.startswith(b"\xff\xd8")
    assert client.rpc_calls == ["camera.start_monitor"]


@pytest.mark.asyncio
async def test_start_monitor_is_not_hammered():
    """The plugin misbehaves when it is; repeat frames ride the keepalive."""
    client = FakeClient()
    for _ in range(5):
        await u1_camera.capture_frame(client)

    assert client.rpc_calls.count("camera.start_monitor") == 1
    assert client.frame_requests == 5


@pytest.mark.asyncio
async def test_a_404_is_retried_because_the_plugin_is_mid_rewrite():
    client = FakeClient(statuses=[404, None])
    frame = await u1_camera.capture_frame(client)

    assert frame.startswith(b"\xff\xd8")
    assert client.frame_requests == 2


@pytest.mark.asyncio
async def test_a_persistent_404_is_reported_rather_than_retried_forever():
    client = FakeClient(statuses=[404, 404, 404])
    with pytest.raises(MoonrakerError):
        await u1_camera.capture_frame(client)
    assert client.frame_requests == 3


@pytest.mark.asyncio
async def test_a_warm_camera_does_not_pay_the_cold_start_wait(monkeypatch):
    """Waiting on a keepalive would stall every few frames of a live view."""
    monkeypatch.setattr(u1_camera, "FIRST_FRAME_DELAY", 5.0)
    monkeypatch.setattr(u1_camera, "START_COOLDOWN", 0.0)

    client = FakeClient()
    slept: list[float] = []

    async def _record_sleep(seconds):
        slept.append(seconds)

    await u1_camera.capture_frame(client)  # cold: one wait
    monkeypatch.setattr(asyncio, "sleep", _record_sleep)
    await u1_camera.capture_frame(client)  # warm: none

    assert slept == []
    assert client.rpc_calls.count("camera.start_monitor") == 2


@pytest.mark.asyncio
async def test_the_stream_yields_multipart_parts_and_publishes_raw_frames():
    client = FakeClient()
    stop = asyncio.Event()
    raw: list[bytes] = []
    parts = []

    async for part in u1_camera.generate_mjpeg_stream(client, fps=10, on_frame=raw.append, stop_event=stop):
        parts.append(part)
        if len(parts) == 3:
            stop.set()

    assert len(parts) == 3
    assert parts[0].startswith(b"--frame\r\nContent-Type: image/jpeg")
    assert raw == [b"\xff\xd8jpeg"] * 3, "consumers need the bare JPEG, not the wire framing"


@pytest.mark.asyncio
async def test_the_stream_gives_up_after_repeated_failures():
    client = FakeClient(statuses=[500] * 40)
    parts = [part async for part in u1_camera.generate_mjpeg_stream(client, fps=10)]
    assert parts == []


@pytest.mark.asyncio
async def test_releasing_stops_the_camera_now():
    client = FakeClient()
    await u1_camera.capture_frame(client)
    await u1_camera.release(client)

    assert "camera.stop_monitor" in client.rpc_calls
    assert client.base_url not in u1_camera._states
