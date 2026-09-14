"""The Snapmaker U1 driver, against a stand-in Moonraker.

The fake records every G-code script and HTTP call and serves a status
payload the test can mutate between polls, so the transitions that matter —
print start, completion, the first poll after Bambuddy restarts — are driven
the way the real poll loop drives them.
"""

import asyncio

import pytest

from backend.app.services.printer_drivers.base import UnsupportedOperation, call_driver
from backend.app.services.printer_drivers.snapmaker_u1 import SnapmakerU1Driver
from backend.app.services.snapmaker.moonraker import MoonrakerError


class FakeMoonraker:
    """Enough of MoonrakerClient for the driver, with a recording surface."""

    def __init__(self, status: dict):
        self.base_url = "http://192.168.1.9"
        self.status = status
        self.scripts: list[str] = []
        self.actions: list[str] = []
        self.started: list[str] = []
        self.uploads: list[dict] = []
        self.metadata: dict = {}
        self.system = {"product_info": {"machine_type": "U1", "serial_number": "SM-1", "firmware_version": "1.6.0"}}
        self.closed = False

    async def query_objects(self, objects=None):
        if objects is None:
            return self.status
        return {name: self.status.get(name, {}) for name in objects}

    async def run_gcode(self, script, *, timeout=None):
        self.scripts.append(script)
        return True

    async def print_action(self, action, *, timeout=None):
        self.actions.append(action)
        return True

    async def start_print(self, filename, *, timeout=None):
        self.started.append(filename)
        return True

    async def emergency_stop(self):
        self.actions.append("emergency_stop")
        return True

    async def file_metadata(self, filename):
        return self.metadata

    async def system_info(self):
        return self.system

    async def server_info(self):
        return {"klippy_version": "v0.12.0"}

    async def upload(self, local_path, **kwargs):
        self.uploads.append({"path": str(local_path), **kwargs})
        return {"item": {"path": kwargs.get("remote_name")}}

    async def close(self):
        self.closed = True


def _payload(state="standby", filename="", progress=0.0, layer=0):
    return {
        "print_stats": {
            "state": state,
            "filename": filename,
            "print_duration": 120.0,
            "info": {"current_layer": layer, "total_layer": 100},
        },
        "virtual_sdcard": {"progress": progress},
        "display_status": {"progress": progress},
        "heater_bed": {"temperature": 60.0, "target": 60.0},
        "extruder": {"temperature": 210.0, "target": 210.0},
        "toolhead": {"extruder": "extruder"},
        "fan": {"speed": 0.0},
        "gcode_move": {"speed_factor": 1.0},
        "exclude_object": {"objects": [], "excluded_objects": []},
        "webhooks": {"state": "ready"},
        "print_task_config": {
            "filament_exist": [True, False, False, False],
            "filament_color_rgba": ["00AE42FF", "", "", ""],
            "filament_type": ["PLA", "", "", ""],
            "filament_sub_type": ["", "", "", ""],
            "filament_official": [False, False, False, False],
            "filament_edit": [True, True, True, True],
        },
    }


@pytest.fixture
def driver():
    events = {"start": [], "complete": [], "running_observed": [], "layers": [], "progress": []}
    d = SnapmakerU1Driver(
        ip_address="192.168.1.9",
        serial_number="SM-1",
        on_print_start=events["start"].append,
        on_print_complete=events["complete"].append,
        on_print_running_observed=events["running_observed"].append,
        on_layer_change=events["layers"].append,
        on_print_progress=events["progress"].append,
    )
    d.client = FakeMoonraker(_payload())
    d.events = events
    return d


