from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "flask_app") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))

import server


EXPECTED_SETUPS = {
    "source_only",
    "borewell_upper",
    "municipal_direct",
    "municipal_source_gravity",
    "dual_source_gravity",
    "dual_source_pumped",
}


def test_every_setup_has_complete_features_and_automatic_scenario_levels():
    assert set(server.DEVICE_SETUP_TYPE_FEATURES) == EXPECTED_SETUPS
    for setup_type, preset in server.DEVICE_SETUP_TYPE_FEATURES.items():
        assert isinstance(preset["source_tank_monitoring_enabled"], bool)
        assert isinstance(preset["municipal_sensor_enabled"], bool)
        assert isinstance(preset["municipal_valve_enabled"], bool)
        assert isinstance(preset["source_outlet_valve_enabled"], bool)
        assert preset["auto_mode_enabled"] is True
        assert 0 <= preset["simulator_upper_level"] <= 100
        if preset["source_tank_monitoring_enabled"]:
            assert 0 <= preset["simulator_source_level"] <= 100
        assert preset["simulator_route"]


def test_borewell_setup_has_no_source_tank_or_municipal_hardware():
    preset = server.DEVICE_SETUP_TYPE_FEATURES["borewell_upper"]

    assert preset["source_tank_monitoring_enabled"] is False
    assert preset["municipal_sensor_enabled"] is False
    assert preset["municipal_valve_enabled"] is False
    assert preset["source_outlet_valve_enabled"] is False


def test_automatic_commands_reset_faults_and_enable_only_installed_features():
    for setup_type, preset in server.DEVICE_SETUP_TYPE_FEATURES.items():
        commands = server.automatic_simulator_commands_for_setup(setup_type, preset)
        assert commands[0] == "SIMULATOR_ON"
        assert "set:upper_sensor_fault:none" in commands
        assert "set:source_sensor_fault:none" in commands
        assert "set:pump_feedback_override:off" in commands
        assert "set:peer_drop_every:0" in commands
        assert f"set:simulator_level:{preset['simulator_upper_level']}" in commands
        assert ("MUNICIPAL_SIMULATOR_ON" in commands) == preset["municipal_sensor_enabled"]
        assert ("MUNICIPAL_VALVE_SIMULATOR_ON" in commands) == preset["municipal_valve_enabled"]
        assert ("SOURCE_OUTLET_VALVE_SIMULATOR_ON" in commands) == preset["source_outlet_valve_enabled"]


def test_custom_setup_does_not_auto_start_simulators():
    assert server.automatic_simulator_commands_for_setup("custom", {}) == []


def test_setup_type_is_serialized_and_persistable():
    payload = server.serialize_device_service_config(
        "swt-000-000-000-001",
        {"device_setup_type": "borewell_upper"},
    )
    assert payload["device_setup_type"] == "borewell_upper"


def test_device_detail_lists_every_setup_and_enables_auto_mode_on_selection():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    for setup_type in EXPECTED_SETUPS:
        assert f'value="{setup_type}"' in template
        assert f"{setup_type}:" in template
    assert 'setChecked("autoModeOption",preset.auto);' in template
    assert "borewell_upper:{source:false,municipal:false,inlet:false,outlet:false,auto:true}" in template


def test_configuration_route_persists_setup_and_queues_scenario_for_test_firmware():
    source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    route_start = source.index("def admin_device_detail_configuration(device_id):")
    route_end = source.index("@app.route", route_start)
    route = source[route_start:route_end]
    assert "device_setup_type=setup_type" in route
    assert "simulator_firmware_detected" in route
    assert "automatic_simulator_commands_for_setup(setup_type, updated_config)" in route
