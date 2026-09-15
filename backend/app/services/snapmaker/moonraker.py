"""Moonraker transport for the Snapmaker U1.

The U1 does not speak the Bambu MQTT protocol. Its stock firmware is a
Moonraker fork sitting on top of Klipper, served over **plain HTTP on port
80** (Moonraker's usual 7125 also answers, which is why the port stays
configurable), with a JSON-RPC WebSocket at ``/websocket``. On top of vanilla
Moonraker Snapmaker adds:

* ``print_task_config`` — the four toolheads' filament state (presence,
  colour, type, whether the spool is an RFID-tagged official one). This is
  what the touchscreen writes, and it is the only source that also covers
  third-party spools; ``filament_detect`` only knows about official RFID
  spools and reads blank for everything else.
* structured errors on ``print_stats.exception`` / ``print_stats.message``.
* ``product_info`` on ``/machine/system_info`` — device name, machine type and
  the printer's serial number, none of which vanilla Moonraker reports.
* a camera plugin addressed over the WebSocket (``camera.start_monitor``),
  see ``services/snapmaker/camera.py``.

Everything in this module is transport only: issue a request, hand back
parsed JSON, raise :class:`MoonrakerError` on anything else. Interpreting the
payload is ``status.py``'s job and driving the printer is the driver's.

Access control: a U1 on a private LAN answers every one of these endpoints
without a key or a token. ``token`` exists because Moonraker *can* be
configured to demand one and a user may well have done so; it is never
required by stock firmware.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Moonraker answers a status query in single-digit milliseconds on a healthy
# printer. This bound is for the network, not for the printer's own work.
DEFAULT_TIMEOUT = 8.0

# /printer/gcode/script BLOCKS until the script has finished running, and some
# scripts legitimately run for a minute or more: CANCEL_PRINT executes the
# whole end-of-print routine (park, cool, retract), and a filament unload is a
# physical feed operation. Giving up early does NOT abort the command — it
# lands and completes regardless — it only makes Bambuddy report a failure for
# something that worked, which is the bug this constant exists to avoid.
GCODE_TIMEOUT = 60.0

# The objects a U1 status poll asks for. Keep this list in one place: it is
# used both for the one-shot HTTP query and for the WebSocket subscription, and
# the two diverging is how a field ends up updating only on some code paths.
STATUS_OBJECTS: dict[str, list[str] | None] = {
    "print_task_config": None,
    "print_stats": None,
    "display_status": None,
    "virtual_sdcard": None,
    "heater_bed": None,
    "extruder": None,
    "extruder1": None,
    "extruder2": None,
    "extruder3": None,
    "fan": None,
    "gcode_move": None,
    "toolhead": None,
    "exclude_object": None,
    "webhooks": None,
}


class MoonrakerError(Exception):
    """A Moonraker request did not produce a usable result."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_base_url(host: str, port: int | None = None) -> str:
    """Build the printer's HTTP base URL from what the DB stores.

    ``Printer.ip_address`` holds a bare host (the column's validator rejects a
    scheme), so the scheme is always added here. A host that already carries
    one is still accepted, because a user pasting ``http://1.2.3.4`` into a
    field labelled "IP address" is a mistake worth absorbing rather than an
    error worth surfacing.
    """
    host = (host or "").strip().rstrip("/")
    if not host:
        raise MoonrakerError("No printer address configured")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    if port and ":" not in host.split("//", 1)[1]:
        host = f"{host}:{int(port)}"
    return host


def websocket_url(base_url: str, token: str | None = None) -> str:
    """WebSocket URL for a base URL, preserving an explicit port.

    Deliberately keeps the host *with* its port: a printer configured as
    ``http://ip:7125`` has to reach 7125 on the socket too, and dropping the
    port silently falls back to 80, where nothing may be listening.
    """
    ws = base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    url = f"{ws}/websocket"
    if token:
        from urllib.parse import quote

        url = f"{url}?token={quote(token, safe='')}"
    return url


