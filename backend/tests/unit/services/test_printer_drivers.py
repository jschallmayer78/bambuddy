"""The driver seam itself: type normalisation, the factory, and call_driver."""

from unittest.mock import MagicMock

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_drivers.base import (
    PRINTER_TYPE_BAMBU,
    PRINTER_TYPE_SNAPMAKER_U1,
    UnsupportedOperation,
    call_driver,
    normalize_printer_type,
    supports,
)
from backend.app.services.printer_drivers.factory import create_driver, driver_class
from backend.app.services.printer_drivers.snapmaker_u1 import SnapmakerU1Driver


class TestPrinterType:
    @pytest.mark.parametrize("value", [None, "", "  ", "bambu", "BAMBU", "something_else"])
    def test_anything_unrecognised_reads_as_bambu(self, value):
        """Every row written before the column existed is a Bambu machine."""
        assert normalize_printer_type(value) == PRINTER_TYPE_BAMBU

    def test_a_known_type_survives_case_and_padding(self):
        assert normalize_printer_type("  Snapmaker_U1 ") == PRINTER_TYPE_SNAPMAKER_U1


class TestFactory:
    def test_each_type_maps_to_its_driver(self):
        assert driver_class(PRINTER_TYPE_BAMBU) is BambuMQTTClient
        assert driver_class(PRINTER_TYPE_SNAPMAKER_U1) is SnapmakerU1Driver
        assert driver_class(None) is BambuMQTTClient

    def test_a_driver_ignores_arguments_that_mean_nothing_to_it(self):
        """connect_printer passes one keyword set for every protocol."""
        driver = create_driver(
            PRINTER_TYPE_SNAPMAKER_U1,
            ip_address="192.168.1.9",
            serial_number="SM-1",
            access_code="",
            model="U1",
            on_state_change=lambda _state: None,
            on_ams_change=lambda _ams: None,
            on_drying_complete=lambda _ams_id: None,  # Bambu-only; harmless here
        )
        assert isinstance(driver, SnapmakerU1Driver)
        assert driver.ip_address == "192.168.1.9"


class TestCallDriver:
    @pytest.mark.asyncio
    async def test_a_synchronous_driver_is_called_directly(self):
        client = MagicMock()
        client.pause_print.return_value = True
        assert await call_driver(client, "pause_print") is True
        client.pause_print.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_an_async_driver_is_awaited(self):
        class AsyncDriver:
            async def pause_print(self):
                return "paused"

        assert await call_driver(AsyncDriver(), "pause_print") == "paused"

    @pytest.mark.asyncio
    async def test_a_missing_operation_becomes_unsupported_not_attribute_error(self):
        class Bare:
            pass

        with pytest.raises(UnsupportedOperation):
            await call_driver(Bare(), "set_chamber_light", True)

    @pytest.mark.asyncio
    async def test_no_connected_printer_is_unsupported_too(self):
        with pytest.raises(UnsupportedOperation):
            await call_driver(None, "pause_print")

    def test_supports_reports_what_a_driver_can_do(self):
        driver = SnapmakerU1Driver(ip_address="192.168.1.9", serial_number="SM-1")
        assert supports(driver, "pause_print") is True
        assert supports(driver, "ams_load_filament") is False
