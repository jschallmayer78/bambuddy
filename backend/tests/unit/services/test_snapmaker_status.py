"""Mapping a Moonraker payload onto Bambuddy's PrinterState.

The payload shapes here are the ones a real U1 sends (firmware 1.6.x), not
invented minimal dicts — the fields that are easy to get wrong are the ones
that only appear on real hardware: ``print_task_config``'s parallel arrays,
the two different error spellings, and Klipper's frozen ``print_stats``
after a shutdown.
"""

import pytest

from backend.app.services.snapmaker import status as u1


def _status(**overrides) -> dict:
    base = {
        "print_stats": {"state": "printing", "filename": "bracket.gcode", "print_duration": 600.0, "info": {}},
        "virtual_sdcard": {"progress": 0.25},
        "display_status": {"progress": 0.25},
        "heater_bed": {"temperature": 59.8, "target": 60.0},
        "extruder": {"temperature": 219.4, "target": 220.0},
        "extruder1": {"temperature": 24.0, "target": 0.0},
        "extruder2": {"temperature": 24.2, "target": 0.0},
        "extruder3": {"temperature": 23.9, "target": 0.0},
        "toolhead": {"extruder": "extruder"},
        "fan": {"speed": 0.6},
        "gcode_move": {"speed_factor": 1.0},
        "exclude_object": {"objects": [], "excluded_objects": []},
        "webhooks": {"state": "ready"},
        "print_task_config": {
            "filament_exist": [True, True, False, False],
            "filament_color_rgba": ["#FF6A13FF", "1A1A1AFF", "", ""],
            "filament_type": ["PLA", "PETG", "", ""],
            "filament_sub_type": ["Matte", "NONE", "", ""],
            "filament_official": [True, False, False, False],
            "filament_edit": [False, True, True, True],
        },
    }
    base.update(overrides)
    return base


class TestStateMapping:
    @pytest.mark.parametrize(
        ("klipper", "expected"),
        [
            ("standby", "IDLE"),
            ("printing", "RUNNING"),
            ("paused", "PAUSE"),
            ("complete", "FINISH"),
            ("cancelled", "FAILED"),
            ("error", "FAILED"),
            ("something_new", "unknown"),
        ],
    )
    def test_states_map_to_bambuddys_vocabulary(self, klipper, expected):
        assert u1.map_state(klipper) == expected

    def test_klipper_shutdown_beats_a_frozen_print_stats(self):
        """A shutdown freezes print_stats mid-print; that must not read as running.

        This is the case that matters for the queue: a printer reporting
        ``printing`` at 25% forever would keep its slot and never free up.
        """
        fields = u1.build_state_fields(
            _status(webhooks={"state": "shutdown", "state_message": "MCU 'mcu' shutdown: Timer too close\nmore"})
        )
        assert fields["state"] == "FAILED"
        assert fields["error_message"] == "MCU 'mcu' shutdown: Timer too close"


class TestErrorDecoding:
    def test_structured_exception_becomes_a_padded_code(self):
        code, message = u1.decode_error(
            {"exception": {"level": 2, "id": 13, "index": 1, "code": 7, "message": "Filament runout on T1"}}
        )
        assert code == "0002-0013-0001-0007"
        assert message == "Filament runout on T1"

    def test_all_zero_exception_is_not_an_error(self):
        assert u1.decode_error({"exception": {"level": 0, "id": 0, "index": 0, "code": 0}}) == ("", "")

    def test_legacy_json_message_is_parsed(self):
        code, message = u1.decode_error({"message": '{"coded": "2-13-1-7", "msg": "Filament runout"}'})
        assert code == "0002-0013-0001-0007"
        assert message == "Filament runout"

    def test_plain_message_survives_as_text(self):
        assert u1.decode_error({"message": "Heating failed"}) == ("", "Heating failed")