class TestPolling:
    @pytest.mark.asyncio
    async def test_a_poll_fills_in_the_shared_printer_state(self, driver):
        driver.client.status = _payload("printing", "bracket.gcode", 0.5, 30)
        await driver._poll_once()

        assert driver.state.connected is True
        assert driver.state.state == "RUNNING"
        assert driver.state.progress == 50.0
        assert driver.state.layer_num == 30
        assert driver.state.temperatures["bed"] == 60.0
        # The four toolheads ride in the AMS field the card already renders.
        assert driver.state.raw_data["ams"][0]["tray"][0]["tray_color"] == "00AE42FF"
        assert driver.state.firmware_version == "1.6.0"

    @pytest.mark.asyncio
    async def test_a_print_already_running_at_startup_does_not_fire_print_start(self, driver):
        """Otherwise a Bambuddy restart re-archives and re-notifies every job."""
        driver.client.status = _payload("printing", "bracket.gcode", 0.5, 30)
        await driver._poll_once()

        assert driver.events["start"] == []
        assert len(driver.events["running_observed"]) == 1

    @pytest.mark.asyncio
    async def test_a_print_starting_while_connected_fires_print_start(self, driver):
        await driver._poll_once()  # idle first poll
        driver.client.status = _payload("printing", "bracket.gcode", 0.01, 1)
        await driver._poll_once()

        assert len(driver.events["start"]) == 1
        assert driver.events["start"][0]["filename"] == "bracket.gcode"

    @pytest.mark.asyncio
    async def test_finishing_a_print_reports_the_file_that_was_running(self, driver):
        await driver._poll_once()
        driver.client.status = _payload("printing", "bracket.gcode", 0.5, 30)
        await driver._poll_once()
        driver.client.status = _payload("complete", "bracket.gcode", 1.0, 100)
        await driver._poll_once()

        assert len(driver.events["complete"]) == 1
        assert driver.events["complete"][0]["status"] == "FINISH"
        assert driver.events["complete"][0]["filename"] == "bracket.gcode"

    @pytest.mark.asyncio
    async def test_a_cancelled_print_completes_as_failed(self, driver):
        await driver._poll_once()
        driver.client.status = _payload("printing", "bracket.gcode", 0.5, 30)
        await driver._poll_once()
        driver.client.status = _payload("cancelled", "bracket.gcode", 0.5, 30)
        await driver._poll_once()

        assert driver.events["complete"][0]["status"] == "FAILED"

    @pytest.mark.asyncio
    async def test_one_failed_poll_does_not_take_the_printer_offline(self, driver):
        await driver._poll_once()

        async def _boom(*_args, **_kwargs):
            raise MoonrakerError("connection reset")

        driver.client.query_objects = _boom
        await driver._poll_once()
        assert driver.state.connected is True, "a single dropped packet is not a printer going away"

        await driver._poll_once()
        await driver._poll_once()
        assert driver.state.connected is False

    @pytest.mark.asyncio
    async def test_a_slicer_estimate_is_fetched_once_per_file(self, driver):
        calls = []

        async def _metadata(filename):
            calls.append(filename)
            return {"estimated_time": 3600}

        driver.client.file_metadata = _metadata
        driver.client.status = _payload("printing", "bracket.gcode", 0.001, 1)
        await driver._poll_once()
        await driver._poll_once()

        assert calls == ["bracket.gcode"], "the estimate cannot change while the file prints"

    @pytest.mark.asyncio
    async def test_a_report_after_a_presumed_power_off_undoes_the_presumption(self, driver):
        await driver._poll_once()
        assert driver.mark_power_off() is True
        assert driver.state.connected is False

        await driver._poll_once()
        assert driver.state.connected is True


class TestControl:
    @pytest.mark.asyncio
    async def test_print_control_uses_moonrakers_own_routes(self, driver):
        await driver.pause_print()
        await driver.resume_print()
        await driver.stop_print()
        assert driver.client.actions == ["pause", "resume", "cancel"]

    @pytest.mark.asyncio
    async def test_temperatures_and_fan_translate_to_gcode(self, driver):
        await driver.set_bed_temperature(60)
        await driver.set_nozzle_temperature(215, nozzle=2)
        await driver.set_part_fan(50)
        assert driver.client.scripts == ["M140 S60", "M104 T2 S215", "M106 S128"]

    @pytest.mark.asyncio
    async def test_a_bed_target_above_the_firmware_limit_is_clamped(self, driver):
        """Stock firmware rejects >100 °C outright; clamping still heats."""
        await driver.set_bed_temperature(120)
        assert driver.client.scripts == ["M140 S100"]

    @pytest.mark.asyncio
    async def test_speed_presets_map_to_percentages(self, driver):
        await driver.set_print_speed(3)
        assert driver.client.scripts == ["M220 S124"]

    @pytest.mark.asyncio
    async def test_a_relative_move_restores_absolute_positioning(self, driver):
        """Leaving the machine in G91 would corrupt the next print."""
        await driver.move_axis("Z", 10, speed=600)
        assert driver.client.scripts == ["G91\nG1 Z10.00 F600\nG90"]

    @pytest.mark.asyncio
    async def test_the_auxiliary_fan_ids_are_declined_rather_than_faked(self, driver):
        with pytest.raises(UnsupportedOperation):
            await driver.set_fan_speed(2, 50)

    @pytest.mark.asyncio
    async def test_skip_objects_excludes_by_name(self, driver):
        driver.state.printable_objects = {"0": "cube", "1": "cone"}
        await driver.skip_objects([1])
        assert driver.client.scripts == ["EXCLUDE_OBJECT NAME=cone"]