class MoonrakerClient:
    """Thin async HTTP + WebSocket client for one printer.

    One instance owns one :class:`aiohttp.ClientSession`. The session is
    created lazily on first use and must be released with :meth:`close`;
    the driver does that when the printer disconnects.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = normalize_base_url(host, port)
        self.token = token or None
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    # -- session -----------------------------------------------------------

    async def session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    headers=({"X-Api-Key": self.token} if self.token else None),
                )
            return self._session

    async def close(self) -> None:
        session, self._session = self._session, None
        if session and not session.closed:
            with contextlib.suppress(Exception):
                await session.close()

    # -- plumbing ----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        timeout: float | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Perform one request and return ``result`` from the JSON envelope.

        Moonraker wraps every successful answer in ``{"result": ...}`` and
        every failure in ``{"error": {"message": ...}}`` — a 200 with an
        ``error`` body is a failure, so the body is inspected rather than the
        status code alone.
        """
        url = f"{self.base_url}{path}"
        session = await self.session()
        client_timeout = aiohttp.ClientTimeout(total=timeout or self.timeout)
        try:
            async with session.request(
                method, url, params=params, json=json_body, data=data, timeout=client_timeout
            ) as response:
                if not expect_json:
                    body = await response.read()
                    if response.status >= 400:
                        raise MoonrakerError(f"{path}: HTTP {response.status}", status=response.status)
                    return body
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except ValueError:
                    payload = {}
                if isinstance(payload, dict) and payload.get("error"):
                    error = payload["error"]
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    raise MoonrakerError(f"{path}: {message or 'unknown error'}", status=response.status)
                if response.status >= 400:
                    raise MoonrakerError(f"{path}: HTTP {response.status}", status=response.status)
                if isinstance(payload, dict) and "result" in payload:
                    return payload["result"]
                return payload
        except TimeoutError as exc:
            raise MoonrakerError(f"{path}: printer did not answer within {timeout or self.timeout:.0f}s") from exc
        except aiohttp.ClientError as exc:
            raise MoonrakerError(f"{path}: {exc}") from exc

    # -- status ------------------------------------------------------------

    async def query_objects(self, objects: dict[str, list[str] | None] | None = None) -> dict:
        """Query Klipper objects. Returns the ``status`` sub-dict."""
        objects = objects or STATUS_OBJECTS
        # Moonraker's HTTP form is ?obj1&obj2=a,b — a bare name means "all
        # fields", which is what every entry here wants.
        query = "&".join(name if fields is None else f"{name}={','.join(fields)}" for name, fields in objects.items())
        result = await self.request("GET", f"/printer/objects/query?{query}")
        return (result or {}).get("status", {}) if isinstance(result, dict) else {}

    async def server_info(self) -> dict:
        result = await self.request("GET", "/server/info")
        return result if isinstance(result, dict) else {}

    async def system_info(self) -> dict:
        """``/machine/system_info`` — includes Snapmaker's ``product_info``."""
        result = await self.request("GET", "/machine/system_info")
        return (result or {}).get("system_info", {}) if isinstance(result, dict) else {}

    # -- control -----------------------------------------------------------

    async def run_gcode(self, script: str, *, timeout: float = GCODE_TIMEOUT) -> bool:
        """Run a G-code script and wait for Klipper to finish executing it."""
        await self.request("POST", "/printer/gcode/script", params={"script": script}, timeout=timeout)
        return True

    async def print_action(self, action: str, *, timeout: float = GCODE_TIMEOUT) -> bool:
        """``pause`` / ``resume`` / ``cancel`` through Moonraker's own routes."""
        if action not in ("pause", "resume", "cancel"):
            raise MoonrakerError(f"Unknown print action: {action}")
        await self.request("POST", f"/printer/print/{action}", timeout=timeout)
        return True

    async def start_print(self, filename: str, *, timeout: float = GCODE_TIMEOUT) -> bool:
        await self.request("POST", "/printer/print/start", params={"filename": filename}, timeout=timeout)
        return True

    async def emergency_stop(self) -> bool:
        # Deliberately on the short default timeout: in an emergency the
        # operator needs to learn fast that the command is not landing, so they
        # can pull power, rather than watch Bambuddy wait out a minute.
        await self.request("POST", "/printer/emergency_stop", timeout=self.timeout)
        return True

    # -- files -------------------------------------------------------------

    async def list_files(self, root: str = "gcodes") -> list[dict]:
        result = await self.request("GET", "/server/files/list", params={"root": root}, timeout=30.0)
        return result if isinstance(result, list) else []

    async def file_metadata(self, filename: str) -> dict:
        result = await self.request("GET", "/server/files/metadata", params={"filename": filename})
        return result if isinstance(result, dict) else {}

    async def file_roots(self) -> dict[str, str]:
        """Map each Moonraker root name to its absolute path on the printer."""
        result = await self.request("GET", "/server/files/roots")
        return {entry.get("name"): entry.get("path") for entry in (result or []) if isinstance(entry, dict)}

    async def download(self, root: str, path: str, *, timeout: float = 120.0) -> bytes:
        return await self.request("GET", f"/server/files/{root}/{path.lstrip('/')}", timeout=timeout, expect_json=False)

    async def delete_file(self, root: str, path: str) -> bool:
        await self.request("DELETE", f"/server/files/{root}/{path.lstrip('/')}", timeout=30.0)
        return True

    async def upload(
        self,
        local_path,
        *,
        remote_name: str | None = None,
        root: str = "gcodes",
        subdir: str = "",
        start_print: bool = False,
        timeout: float | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> dict:
        """Upload a file to the printer's ``gcodes`` root.

        Streams from disk rather than reading the file into memory — a sliced
        job can be hundreds of megabytes and this process also runs a print
        queue. ``timeout=None`` means no overall bound: a slow link
        legitimately takes minutes, and aborting halfway leaves a truncated
        file on the printer that someone might later try to print.
        """
        import os

        local_path = str(local_path)
        name = remote_name or os.path.basename(local_path)
        total = os.path.getsize(local_path)

        form = aiohttp.FormData()
        form.add_field("root", root)
        if subdir:
            form.add_field("path", subdir)
        form.add_field("print", "true" if start_print else "false")
        form.add_field(
            "file",
            _ProgressFileReader(local_path, total, progress_callback),
            filename=name,
            content_type="application/octet-stream",
        )
        result = await self.request(
            "POST",
            "/server/files/upload",
            data=form,
            timeout=timeout,
            # aiohttp's ClientTimeout(total=None) means "no limit", which is
            # exactly what a multi-minute upload needs.
        )
        return result if isinstance(result, dict) else {}

    # -- websocket ---------------------------------------------------------

    async def ws_rpc(
        self,
        calls: list[tuple[int, str, dict]],
        *,
        collect: Callable[[dict], bool] | None = None,
        timeout: float = 15.0,
    ) -> list[dict]:
        """Open one WebSocket, send `calls`, gather replies, close.

        ``collect`` decides when enough has arrived: it is handed every parsed
        message and returns True once the caller is satisfied. Without it the
        call returns as soon as every request id has been answered.

        A short-lived socket per call is deliberate. These are occasional
        operations (a camera keepalive, a one-shot RPC), and a persistent
        socket would have to be reconnected, health-checked and torn down
        alongside the polling loop for no gain.
        """
        session = await self.session()
        wanted = {call_id for call_id, _, _ in calls}
        received: list[dict] = []
        try:
            async with session.ws_connect(
                websocket_url(self.base_url, self.token), timeout=aiohttp.ClientWSTimeout(ws_close=timeout)
            ) as ws:
                for call_id, method, params in calls:
                    await ws.send_json({"jsonrpc": "2.0", "id": call_id, "method": method, "params": params})

                async def _pump():
                    async for message in ws:
                        if message.type is not aiohttp.WSMsgType.TEXT:
                            continue
                        try:
                            parsed = json.loads(message.data)
                        except ValueError:
                            continue
                        # The printer broadcasts notify_* to every socket about
                        # once a second; anything without one of our ids is noise.
                        if parsed.get("id") in wanted:
                            received.append(parsed)
                            wanted.discard(parsed["id"])
                        if collect is not None:
                            if collect(parsed):
                                return
                        elif not wanted:
                            return

                await asyncio.wait_for(_pump(), timeout=timeout)
        except TimeoutError:
            # A partial result is still a result: camera keepalives in
            # particular do not care whether the acknowledgement arrived.
            logger.debug("Moonraker WebSocket RPC timed out after %.0fs (%s)", timeout, self.base_url)
        except aiohttp.ClientError as exc:
            raise MoonrakerError(f"WebSocket: {exc}") from exc
        return received


class _ProgressFileReader:
    """Async-iterable file body that reports bytes sent.

    aiohttp accepts any async iterator as a form-field body. Reading in chunks
    keeps a large upload off the heap, and the callback is what turns a
    multi-minute transfer into something a user can watch.
    """

    CHUNK = 256 * 1024

    def __init__(self, path: str, total: int, callback: Callable[[int, int], None] | None):
        self._path = path
        self._total = total
        self._callback = callback

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        sent = 0
        loop = asyncio.get_running_loop()
        with open(self._path, "rb") as handle:
            while True:
                chunk = await loop.run_in_executor(None, handle.read, self.CHUNK)
                if not chunk:
                    break
                sent += len(chunk)
                if self._callback:
                    try:
                        self._callback(sent, self._total)
                    except Exception:  # pragma: no cover - progress is best-effort
                        pass
                yield chunk
