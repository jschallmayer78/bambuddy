"""Driver for the Snapmaker U1.

Shape of the thing: the U1 runs Klipper behind a Moonraker fork over plain
HTTP, so where the Bambu client sits on a pushed MQTT feed this driver polls.
One task per printer queries the same object set Moonraker would push,
translates it through ``services/snapmaker/status.py`` into the shared
:class:`PrinterState`, and fires the same callbacks the MQTT client fires —
which is what lets the queue, the archive, notifications and the printer card
treat a U1 like any other machine.

Polling rather than the WebSocket's ``objects/subscribe`` is a deliberate
trade. A subscription would be cheaper, but it also has to be kept healthy
across Klipper restarts, firmware updates and Wi-Fi drops, and each of those
is a silent-stall failure mode where the printer looks frozen rather than
disconnected. A 2 s poll (1 s while printing) costs a few HTTP requests a
second on a LAN and fails loudly.

What the U1 genuinely does not have, and therefore never reports: an AMS (the
four toolheads are surfaced as one synthetic unit — see ``status.py``), a
filament dryer, a chamber heater, HMS fault codes (its own structured error
codes are mapped onto the same list), K-profiles, and Bambu's calibration
stages. Every one of those routes answers 501 for a U1 rather than a 500.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from backend.app.services.bambu_mqtt import HMSError, PrinterState
from backend.app.services.printer_drivers.base import (
    PRINTER_TYPE_SNAPMAKER_U1,
    UnsupportedOperation,
)
from backend.app.services.snapmaker import camera as u1_camera, status as u1_status
from backend.app.services.snapmaker.moonraker import MoonrakerClient, MoonrakerError

logger = logging.getLogger(__name__)

POLL_INTERVAL_IDLE = 2.0
POLL_INTERVAL_PRINTING = 1.0
# Two missed polls still count as connected — a single dropped packet on a
# busy LAN is not a printer going away. The third consecutive failure is.
MAX_CONSECUTIVE_FAILURES = 3
# Beyond this the last successful poll is too old to call the printer online,
# whatever the failure counter says (a poll task wedged on a socket that never
# times out would otherwise keep a dead printer green).
STALE_AFTER_SECONDS = 20.0

GCODES_ROOT = "gcodes"

# Klipper prints G-code. A 3MF is a slicer project, not something the U1 can
# execute, and uploading one produces a file the printer lists but cannot open.
PRINTABLE_SUFFIXES = (".gcode", ".gco", ".g", ".gcode.gz")


class SnapmakerU1Driver:
    """Live connection to one Snapmaker U1."""

    printer_type = PRINTER_TYPE_SNAPMAKER_U1

    def __init__(
        self,
        ip_address: str,
        serial_number: str,
        *,
        access_code: str | None = None,
        model: str | None = "U1",
        port: int | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_finish_photo_moment: Callable[[dict], None] | None = None,
        on_ams_change: Callable[[list], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_print_progress: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
        on_tray_change: Callable[[int, int], None] | None = None,
        **_ignored: Any,
    ):
        # ``ip_address`` may carry an explicit port ("192.168.1.9:7125") for a
        # printer whose Moonraker is not on 80; MoonrakerClient keeps it.
        self.ip_address = ip_address
        self.serial_number = serial_number
        # There is no access code on a U1 — Moonraker on a private LAN answers
        # unauthenticated. The field carries an optional API token for the rare
        # installation that configured one.
        self.access_code = access_code or ""
        self.model = model or "U1"

        self.state = PrinterState()
        # Read by PrinterManager.get_drying_targets for every printer. The U1
        # has no dryer, so the answer is "no cycles", not an attribute error.
        self._drying_targets: dict[int, dict] = {}

        self.client = MoonrakerClient(ip_address, port=port, token=self.access_code or None)

        self.on_state_change = on_state_change
        self.on_print_start = on_print_start
        self.on_print_complete = on_print_complete
        self.on_print_running_observed = on_print_running_observed
        self.on_finish_photo_moment = on_finish_photo_moment
        self.on_ams_change = on_ams_change
        self.on_layer_change = on_layer_change
        self.on_print_progress = on_print_progress
        self.on_bed_temp_update = on_bed_temp_update
        self.on_tray_change = on_tray_change

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._failures = 0
        self._last_success = 0.0
        self._power_off_marked = False

        # Print-lifecycle bookkeeping, mirroring the MQTT client's: what was
        # running last poll, so a transition can be detected rather than a
        # level re-reported every second.
        self._previous_state = "unknown"
        self._previous_file: str | None = None
        self._previous_layer = 0
        self._previous_progress = -1
        self._previous_trays: list[dict] | None = None
        self._previous_tray_now = 255
        self._slicer_estimate: float | None = None
        self._estimate_for_file: str | None = None
        self._first_poll_done = False
        self._finish_photo_sent = False

    # -- lifecycle ---------------------------------------------------------

    def connect(self, loop: asyncio.AbstractEventLoop | None = None):
        """Start polling. Returns immediately, like the MQTT client's connect."""
        self._stop.clear()
        if self._task is not None and not self._task.done():
            return
        loop = loop or asyncio.get_event_loop()
        self._task = loop.create_task(self._poll_loop())

    def disconnect(self, timeout: float = 0):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self.state.connected = False
        # Release the camera keepalive and the HTTP session without blocking
        # the caller — disconnect() is called from synchronous code paths.
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_event_loop()
            loop.create_task(self._async_teardown())

    async def _async_teardown(self):
        with contextlib.suppress(Exception):
            await u1_camera.release(self.client)
        with contextlib.suppress(Exception):
            await self.client.close()

    def check_staleness(self) -> bool:
        """Mirror of the MQTT client's contract: True when usable right now."""
        if self.state.connected and self._last_success:
            if time.monotonic() - self._last_success > STALE_AFTER_SECONDS:
                self.state.connected = False
                self._emit_state()
        return self.state.connected

    @property
    def is_stale(self) -> bool:
        return not self.check_staleness()

    def mark_power_off(self) -> bool:
        """Presume the printer lost power (its smart plug was switched off)."""
        if not self.state.connected:
            return False
        self.state.connected = False
        self._power_off_marked = True
        return True

    def request_status_update(self) -> bool:
        """No-op with a meaningful answer: the next poll is at most 2 s away."""
        return True

    # -- polling -----------------------------------------------------------

    async def _poll_loop(self):
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("U1 %s: poll failed: %s", self.ip_address, exc)
                self._record_failure(exc)
            interval = POLL_INTERVAL_PRINTING if self.state.state == "RUNNING" else POLL_INTERVAL_IDLE
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    def _record_failure(self, exc: Exception):
        self._failures += 1
        if self._failures >= MAX_CONSECUTIVE_FAILURES and self.state.connected:
            logger.info("U1 %s: marking offline after %s failed polls (%s)", self.ip_address, self._failures, exc)
            self.state.connected = False
            self._emit_state()

    async def _poll_once(self):
        try:
            status = await self.client.query_objects()
        except MoonrakerError as exc:
            self._record_failure(exc)
            return

        self._failures = 0
        self._last_success = time.monotonic()
        was_connected = self.state.connected
        # A report arriving after a presumed power-off proves the presumption
        # wrong — the plug evidently does not feed this printer (#2629's case).
        self._power_off_marked = False
        self.state.connected = True

        fields = u1_status.build_state_fields(status, slicer_estimate=self._slicer_estimate)
        await self._refresh_slicer_estimate(fields["gcode_file"])
        self._apply(status, fields)

        if not was_connected:
            await self._refresh_firmware_info()

        self._emit_state()

    async def _refresh_slicer_estimate(self, filename: str | None):
        """Cache the sliced file's own time estimate, for the opening percent.

        Fetched once per file: the metadata does not change while the file
        prints, and a per-poll request would triple this driver's traffic for
        a number that is constant.
        """
        if not filename or filename == self._estimate_for_file:
            return
        self._estimate_for_file = filename
        self._slicer_estimate = None
        try:
            metadata = await self.client.file_metadata(filename)
        except MoonrakerError:
            return
        estimate = metadata.get("estimated_time")
        if isinstance(estimate, int | float) and estimate > 0:
            self._slicer_estimate = float(estimate)

    async def _refresh_firmware_info(self):
        """Firmware version and the printer's own serial, on (re)connect."""
        try:
            system_info = await self.client.system_info()
        except MoonrakerError:
            return
        product_info = system_info.get("product_info") or {}
        version = product_info.get("firmware_version") or product_info.get("version")
        if not version:
            # Vanilla Moonraker reports the Klipper version instead; better
            # than showing nothing on the card's firmware row.
            with contextlib.suppress(MoonrakerError):
                server = await self.client.server_info()
                version = server.get("klippy_version")
        if version:
            self.state.firmware_version = str(version)

    # -- state application -------------------------------------------------

    def _apply(self, status: dict, fields: dict):
        state = self.state
        trays = fields.pop("trays")
        error_code = fields.pop("error_code")
        error_message = fields.pop("error_message")

        previous_state = self._previous_state
        previous_file = self._previous_file

        for key, value in fields.items():
            setattr(state, key, value)

        # raw_data is what printer_state_to_dict reads the AMS out of, and what
        # the diagnostic snapshot dumps. Keeping the untouched Moonraker
        # payload alongside the synthetic unit makes a support bundle useful.
        state.raw_data = {
            "ams": u1_status.synthetic_ams(trays),
            "snapmaker": status,
        }
        state.hms_errors = self._build_errors(error_code, error_message)
        # Nothing on a U1 populates these; they are re-stated each poll so a
        # value cannot survive from a previous connection.
        state.ams_status_main = 0
        state.ams_status_sub = 0
        state.stg_cur = -1
        state.sdcard_reported = False

        self._emit_transitions(state, trays, previous_state, previous_file, status)

    def _build_errors(self, code: str, message: str) -> list:
        if not code and not message:
            return []
        # The U1's codes are its own four-group form, not Bambu HMS codes, so
        # no catalogue lookup applies and the printer's own sentence is the
        # description. severity 1 (fatal) because the firmware only raises
        # these for conditions that stopped the machine.
        return [
            HMSError(
                code=code or "SNAPMAKER",
                attr=0,
                module=0,
                severity=1,
                description=message or None,
                actions=[],
                job_id=self.state.subtask_id,
            )
        ]

    def _emit_transitions(self, state: PrinterState, trays: list[dict], previous_state, previous_file, status: dict):
        # The first poll after (re)connecting describes a situation, not a
        # change. Firing print-start for a job that has been running for an
        # hour would re-archive it and re-notify; the MQTT client guards the
        # same way.
        first_poll = not self._first_poll_done
        self._first_poll_done = True

        current_file = state.gcode_file
        started = state.state == "RUNNING" and (previous_state != "RUNNING" or current_file != previous_file)

        if started and not first_poll and self.on_print_start:
            self._finish_photo_sent = False
            self.on_print_start(
                {
                    "filename": current_file,
                    "subtask_name": state.subtask_name,
                    "remaining_time": state.remaining_time * 60 if state.remaining_time > 0 else None,
                    "raw_data": state.raw_data,
                    "ams_mapping": None,
                }
            )
        elif started and first_poll and self.on_print_running_observed:
            # Bambuddy started while this print was already running: no start
            # event, but the timelapse baseline still has to be taken.
            self.on_print_running_observed(
                {
                    "filename": current_file,
                    "subtask_name": state.subtask_name,
                    "raw_data": state.raw_data,
                }
            )

        finished = previous_state in ("RUNNING", "PAUSE") and state.state in ("FINISH", "FAILED", "IDLE")
        if finished and not first_poll:
            if self.on_finish_photo_moment and not self._finish_photo_sent:
                self._finish_photo_sent = True
                self.on_finish_photo_moment({"trigger": "print_end", "timelapse_was_active": False})
            if self.on_print_complete:
                self.on_print_complete(
                    {
                        # A U1 that simply went idle after a print finished it;
                        # cancelled and error both map to FAILED upstream.
                        "status": "FINISH" if state.state in ("FINISH", "IDLE") else "FAILED",
                        "filename": previous_file or current_file,
                        "subtask_name": state.subtask_name,
                        "raw_data": state.raw_data,
                        "timelapse_was_active": False,
                        "hms_errors": [
                            {"code": e.code, "severity": e.severity, "description": e.description}
                            for e in (state.hms_errors or [])
                        ],
                        "ams_mapping": None,
                        "last_progress": self._previous_progress if self._previous_progress >= 0 else 0,
                        "last_layer_num": self._previous_layer,
                    }
                )

        if state.layer_num != self._previous_layer:
            if self.on_layer_change and state.layer_num:
                self.on_layer_change(state.layer_num)
            self._previous_layer = state.layer_num

        progress = int(state.progress)
        if progress != self._previous_progress:
            if self.on_print_progress:
                self.on_print_progress(progress)
            self._previous_progress = progress

        bed_temp = (state.temperatures or {}).get("bed")
        if isinstance(bed_temp, int | float) and self.on_bed_temp_update:
            self.on_bed_temp_update(float(bed_temp))

        if trays != self._previous_trays:
            if self.on_ams_change:
                self.on_ams_change(u1_status.synthetic_ams(trays))
            self._previous_trays = trays

        if state.tray_now != self._previous_tray_now:
            if self.on_tray_change and state.tray_now != 255:
                self.on_tray_change(state.tray_now, state.layer_num)
            self._previous_tray_now = state.tray_now

        self._previous_state = state.state
        self._previous_file = current_file

    def _emit_state(self):
        if self.on_state_change:
            with contextlib.suppress(Exception):
                self.on_state_change(self.state)

    # -- print control -----------------------------------------------------

    async def pause_print(self) -> bool:
        return await self.client.print_action("pause")

    async def resume_print(self) -> bool:
        return await self.client.print_action("resume")

    async def stop_print(self) -> bool:
        return await self.client.print_action("cancel")

    async def emergency_stop(self) -> bool:
        return await self.client.emergency_stop()

    async def send_gcode(self, gcode: str) -> bool:
        return await self.client.run_gcode(gcode)

    async def set_bed_temperature(self, target: int) -> bool:
        # Stock U1 firmware refuses a bed target above 100 °C; clamping here
        # turns a rejected command into the hottest one the machine accepts.
        target = max(0, min(int(target), 100))
        return await self.client.run_gcode(f"M140 S{target}")

    async def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:
        nozzle = max(0, min(int(nozzle), u1_status.TOOLHEAD_COUNT - 1))
        return await self.client.run_gcode(f"M104 T{nozzle} S{max(0, int(target))}")

    async def set_print_speed(self, mode: int) -> bool:
        percent = u1_status.SPEED_PERCENT_BY_LEVEL.get(int(mode))
        if percent is None:
            raise MoonrakerError(f"Unknown speed mode: {mode}")
        return await self.client.run_gcode(f"M220 S{percent}")

    async def set_part_fan(self, speed: int) -> bool:
        # Bambuddy speaks percent; M106 speaks 0-255.
        percent = max(0, min(int(speed), 100))
        return await self.client.run_gcode(f"M106 S{round(percent * 255 / 100)}")

    async def set_fan_speed(self, fan: int, speed: int) -> bool:
        # The U1 exposes one controllable part-cooling fan; the auxiliary and
        # chamber fan ids Bambu uses have no counterpart.
        if int(fan) != 1:
            raise UnsupportedOperation("set_fan_speed(aux/chamber)", "The Snapmaker U1")
        return await self.set_part_fan(speed)

    async def home_axes(self, axes: str = "XYZ") -> bool:
        axes = "".join(ch for ch in (axes or "XYZ").upper() if ch in "XYZ") or "XYZ"
        return await self.client.run_gcode(f"G28 {' '.join(axes)}")

    async def move_axis(self, axis: str, distance: float, speed: int = 3000) -> bool:
        axis = (axis or "").upper()
        if axis not in ("X", "Y", "Z", "E"):
            raise MoonrakerError(f"Unknown axis: {axis}")
        if axis == "E":
            return await self.client.run_gcode(f"M83\nG1 E{float(distance):.2f} F{int(speed)}")
        # G91/G90 rather than a bare relative move: leaving the machine in
        # relative mode would corrupt the next print's coordinates.
        return await self.client.run_gcode(f"G91\nG1 {axis}{float(distance):.2f} F{int(speed)}\nG90")

    async def disable_motors(self) -> bool:
        return await self.client.run_gcode("M84")

    async def select_extruder(self, extruder: int) -> bool:
        extruder = int(extruder)
        if not 0 <= extruder < u1_status.TOOLHEAD_COUNT:
            raise MoonrakerError(f"Toolhead must be 0-{u1_status.TOOLHEAD_COUNT - 1}")
        return await self.client.run_gcode(f"T{extruder}")

    async def unload_filament(self, extruder: int) -> bool:
        extruder = int(extruder)
        if not 0 <= extruder < u1_status.TOOLHEAD_COUNT:
            raise MoonrakerError(f"Toolhead must be 0-{u1_status.TOOLHEAD_COUNT - 1}")
        # A physical feed operation of unconfirmed duration — generous bound
        # rather than the default, which would report a working unload as failed.
        return await self.client.run_gcode(f"AUTO_FEEDING EXTRUDER={extruder} UNLOAD=1", timeout=300.0)

    async def skip_objects(self, object_ids: list[int]) -> bool:
        names = [self.state.printable_objects.get(str(oid)) for oid in object_ids]
        names = [name for name in names if name]
        if not names:
            raise MoonrakerError("No matching objects to exclude")
        for name in names:
            await self.client.run_gcode(f"EXCLUDE_OBJECT NAME={name}")
        return True

    async def set_filament_color(self, extruder: int, hex_color: str) -> str:
        """Write a slot's colour back to the printer.

        ``SET_PRINT_FILAMENT_CONFIG`` is the command the touchscreen itself
        issues. Three refusals are deliberate and not worked around: the
        firmware has no documented mid-print colour change, an empty slot has
        no colour to correct, and an official RFID spool's colour belongs to
        its tag — overriding that needs ``FORCE=1``, which also marks the spool
        unofficial, so it is never sent. The write is read back because
        ``SAVE='1'`` is what makes it stick, and a silent no-op is worse than
        an error.
        """
        import re

        slot = int(extruder)
        if not 0 <= slot < u1_status.TOOLHEAD_COUNT:
            raise MoonrakerError(f"Toolhead must be 0-{u1_status.TOOLHEAD_COUNT - 1}")
        match = re.match(r"^#?([0-9a-fA-F]{6})$", str(hex_color or ""))
        if not match:
            raise MoonrakerError("Colour must be RRGGBB hex")
        rgba = match.group(1).upper() + "FF"

        status = await self.client.query_objects({"print_stats": None, "print_task_config": None})
        state = str((status.get("print_stats") or {}).get("state") or "").lower()
        if state in ("printing", "paused"):
            raise MoonrakerError(f"Printer is {state} — colours can only be changed while idle")
        trays = u1_status.decode_toolheads(status.get("print_task_config") or {})
        if not trays[slot]["exists"]:
            raise MoonrakerError(f"No filament loaded in toolhead T{slot + 1}")
        if not trays[slot]["color_editable"]:
            raise MoonrakerError(f"T{slot + 1} holds an official Snapmaker RFID spool — its colour comes from the tag")

        await self.client.run_gcode(
            f"SET_PRINT_FILAMENT_CONFIG CONFIG_EXTRUDER='{slot}' FILAMENT_COLOR_RGBA='{rgba}' SAVE='1'"
        )
        confirm = await self.client.query_objects({"print_task_config": None})
        written = (confirm.get("print_task_config") or {}).get("filament_color_rgba") or []
        got = str(written[slot] if slot < len(written) else "").upper().lstrip("#")
        if got != rgba:
            raise MoonrakerError(f"Write not confirmed — printer reports {got or 'nothing'}")
        return "#" + rgba[:6]

    # -- files -------------------------------------------------------------

    async def list_files(self, path: str = "/") -> list[dict]:
        """Files on the printer, in the shape the file-manager route expects."""
        entries = await self.client.list_files(GCODES_ROOT)
        files = []
        for entry in entries:
            name = entry.get("path") or entry.get("filename") or ""
            if not name:
                continue
            files.append(
                {
                    "name": name.rsplit("/", 1)[-1],
                    "path": f"/{name}",
                    "size": int(entry.get("size") or 0),
                    "date": entry.get("modified"),
                    "is_dir": False,
                }
            )
        return files

    async def download_file(self, remote_path: str) -> bytes:
        return await self.client.download(GCODES_ROOT, remote_path)

    async def delete_file(self, remote_path: str) -> bool:
        return await self.client.delete_file(GCODES_ROOT, remote_path)

    async def get_thumbnail(self, remote_path: str) -> bytes | None:
        """Largest embedded thumbnail for a sliced file, as PNG bytes.

        Moonraker extracts these at upload time and serves them from the same
        root, so this is two cheap requests rather than parsing the G-code.
        """
        metadata = await self.client.file_metadata(remote_path.lstrip("/"))
        thumbnails = metadata.get("thumbnails") or []
        if not thumbnails:
            return None
        largest = max(thumbnails, key=lambda t: int(t.get("size") or 0))
        relative = largest.get("relative_path")
        if not relative:
            return None
        return await self.client.download(GCODES_ROOT, relative)

    async def upload_file(
        self,
        local_path,
        remote_name: str | None = None,
        *,
        start_print: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> bool:
        name = remote_name or os.path.basename(str(local_path))
        if not name.lower().endswith(PRINTABLE_SUFFIXES):
            raise MoonrakerError(
                f"The Snapmaker U1 prints G-code, and '{name}' is not a G-code file. "
                "Slice for the U1 and queue the resulting .gcode."
            )
        await self.client.upload(
            local_path,
            remote_name=name,
            root=GCODES_ROOT,
            start_print=start_print,
            timeout=None,
            progress_callback=progress_callback,
        )
        return True

    # -- print start -------------------------------------------------------

    async def start_print(
        self,
        filename: str,
        plate_id: int = 1,
        *,
        ams_mapping: list[int] | None = None,
        timelapse: bool = False,
        bed_levelling: str = "auto",
        flow_cali: str = "auto",
        **_ignored: Any,
    ) -> bool:
        """Start a file already on the printer.

        The toolhead mapping and per-print preferences are U1 firmware macros
        that must be sent *before* the print begins — they are stored into the
        job's task config and read by the print-start routine, so sending them
        afterwards has no effect on the running job.

        ``ams_mapping`` arrives in Bambuddy's convention: index = the job's
        filament slot, value = the physical slot to feed it from, with -1 for
        "unused". That is exactly what ``SET_PRINT_EXTRUDER_MAP`` wants.
        """
        await self._apply_print_preferences(
            ams_mapping,
            auto_level=bed_levelling != "false",
            flow_calibrate=flow_cali != "false",
            timelapse=bool(timelapse),
        )
        return await self.client.start_print(filename.lstrip("/"))

    async def _apply_print_preferences(
        self,
        ams_mapping: list[int] | None,
        *,
        auto_level: bool,
        flow_calibrate: bool,
        timelapse: bool,
    ) -> None:
        lines: list[str] = []
        used: list[int] = []
        for job_slot, physical in enumerate(ams_mapping or []):
            if physical is None or int(physical) < 0:
                continue
            physical = int(physical)
            if not 0 <= physical < u1_status.TOOLHEAD_COUNT:
                raise MoonrakerError(f"Toolhead {physical} does not exist on a U1")
            lines.append(f"SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER={job_slot} MAP_EXTRUDER={physical}")
            used.append(physical)
        if used:
            lines.append("SET_PRINT_USED_EXTRUDERS EXTRUDERS=" + ",".join(str(slot) for slot in sorted(set(used))))
        lines.append(
            "SET_PRINT_PREFERENCES "
            f"BED_LEVEL={1 if auto_level else 0} "
            f"FLOW_CALIBRATE={1 if flow_calibrate else 0} "
            f"TIME_LAPSE_CAMERA={1 if timelapse else 0}"
        )
        # Generous bound: whether BED_LEVEL=1 merely records a flag or kicks
        # off levelling synchronously is not settled from the firmware source,
        # and a ceiling on an unconfirmed duration beats reporting a failure
        # for a command that was still working.
        await self.client.run_gcode("\n".join(lines), timeout=300.0)

    # -- camera ------------------------------------------------------------

    async def capture_camera_frame(self) -> bytes:
        return await u1_camera.capture_frame(self.client)

    def camera_stream(self, fps: int = 5, *, on_frame=None, stop_event=None):
        return u1_camera.generate_mjpeg_stream(self.client, fps, on_frame=on_frame, stop_event=stop_event)

    # -- everything else ---------------------------------------------------

    def __getattr__(self, name: str):
        """Answer for Bambu-only operations without pretending to have them.

        Reached only for names not found normally. ``UnsupportedOperation``
        derives from ``AttributeError``, so ``hasattr`` and
        ``getattr(x, n, None)`` still behave, while a direct call carries the
        operation's name into a 501 response.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        raise UnsupportedOperation(name, "The Snapmaker U1")


async def probe(ip_address: str, *, token: str | None = None, port: int | None = None, timeout: float = 5.0) -> dict:
    """Identify a U1 at an address, for add-printer validation and discovery.

    Returns ``{"success", "model", "serial", "name", "state", "reason"}``.
    A machine that answers ``/machine/system_info`` with Snapmaker's
    ``product_info`` block is a U1-family printer; vanilla Moonraker answers
    the same endpoint without that block, which is how the two are told apart.
    """
    client = MoonrakerClient(ip_address, port=port, token=token, timeout=timeout)
    try:
        system_info = await client.system_info()
        product_info = system_info.get("product_info") or {}
        if not product_info:
            return {
                "success": False,
                "reason": "The address answered Moonraker, but did not identify itself as a Snapmaker printer.",
            }
        status = await client.query_objects({"print_stats": None, "webhooks": None})
        return {
            "success": True,
            "model": product_info.get("machine_type") or "U1",
            "serial": product_info.get("serial_number"),
            "name": product_info.get("device_name"),
            "state": u1_status.map_state((status.get("print_stats") or {}).get("state")),
            "reason": None,
        }
    except MoonrakerError as exc:
        return {"success": False, "reason": str(exc)}
    finally:
        await client.close()