class TestToolheads:
    def test_colours_and_materials_come_from_print_task_config(self):
        trays = u1.decode_toolheads(_status()["print_task_config"])
        assert [t["exists"] for t in trays] == [True, True, False, False]
        # Normalised to Bambu's 8-digit form without a leading '#', which is
        # what the AMS graphic renders.
        assert trays[0]["tray_color"] == "FF6A13FF"
        assert trays[1]["tray_color"] == "1A1A1AFF"
        assert trays[0]["tray_type"] == "PLA"
        assert trays[0]["tray_sub_brands"] == "Matte"
        assert trays[1]["tray_sub_brands"] is None, "NONE means no sub-brand, not a sub-brand called NONE"

    def test_an_empty_slot_carries_no_colour_and_the_empty_state(self):
        trays = u1.decode_toolheads(_status()["print_task_config"])
        assert trays[2]["tray_color"] is None
        assert trays[2]["tray_type"] is None
        assert trays[2]["state"] == 9

    def test_rfid_spools_are_marked_uneditable(self):
        trays = u1.decode_toolheads(_status()["print_task_config"])
        assert trays[0]["color_editable"] is False
        assert trays[1]["color_editable"] is True

    def test_a_printer_that_reports_nothing_still_yields_four_slots(self):
        trays = u1.decode_toolheads({})
        assert len(trays) == u1.TOOLHEAD_COUNT
        assert all(t["exists"] is False for t in trays)

    def test_synthetic_ams_has_no_humidity(self):
        """The U1 has no dryer — a humidity of 0 would render as bone-dry."""
        unit = u1.synthetic_ams(u1.decode_toolheads(_status()["print_task_config"]))[0]
        assert unit["id"] == 0
        assert "humidity" not in unit
        assert len(unit["tray"]) == 4


class TestDerivedFields:
    def test_progress_layers_and_temperatures(self):
        fields = u1.build_state_fields(
            _status(
                print_stats={
                    "state": "printing",
                    "filename": "parts/bracket.gcode",
                    "print_duration": 600.0,
                    "info": {"current_layer": 42, "total_layer": 210},
                }
            )
        )
        assert fields["progress"] == 25.0
        assert fields["layer_num"] == 42
        assert fields["total_layers"] == 210
        assert fields["subtask_name"] == "bracket.gcode"
        assert fields["temperatures"]["bed"] == 59.8
        assert fields["temperatures"]["nozzle_target"] == 220.0
        assert fields["temperatures"]["nozzle_heating"] is True
        # Idle toolheads are reported too, under U1-specific keys.
        assert fields["temperatures"]["toolhead_2"] == 24.2

    def test_the_active_toolhead_supplies_the_nozzle_reading(self):
        fields = u1.build_state_fields(
            _status(toolhead={"extruder": "extruder2"}, extruder2={"temperature": 231.0, "target": 230.0})
        )
        assert fields["active_extruder"] == 2
        assert fields["temperatures"]["nozzle"] == 231.0

    def test_remaining_time_is_derived_from_file_progress(self):
        # 600 s elapsed at 25% -> 1800 s left -> 30 minutes.
        assert u1.build_state_fields(_status())["remaining_time"] == 30

    def test_the_slicer_estimate_covers_the_opening_percent(self):
        """Below 2% the progress-based figure swings by hours between polls."""
        payload = _status(
            virtual_sdcard={"progress": 0.005},
            print_stats={"state": "printing", "filename": "a.gcode", "print_duration": 30.0, "info": {}},
        )
        assert u1.build_state_fields(payload, slicer_estimate=3630.0)["remaining_time"] == 60

    def test_no_estimate_available_reports_zero_rather_than_a_guess(self):
        payload = _status(
            virtual_sdcard={"progress": 0.0},
            print_stats={"state": "printing", "filename": "a.gcode", "print_duration": 5.0, "info": {}},
        )
        assert u1.build_state_fields(payload)["remaining_time"] == 0

    def test_fan_percent_and_speed_preset_round_trip(self):
        assert u1.build_state_fields(_status())["cooling_fan_speed"] == 60
        for level, percent in u1.SPEED_PERCENT_BY_LEVEL.items():
            payload = _status(gcode_move={"speed_factor": percent / 100.0})
            assert u1.build_state_fields(payload)["speed_level"] == level

    def test_tray_now_is_255_when_the_active_toolhead_is_empty(self):
        payload = _status(toolhead={"extruder": "extruder3"})
        assert u1.build_state_fields(payload)["tray_now"] == 255

    def test_excludable_objects_are_exposed_by_index(self):
        payload = _status(
            exclude_object={"objects": [{"name": "cube"}, {"name": "cone"}], "excluded_objects": ["cube"]}
        )
        fields = u1.build_state_fields(payload)
        assert fields["printable_objects"] == {"0": "cube", "1": "cone"}
        assert fields["skipped_objects"] == ["cube"]
