"""Files on a printer, whichever protocol it speaks.

Bambu machines serve their storage over implicit FTPS (``services/bambu_ftp``)
and a Snapmaker U1 over Moonraker's file API. The two have nothing in common
at the wire level but exactly the same job here — list, fetch, delete, report
free space, upload — so this module is the one place that picks.

The file operations deliberately do not go through the live driver. A user
browsing a printer's storage, and the queue uploading to it, must work while
the printer is merely reachable; tying them to a connected driver would make
"the card is grey" also mean "you cannot see your files". Each call opens its
own short-lived connection, which is what the FTPS path has always done.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from backend.app.services.printer_drivers.base import (
    PRINTER_TYPE_SNAPMAKER_U1,
    normalize_printer_type,
)

logger = logging.getLogger(__name__)


def is_snapmaker(printer) -> bool:
    return normalize_printer_type(getattr(printer, "printer_type", None)) == PRINTER_TYPE_SNAPMAKER_U1


def _moonraker(printer):
    from backend.app.services.snapmaker.moonraker import MoonrakerClient

    return MoonrakerClient(printer.ip_address, token=(printer.access_code or None))


async def list_files(printer, path: str = "/") -> list[dict]:
    """Files on the printer, as ``{name, path, size, date, is_dir}`` dicts.

    Moonraker returns the whole ``gcodes`` root flat, with subfolders encoded
    in each entry's path, so ``path`` narrows the listing rather than
    navigating — which is what the file browser does with the result anyway.
    """
    client = _moonraker(printer)
    try:
        entries = await client.list_files()
    finally:
        await client.close()

    prefix = path.strip("/")
    files = []
    for entry in entries:
        name = entry.get("path") or entry.get("filename") or ""
        if not name or (prefix and not name.startswith(prefix + "/")):
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


async def download_file(printer, remote_path: str) -> bytes:
    client = _moonraker(printer)
    try:
        return await client.download("gcodes", remote_path)
    finally:
        await client.close()


async def delete_file(printer, remote_path: str) -> bool:
    client = _moonraker(printer)
    try:
        return await client.delete_file("gcodes", remote_path)
    finally:
        await client.close()


async def storage_info(printer) -> dict:
    """Free/used bytes, in the shape the storage endpoint already returns.

    Moonraker reports disk usage on the directory listing rather than through
    a dedicated endpoint, so the root's own listing is what answers this.
    """
    client = _moonraker(printer)
    try:
        result = await client.request("GET", "/server/files/directory", params={"path": "gcodes"})
    finally:
        await client.close()
    usage = (result or {}).get("disk_usage") or {}
    return {
        "used_bytes": usage.get("used"),
        "free_bytes": usage.get("free"),
        "total_bytes": usage.get("total"),
    }


async def upload_file(
    printer,
    local_path,
    remote_name: str,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> bool:
    """Put a sliced file on the printer, ready to print.

    Raises when the file is not something the machine can execute. Klipper
    prints G-code: a 3MF uploaded here would be listed by the printer and then
    fail to open, which is a worse outcome than a refusal that says why.
    """
    from backend.app.services.printer_drivers.snapmaker_u1 import PRINTABLE_SUFFIXES
    from backend.app.services.snapmaker.moonraker import MoonrakerError

    if not remote_name.lower().endswith(PRINTABLE_SUFFIXES):
        raise MoonrakerError(
            f"The Snapmaker U1 prints G-code, and '{remote_name}' is not a G-code file. "
            "Slice the model for the U1 and queue the resulting .gcode."
        )

    client = _moonraker(printer)
    try:
        await client.upload(
            local_path,
            remote_name=remote_name,
            timeout=None,
            progress_callback=progress_callback,
        )
        return True
    finally:
        await client.close()


async def thumbnail(printer, remote_path: str) -> bytes | None:
    """Largest embedded thumbnail Moonraker extracted for a sliced file."""
    client = _moonraker(printer)
    try:
        metadata = await client.file_metadata(remote_path.lstrip("/"))
        thumbnails = metadata.get("thumbnails") or []
        if not thumbnails:
            return None
        largest = max(thumbnails, key=lambda t: int(t.get("size") or 0))
        relative = largest.get("relative_path")
        if not relative:
            return None
        return await client.download("gcodes", relative)
    finally:
        await client.close()