class TestFilamentColour:
    @pytest.mark.asyncio
    async def test_writing_a_colour_is_confirmed_by_reading_it_back(self, driver):
        driver.client.status = _payload()
        original_query = driver.client.query_objects

        async def _query(objects=None):
            result = await original_query(objects)
            if objects and "print_task_config" in objects and driver.client.scripts:
                # After the write, the printer reports the new colour.
                result = {"print_task_config": {**result["print_task_config"], "filament_color_rgba": ["112233FF"]}}
            return result

        driver.client.query_objects = _query
        assert await driver.set_filament_color(0, "112233") == "#112233"
        assert "SET_PRINT_FILAMENT_CONFIG" in driver.client.scripts[0]
        assert "SAVE='1'" in driver.client.scripts[0]

    @pytest.mark.asyncio
    async def test_an_rfid_spool_is_refused_rather_than_forced(self, driver):
        payload = _payload()
        payload["print_task_config"]["filament_edit"] = [False, True, True, True]
        driver.client.status = payload
        with pytest.raises(MoonrakerError, match="RFID"):
            await driver.set_filament_color(0, "112233")
        assert driver.client.scripts == [], "FORCE=1 would also mark the spool unofficial"

    @pytest.mark.asyncio
    async def test_colours_cannot_be_changed_mid_print(self, driver):
        driver.client.status = _payload("printing", "a.gcode", 0.2)
        with pytest.raises(MoonrakerError, match="printing"):
            await driver.set_filament_color(0, "112233")

    @pytest.mark.asyncio
    async def test_an_empty_slot_has_no_colour_to_correct(self, driver):
        with pytest.raises(MoonrakerError, match="No filament"):
            await driver.set_filament_color(2, "112233")


class TestPrintStart:
    @pytest.mark.asyncio
    async def test_preferences_and_toolhead_mapping_go_out_before_the_print(self, driver):
        await driver.start_print("bracket.gcode", ams_mapping=[2, -1, 0], timelapse=True)

        script = driver.client.scripts[0]
        assert "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=0 MAP_EXTRUDER=2" in script
        assert "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=1" not in script, "-1 means the job does not use that slot"
        assert "SET_PRINT_EXTRUDER_MAP CONFIG_EXTRUDER=2 MAP_EXTRUDER=0" in script
        assert "SET_PRINT_USED_EXTRUDERS EXTRUDERS=0,2" in script
        assert "TIME_LAPSE_CAMERA=1" in script
        assert driver.client.started == ["bracket.gcode"]

    @pytest.mark.asyncio
    async def test_preferences_are_sent_even_with_no_mapping(self, driver):
        await driver.start_print("bracket.gcode")
        assert "SET_PRINT_PREFERENCES" in driver.client.scripts[0]
        assert "SET_PRINT_USED_EXTRUDERS" not in driver.client.scripts[0]

    @pytest.mark.asyncio
    async def test_a_slot_the_machine_does_not_have_is_refused(self, driver):
        with pytest.raises(MoonrakerError, match="does not exist"):
            await driver.start_print("bracket.gcode", ams_mapping=[7])
        assert driver.client.started == []

    @pytest.mark.asyncio
    async def test_a_3mf_is_refused_with_a_reason(self, driver, tmp_path):
        """Klipper prints G-code; an uploaded 3MF would list and then fail."""
        project = tmp_path / "bracket.3mf"
        project.write_bytes(b"PK\x03\x04")
        with pytest.raises(MoonrakerError, match="G-code"):
            await driver.upload_file(project)
        assert driver.client.uploads == []

    @pytest.mark.asyncio
    async def test_a_gcode_file_uploads(self, driver, tmp_path):
        job = tmp_path / "bracket.gcode"
        job.write_text("G28\n")
        assert await driver.upload_file(job) is True
        assert driver.client.uploads[0]["remote_name"] == "bracket.gcode"


class TestUnsupportedOperations:
    @pytest.mark.asyncio
    async def test_a_bambu_only_operation_names_itself(self, driver):
        with pytest.raises(UnsupportedOperation) as excinfo:
            await call_driver(driver, "ams_load_filament", 1)
        assert excinfo.value.operation == "ams_load_filament"
        assert "Snapmaker U1" in str(excinfo.value)

    def test_probing_for_an_unsupported_operation_answers_honestly(self, driver):
        """UnsupportedOperation derives from AttributeError so these still work."""
        assert getattr(driver, "get_kprofiles", None) is None
        assert hasattr(driver, "get_kprofiles") is False
        assert hasattr(driver, "pause_print") is True

    def test_drying_targets_exist_and_are_empty(self, driver):
        """PrinterManager reads this for every printer; a U1 has no dryer."""
        assert driver._drying_targets == {}


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_disconnect_stops_the_poll_loop(self, driver):
        driver.connect()
        await asyncio.sleep(0)
        assert driver._task is not None

        driver.disconnect()
        assert driver._task is None
        assert driver.state.connected is False

    @pytest.mark.asyncio
    async def test_a_wedged_poll_eventually_reads_as_offline(self, driver, monkeypatch):
        await driver._poll_once()
        assert driver.check_staleness() is True

        # Pretend the last success is older than the staleness window without
        # sleeping through it.
        driver._last_success -= 999
        assert driver.check_staleness() is False
