"""Adding and driving a Snapmaker U1 through the API.

These cover the seams a second protocol actually touches: the add-printer
probe, the credential rules, what a control route does with an operation the
machine has no equivalent for, and the camera source a U1 gets without anyone
configuring one.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.models.printer import Printer


@pytest.fixture
def snapmaker_probe():
    """A U1 answering its Moonraker."""
    with patch(
        "backend.app.services.printer_drivers.snapmaker_u1.probe",
        new=AsyncMock(
            return_value={
                "success": True,
                "model": "U1",
                "serial": "SM-U1-0001",
                "name": "Snapmaker U1",
                "state": "IDLE",
                "reason": None,
            }
        ),
    ) as probe:
        yield probe


class TestAddingASnapmaker:
    @pytest.mark.asyncio
    async def test_a_u1_is_added_without_an_access_code(self, async_client: AsyncClient, snapmaker_probe, db_session):
        """Moonraker on a private LAN is unauthenticated — there is no code."""
        with patch(
            "backend.app.services.printer_manager.printer_manager.connect_printer",
            new=AsyncMock(return_value=True),
        ):
            response = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "Workshop U1",
                    "serial_number": "SM-U1-0001",
                    "ip_address": "192.168.1.9",
                    "printer_type": "snapmaker_u1",
                    "model": "U1",
                },
            )

        assert response.status_code == 200, response.text
        assert response.json()["printer_type"] == "snapmaker_u1"

        saved = (await db_session.execute(select(Printer).where(Printer.serial_number == "SM-U1-0001"))).scalar_one()
        assert saved.printer_type == "snapmaker_u1"
        assert saved.access_code == ""

    @pytest.mark.asyncio
    async def test_a_bambu_still_requires_its_access_code(self, async_client: AsyncClient):
        """It is the MQTT password; without it the printer is unreachable."""
        response = await async_client.post(
            "/api/v1/printers/",
            json={
                "name": "X1C",
                "serial_number": "00M09A000000001",
                "ip_address": "192.168.1.50",
                "printer_type": "bambu",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_an_unreachable_u1_is_not_persisted_and_says_why(self, async_client: AsyncClient, db_session):
        with patch(
            "backend.app.services.printer_drivers.snapmaker_u1.probe",
            new=AsyncMock(return_value={"success": False, "reason": "/machine/system_info: HTTP 404"}),
        ):
            response = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "Ghost",
                    "serial_number": "SM-U1-GHOST",
                    "ip_address": "192.168.1.99",
                    "printer_type": "snapmaker_u1",
                },
            )

        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "printer_connection_failed"
        # The driver's own reason, not Bambu's advice about LAN-only mode.
        assert "system_info" in detail["message"]
        assert "access code" not in detail["message"].lower()

        rows = (await db_session.execute(select(Printer).where(Printer.serial_number == "SM-U1-GHOST"))).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_a_printer_with_no_type_is_a_bambu(self, async_client: AsyncClient, printer_factory):
        """Rows and requests predating the column keep their old meaning."""
        printer = await printer_factory(name="Legacy", printer_type=None)
        response = await async_client.get(f"/api/v1/printers/{printer.id}")
        assert response.status_code == 200
        assert response.json()["printer_type"] == "bambu"

    @pytest.mark.asyncio
    async def test_an_explicit_moonraker_port_is_accepted(self, async_client: AsyncClient, snapmaker_probe):
        """Moonraker also listens on 7125; the address field has to allow it."""
        with patch(
            "backend.app.services.printer_manager.printer_manager.connect_printer",
            new=AsyncMock(return_value=True),
        ):
            response = await async_client.post(
                "/api/v1/printers/",
                json={
                    "name": "U1 on 7125",
                    "serial_number": "SM-U1-7125",
                    "ip_address": "192.168.1.9:7125",
                    "printer_type": "snapmaker_u1",
                },
            )
        assert response.status_code == 200, response.text


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_a_bambu_only_control_answers_501_naming_the_operation(
        self, async_client: AsyncClient, printer_factory
    ):
        """Not 500: the request was fine, the machine simply has no chamber light."""
        from backend.app.services.printer_drivers.snapmaker_u1 import SnapmakerU1Driver
        from backend.app.services.printer_manager import printer_manager

        printer = await printer_factory(name="U1", printer_type="snapmaker_u1", model="U1", access_code="")
        driver = SnapmakerU1Driver(ip_address=printer.ip_address, serial_number=printer.serial_number)
        printer_manager._clients[printer.id] = driver
        try:
            response = await async_client.post(f"/api/v1/printers/{printer.id}/chamber-light?on=true")
        finally:
            printer_manager._clients.pop(printer.id, None)

        assert response.status_code == 501
        detail = response.json()["detail"]
        assert detail["code"] == "operation_not_supported"
        assert detail["operation"] == "set_chamber_light"


class TestCameraSource:
    def test_a_u1_gets_its_built_in_camera_without_configuration(self):
        from backend.app.services.camera_source import resolve_camera_source

        printer = Printer(
            name="U1",
            serial_number="SM-U1-0002",
            ip_address="192.168.1.9",
            access_code="",
            printer_type="snapmaker_u1",
        )
        source = resolve_camera_source(printer)
        assert source.usable is True
        assert source.type == "snapmaker_u1"
        assert source.url == "http://192.168.1.9"
        assert source.built_in is True

    def test_a_configured_external_camera_still_wins(self):
        """Community firmware with a real stream is the better source."""
        from backend.app.services.camera_source import resolve_camera_source

        printer = Printer(
            name="U1",
            serial_number="SM-U1-0003",
            ip_address="192.168.1.9",
            access_code="",
            printer_type="snapmaker_u1",
            external_camera_enabled=True,
            external_camera_url="http://192.168.1.9:8080/stream",
            external_camera_type="mjpeg",
        )
        source = resolve_camera_source(printer)
        assert source.type == "mjpeg"
        assert source.built_in is False

    def test_a_bambu_has_no_external_source_by_default(self):
        """None here means the native RTSP / chamber-image path applies."""
        from backend.app.services.camera_source import resolve_camera_source

        printer = Printer(
            name="X1C", serial_number="00M09A000000002", ip_address="192.168.1.50", access_code="12345678"
        )
        assert resolve_camera_source(printer).usable is False
