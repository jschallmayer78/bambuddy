"""Where a printer's picture comes from.

Bambuddy used to ask one question — "is an external camera configured?" — and
branch between the user's URL and the Bambu-native protocols. A Snapmaker U1
has a camera but neither answer fits: it is built in, so requiring a user to
paste a URL would be wrong, and it speaks Moonraker rather than RTSP or
Bambu's chamber-image protocol.

So the question becomes "what camera does this printer have?", answered once,
here, and consulted by every capture path. An explicitly configured external
camera still wins everywhere — that is what a user reaches for when the
built-in one is not good enough, and on a U1 running community firmware with
a real WebRTC/RTSP stream it is the better source.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.app.services.external_camera import CAMERA_TYPE_SNAPMAKER_U1
from backend.app.services.printer_drivers.base import (
    PRINTER_TYPE_SNAPMAKER_U1,
    normalize_printer_type,
)


@dataclass(frozen=True)
class CameraSource:
    """A camera reachable through ``services.external_camera``."""

    url: str | None = None
    type: str | None = None
    snapshot_url: str | None = None
    # True when this came from the printer's own protocol rather than from a
    # URL the user typed. Callers that offer "test this external camera" or
    # render its settings need to tell the two apart.
    built_in: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.url and self.type)


NO_CAMERA = CameraSource()


def resolve_camera_source(printer) -> CameraSource:
    """The camera to use for ``printer``, or :data:`NO_CAMERA`.

    ``NO_CAMERA`` does not mean "no picture": for a Bambu printer it means the
    native chamber-image or RTSP path applies, which the camera routes handle
    themselves. It means "nothing here that the external-camera machinery can
    open".
    """
    if getattr(printer, "external_camera_enabled", False) and getattr(printer, "external_camera_url", None):
        return CameraSource(
            url=printer.external_camera_url,
            # The column is nullable and predates the type selector, so rows
            # written before it exists carry a URL and no type. MJPEG is what
            # every consumer already assumed for those.
            type=printer.external_camera_type or "mjpeg",
            snapshot_url=getattr(printer, "external_camera_snapshot_url", None),
            built_in=False,
        )

    if normalize_printer_type(getattr(printer, "printer_type", None)) == PRINTER_TYPE_SNAPMAKER_U1:
        # Addressed by the printer's Moonraker base URL — the camera module
        # builds its own client from it.
        return CameraSource(
            url=f"http://{printer.ip_address}",
            type=CAMERA_TYPE_SNAPMAKER_U1,
            snapshot_url=None,
            built_in=True,
        )

    return NO_CAMERA
