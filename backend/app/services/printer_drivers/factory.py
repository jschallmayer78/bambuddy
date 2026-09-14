"""Pick the driver for a printer, and probe an address before one exists.

Modelled on ``services/git_providers/factory.py``: a name-keyed table plus one
lookup, so adding a third protocol is a new module and one entry rather than
another branch threaded through ``PrinterManager``.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_drivers.base import (
    PRINTER_TYPE_BAMBU,
    PRINTER_TYPE_SNAPMAKER_U1,
    normalize_printer_type,
)
from backend.app.services.printer_drivers.snapmaker_u1 import SnapmakerU1Driver

logger = logging.getLogger(__name__)

_DRIVERS: dict[str, type] = {
    PRINTER_TYPE_BAMBU: BambuMQTTClient,
    PRINTER_TYPE_SNAPMAKER_U1: SnapmakerU1Driver,
}

# Human-facing labels, used by the add-printer dialog and error messages.
DRIVER_LABELS: dict[str, str] = {
    PRINTER_TYPE_BAMBU: "Bambu Lab",
    PRINTER_TYPE_SNAPMAKER_U1: "Snapmaker U1",
}


def driver_class(printer_type: str | None) -> type:
    return _DRIVERS[normalize_printer_type(printer_type)]


def create_driver(printer_type: str | None, **kwargs: Any):
    """Build a live connection object for a printer.

    Every driver takes the same keyword arguments — the union of what any of
    them needs — and ignores the ones that mean nothing to it. That keeps
    ``PrinterManager.connect_printer`` free of per-vendor argument juggling:
    it wires up the callbacks once and hands them over.
    """
    return driver_class(printer_type)(**kwargs)


async def probe_printer(
    printer_type: str | None,
    ip_address: str,
    serial_number: str | None = None,
    access_code: str | None = None,
    *,
    timeout: float = 10.0,
) -> dict:
    """Check that a printer is reachable and is what it claims to be.

    Returns ``{"success": bool, "state": str|None, "model": str|None,
    "serial": str|None, "reason": str|None}``. Used before a printer row is
    written, so a typo in an address is rejected at the dialog rather than
    becoming a permanently red card.
    """
    kind = normalize_printer_type(printer_type)
    if kind == PRINTER_TYPE_SNAPMAKER_U1:
        from backend.app.services.printer_drivers.snapmaker_u1 import probe as u1_probe

        return await u1_probe(ip_address, token=access_code or None, timeout=timeout)

    # Bambu keeps its existing probe, which owns the MQTT handshake, its
    # timeouts and its own reason strings.
    from backend.app.services.printer_manager import printer_manager

    result = await printer_manager.test_connection(ip_address, serial_number or "", access_code or "")
    result.setdefault("serial", serial_number)
    return result
