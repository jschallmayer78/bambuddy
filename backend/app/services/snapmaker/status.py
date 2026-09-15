"""Translate a Moonraker object query into Bambuddy's :class:`PrinterState`.

Everything downstream of the driver — the WebSocket serializer, the REST
status response, the printer card, notifications, the queue — reads
``PrinterState``. Mapping into it here, once, is what lets a U1 use all of
that unchanged instead of growing a second status pipeline.

Two mappings are worth calling out because they are conventions rather than
translations:

**The four toolheads are published as a synthetic AMS unit.** The U1 has four
direct-drive extruders, each with its own spool; Bambuddy already renders "a
unit with four slots that have a colour, a material and one of them active".
So the toolheads are written into ``raw_data["ams"]`` as unit 0 with trays
0-3, and ``tray_now`` is the active extruder. Nothing about the AMS UI needed
changing, and a mapping from a job's filament to a physical slot means the
same thing on both machines. The unit carries no humidity or drying fields —
there is no dryer — and ``supports_drying(model)`` returns False for a U1, so
none of that UI appears.

**Remaining time is derived, not reported.** Klipper publishes elapsed print
duration and file progress but no ETA. The estimate here is the file-progress
one (elapsed / progress - elapsed), which is what Mainsail and Fluidd show by
default; when the slicer's own estimate is known it is preferred for the
first few percent, where the progress-based figure is wildly noisy.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Klipper's print_stats.state -> Bambuddy's gcode_state vocabulary. Bambuddy
# keys queue gating, notifications and the card's colour off these exact
# strings (PrinterManager.ACTIVE_PRINT_STATES), so a U1 has to speak them.
_STATE_MAP = {
    "standby": "IDLE",
    "printing": "RUNNING",
    "paused": "PAUSE",
    "complete": "FINISH",
    "cancelled": "FAILED",
    "error": "FAILED",
}

EXTRUDER_KEYS = ("extruder", "extruder1", "extruder2", "extruder3")

# Slot count of a U1's toolchanger. Fixed by the hardware.
TOOLHEAD_COUNT = 4


def map_state(raw_state: str | None) -> str:
    return _STATE_MAP.get(str(raw_state or "").lower(), "unknown")


def klipper_fault(status: dict) -> tuple[str, str] | None:
    """Return ``(state, message)`` when Klipper itself is unhealthy.

    A Klipper shutdown outranks whatever ``print_stats`` still says: the
    firmware freezes the print fields at the moment it died, so a machine that
    shut down mid-print keeps reporting ``printing`` at 47% forever. Reading
    that as a live print is how a queue dispatches onto a printer that cannot
    move.
    """
    webhooks = status.get("webhooks") or {}
    klippy_state = str(webhooks.get("state") or "").lower()
    if klippy_state in ("shutdown", "error"):
        message = str(webhooks.get("state_message") or "").strip()
        # Klipper's own message is multi-line and starts with the useful part.
        first_line = message.splitlines()[0] if message else "Klipper is not ready"
        return "FAILED", first_line
    return None


def decode_error(print_stats: dict) -> tuple[str, str]:
    """Pull Snapmaker's structured error code out of ``print_stats``.

    Two shapes exist in the wild. Newer firmware puts an object on
    ``exception`` (level/id/index/code/message); older firmware puts a JSON
    string on ``message`` with ``coded`` and ``msg`` keys. All-zero codes mean
    "no error" and are discarded rather than shown as ``0000-0000-0000-0000``.
    """
    exception = print_stats.get("exception")
    if isinstance(exception, dict):
        parts = [exception.get(key, 0) for key in ("level", "id", "index", "code")]
        candidate = "-".join(str(int(part or 0)).zfill(4) for part in parts)
        if candidate != "0000-0000-0000-0000":
            return candidate, str(exception.get("message") or "")
        return "", ""

    message = print_stats.get("message")
    if isinstance(message, str) and message.strip():
        try:
            parsed = json.loads(message)
        except ValueError:
            return "", message.strip()
        if isinstance(parsed, dict):
            code = ""
            if parsed.get("coded"):
                code = "-".join(group.strip().zfill(4) for group in str(parsed["coded"]).split("-"))
            return code, str(parsed.get("msg") or "")
    return "", ""


def _hex_color(value: Any) -> str | None:
    """Normalise ``#RRGGBBAA`` / ``RRGGBB`` into Bambuddy's ``RRGGBBAA``.

    Bambuddy's AMS rendering expects Bambu's 8-digit form without a leading
    ``#``; the U1 reports ``filament_color_rgba`` in assorted spellings.
    """
    match = re.match(r"^#?([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$", str(value or ""))
    if not match:
        return None
    return (match.group(1) + (match.group(2) or "FF")).upper()


def decode_toolheads(print_task_config: dict) -> list[dict]:
    """The four toolheads as AMS-shaped trays.

    Colours come from ``print_task_config`` — the touchscreen-assigned
    filament, which persists with the physical spool until it is unloaded —
    and NOT from ``filament_detect``, which only knows RFID-tagged official
    Snapmaker spools and leaves every third-party spool blank.
    """
    exists = print_task_config.get("filament_exist") or []
    colors = print_task_config.get("filament_color_rgba") or []
    types = print_task_config.get("filament_type") or []
    sub_types = print_task_config.get("filament_sub_type") or []
    official = print_task_config.get("filament_official") or []
    editable = print_task_config.get("filament_edit") or []

    trays = []
    for slot in range(TOOLHEAD_COUNT):
        loaded = bool(exists[slot]) if slot < len(exists) else False
        color = _hex_color(colors[slot]) if loaded and slot < len(colors) else None
        material = (types[slot] if slot < len(types) else None) if loaded else None
        sub_brand = sub_types[slot] if loaded and slot < len(sub_types) else None
        if sub_brand in ("NONE", ""):
            sub_brand = None
        trays.append(
            {
                "id": slot,
                "tray_color": color,
                "tray_type": material or None,
                "tray_sub_brands": sub_brand,
                # The U1 tracks no remaining length for a spool. -1 is
                # Bambuddy's "unknown", which the card renders as a blank bar
                # rather than as an empty spool.
                "remain": -1,
                "exists": loaded,
                # state 9 is the firmware-agnostic "physically empty slot"
                # signal the AMS serializer already understands.
                "state": 0 if loaded else 9,
                "is_official": bool(official[slot]) if slot < len(official) else False,
                # False means the colour is owned by an RFID tag and the
                # printer refuses to overwrite it without FORCE=1 — which
                # would also flip the slot to unofficial, so it is never done.
                "color_editable": bool(editable[slot]) if slot < len(editable) else True,
            }
        )
    return trays


def active_extruder(status: dict) -> int:
    toolhead = status.get("toolhead") or {}
    name = toolhead.get("extruder")
    if isinstance(name, str):
        suffix = name.replace("extruder", "").strip()
        if suffix.isdigit():
            return int(suffix)
        if name == "extruder":
            return 0
    return 0


def _temperatures(status: dict, active: int) -> dict:
    bed = status.get("heater_bed") or {}
    extruder_key = EXTRUDER_KEYS[active] if 0 <= active < len(EXTRUDER_KEYS) else "extruder"
    nozzle = status.get(extruder_key) or status.get("extruder") or {}

    def _round(value: Any) -> float:
        try:
            return round(float(value), 1)
        except (TypeError, ValueError):
            return 0.0

    nozzle_target = _round(nozzle.get("target"))
    bed_target = _round(bed.get("target"))
    temperatures = {
        "nozzle": _round(nozzle.get("temperature")),
        "nozzle_target": nozzle_target,
        "nozzle_heating": nozzle_target > 0,
        "bed": _round(bed.get("temperature")),
        "bed_target": bed_target,
        "bed_heating": bed_target > 0,
    }
    # Every toolhead's temperature, so the card can show the idle ones too.
    # Bambuddy's own dual-nozzle keys stop at nozzle_2; the rest ride along
    # under U1-specific names that only the U1 card section reads.
    for index, key in enumerate(EXTRUDER_KEYS):
        data = status.get(key)
        if not isinstance(data, dict):
            continue
        temperatures[f"toolhead_{index}"] = _round(data.get("temperature"))
        temperatures[f"toolhead_{index}_target"] = _round(data.get("target"))
    return temperatures


def estimate_remaining(status: dict, progress_fraction: float, slicer_estimate: float | None) -> int:
    """Remaining print time in whole minutes (Bambuddy's unit).

    File progress drives the estimate once a print is properly underway. Below
    2% the divisor is small enough that the figure swings by hours between two
    polls, so the slicer's own estimate — when the file's metadata carried one
    — is used for that opening stretch instead.
    """
    print_stats = status.get("print_stats") or {}
    try:
        elapsed = float(print_stats.get("print_duration") or 0.0)
    except (TypeError, ValueError):
        elapsed = 0.0

    if progress_fraction >= 0.02 and elapsed > 0:
        remaining_seconds = elapsed / progress_fraction - elapsed
    elif slicer_estimate:
        remaining_seconds = max(slicer_estimate - elapsed, 0.0)
    else:
        return 0
    return max(int(round(remaining_seconds / 60.0)), 0)


def build_state_fields(status: dict, *, slicer_estimate: float | None = None) -> dict:
    """Map one Moonraker status payload onto ``PrinterState`` field values.

    Returned as a plain dict so the driver can diff it against the previous
    poll and only fire callbacks for what actually moved.
    """
    print_stats = status.get("print_stats") or {}
    virtual_sdcard = status.get("virtual_sdcard") or {}
    display_status = status.get("display_status") or {}
    print_task_config = status.get("print_task_config") or {}
    gcode_move = status.get("gcode_move") or {}
    fan = status.get("fan") or {}
    exclude_object = status.get("exclude_object") or {}

    active = active_extruder(status)
    error_code, error_message = decode_error(print_stats)

    state = map_state(print_stats.get("state"))
    fault = klipper_fault(status)
    if fault:
        # A code the printer itself reported is more specific than Klipper's
        # generic one, so the message is only replaced when there is no code.
        state = fault[0]
        if not error_code:
            error_message = fault[1]

    progress_fraction = virtual_sdcard.get("progress")
    if not isinstance(progress_fraction, int | float):
        progress_fraction = display_status.get("progress")
    progress_fraction = float(progress_fraction) if isinstance(progress_fraction, int | float) else 0.0

    info = print_stats.get("info") or {}
    current_layer = info.get("current_layer")
    total_layer = info.get("total_layer")

    filename = print_stats.get("filename") or ""

    objects = exclude_object.get("objects") or []
    printable_objects = {}
    for index, obj in enumerate(objects):
        if isinstance(obj, dict) and obj.get("name"):
            printable_objects[str(index)] = obj["name"]

    trays = decode_toolheads(print_task_config)

    return {
        "state": state,
        "current_print": filename or None,
        "subtask_name": (filename.rsplit("/", 1)[-1] or None) if filename else None,
        "gcode_file": filename or None,
        "progress": round(progress_fraction * 100.0, 1),
        "remaining_time": estimate_remaining(status, progress_fraction, slicer_estimate),
        "layer_num": int(current_layer) if isinstance(current_layer, int | float) else 0,
        "total_layers": int(total_layer) if isinstance(total_layer, int | float) else 0,
        "temperatures": _temperatures(status, active),
        "active_extruder": active,
        "tray_now": active if trays[active]["exists"] else 255,
        "cooling_fan_speed": (
            int(round(float(fan.get("speed", 0)) * 100)) if isinstance(fan.get("speed"), int | float) else None
        ),
        "speed_level": _speed_level(gcode_move.get("speed_factor")),
        "printable_objects": printable_objects,
        "skipped_objects": list(exclude_object.get("excluded_objects") or []),
        "error_code": error_code,
        "error_message": error_message,
        "trays": trays,
    }


# Bambu exposes four speed presets; Klipper exposes a continuous percentage.
# The thresholds are the percentages Bambu's own presets correspond to, so the
# card's Silent/Standard/Sport/Ludicrous selector round-trips: picking one
# sends that percentage, and reading it back lands on the same preset.
SPEED_PERCENT_BY_LEVEL = {1: 50, 2: 100, 3: 124, 4: 166}


def _speed_level(speed_factor: Any) -> int:
    try:
        percent = float(speed_factor) * 100.0
    except (TypeError, ValueError):
        return 2
    if percent <= 62:
        return 1
    if percent <= 112:
        return 2
    if percent <= 145:
        return 3
    return 4


def synthetic_ams(trays: list[dict]) -> list[dict]:
    """Wrap the toolhead trays in the AMS shape ``printer_state_to_dict`` reads.

    ``humidity`` and the drying fields are deliberately absent: the U1 has no
    dryer, ``supports_drying`` is False for its model, and an AMS unit that
    reported a humidity of 0 would render as a bone-dry sensor rather than as
    a machine that has none.
    """
    return [
        {
            "id": 0,
            "tray": [
                {
                    "id": tray["id"],
                    "tray_color": tray["tray_color"],
                    "tray_type": tray["tray_type"],
                    "tray_sub_brands": tray["tray_sub_brands"],
                    "remain": tray["remain"],
                    "state": tray["state"],
                    "exists": tray["exists"],
                }
                for tray in trays
            ],
        }
    ]
