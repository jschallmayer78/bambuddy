"""The seam between Bambuddy and a printer's protocol.

Bambuddy grew around one machine family, and ``BambuMQTTClient`` became the
de-facto driver interface: ``printer_manager.get_client(id)`` hands it out and
routes, the scheduler and half a dozen services call methods on it directly.
Rather than invert that everywhere at once, this package makes the shape
explicit and lets a second protocol satisfy it.

Three rules follow from keeping the existing surface:

1. **A driver quacks like the MQTT client.** It exposes ``state``,
   ``connect``/``disconnect``, ``check_staleness`` and the control methods
   under the names the call sites already use, so nothing has to learn a new
   vocabulary for a printer that supports the same operation.

2. **What a driver cannot do raises, and raises early.** Bambuddy has a large
   Bambu-only surface — AMS slots, K-profiles, HMS actions, drying, the
   virtual printer. :class:`UnsupportedOperation` subclasses ``AttributeError``
   on purpose: a plain ``getattr(client, name, None)`` probe and ``hasattr``
   keep answering honestly, while a direct call surfaces as HTTP 501 with the
   operation's name instead of a 500 traceback.

3. **A driver method may be async.** MQTT publishes are fire-and-forget and
   return instantly, so the Bambu client's methods are synchronous; anything
   HTTP-based is not, and blocking the event loop for the minute a
   ``CANCEL_PRINT`` can take is not an option. Call sites that want to work on
   both go through :func:`call_driver`, which awaits when there is something
   to await, or — where the call site is itself synchronous and ignores the
   result — :func:`dispatch_driver`. Sync drivers are unaffected by either,
   including the ``MagicMock`` ones the test suite injects.
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol, runtime_checkable

# Value of ``Printer.printer_type``. Existing rows predate the column and read
# back as NULL, so every consumer treats NULL as BAMBU — that is what they were.
PRINTER_TYPE_BAMBU = "bambu"
PRINTER_TYPE_SNAPMAKER_U1 = "snapmaker_u1"

PRINTER_TYPES = (PRINTER_TYPE_BAMBU, PRINTER_TYPE_SNAPMAKER_U1)


def normalize_printer_type(value: str | None) -> str:
    """Coerce a stored/submitted printer type to a known one.

    Unknown values fall back to Bambu rather than raising: a row written by a
    newer build that someone then downgraded should still connect as the
    machine Bambuddy has always assumed, not break the printer list.
    """
    candidate = (value or "").strip().lower()
    return candidate if candidate in PRINTER_TYPES else PRINTER_TYPE_BAMBU


class UnsupportedOperation(AttributeError):
    """This printer's protocol has no equivalent of the requested operation."""

    def __init__(self, operation: str, printer_type: str | None = None):
        self.operation = operation
        self.printer_type = printer_type or "this printer"
        super().__init__(f"{self.printer_type} does not support '{operation}'")


@runtime_checkable
class PrinterDriver(Protocol):
    """The narrow contract every driver satisfies.

    Deliberately small. It is the part ``PrinterManager`` itself relies on;
    everything beyond it is negotiated per call site through
    :func:`call_driver` and :class:`UnsupportedOperation`.
    """

    state: Any  # PrinterState
    ip_address: str
    serial_number: str
    model: str | None

    def connect(self, loop: Any = None) -> Any: ...

    def disconnect(self, timeout: float = 0) -> Any: ...

    def check_staleness(self) -> bool: ...


async def call_driver(client: Any, operation: str, /, *args, **kwargs):
    """Invoke ``operation`` on a driver, awaiting the result when needed.

    Raises :class:`UnsupportedOperation` when the driver has no such method,
    so a route can answer 501 instead of 500.
    """
    if client is None:
        raise UnsupportedOperation(operation)
    try:
        method = getattr(client, operation)
    except UnsupportedOperation:
        raise
    except AttributeError as exc:
        raise UnsupportedOperation(operation) from exc
    if not callable(method):
        raise UnsupportedOperation(operation)
    result = method(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def dispatch_driver(client: Any, operation: str, /, *args, **kwargs) -> bool:
    """Fire a driver operation from *synchronous* code.

    Some of Bambuddy's comfort features — preheat, keep-warm, the dispatch
    rollback that turns heaters back off — live in synchronous methods and
    ignore the return value; an MQTT publish is instant, so they never needed
    to await anything. An HTTP driver's methods are coroutines, and a
    coroutine dropped on the floor there means the bed silently never heats.

    So: sync drivers are called inline and their result returned. Async ones
    are scheduled on the running loop, and the call reports True for "issued"
    — the outcome is logged by the task rather than returned, because these
    call sites have nothing to do with it. Converting their whole call chain
    to async would be a far larger change than the feature warrants.

    An operation the driver does not have returns False instead of raising:
    a printer with no chamber heater is not an error in a preheat sweep.
    """
    import asyncio
    import logging

    method = getattr(client, operation, None) if client is not None else None
    if not callable(method):
        return False

    result = method(*args, **kwargs)
    if not inspect.isawaitable(result):
        return bool(result)

    async def _run():
        try:
            await result
        except Exception as exc:  # pragma: no cover - logged, not raised
            logging.getLogger(__name__).warning("Driver operation %s failed: %s", operation, exc)

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        # No loop to schedule on (a sync context outside the app). Close the
        # coroutine so it does not warn about never being awaited.
        result.close()
        return False
    return True


def supports(client: Any, operation: str) -> bool:
    """Whether a driver implements ``operation`` at all."""
    return callable(getattr(client, operation, None))
