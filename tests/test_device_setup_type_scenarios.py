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
    "dual_source_pumped",
}


def test_every_setup_has_complete_features_and_automatic_scenario_levels():
    assert set(server.DEVICE_SETUP_TYPE_FEATURES) == EXPECTED_SETUPS
    for setup_type, preset in server.DEVICE_SETUP_TYPE_FEATURES.items():
        assert isinstance(preset["source_tank_monitoring_enabled"], bool)
        assert isinstance(preset["municipal_sensor_enabled"], bool)
        assert isinstance(preset["municipal_valve_enabled"], bool)
        assert isinstance(preset["source_outlet_valve_enabled"], bool)
        assert preset["auto_mode_enabled"] is False
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


def test_no_source_setup_tolerates_previously_enabled_source_monitoring():
    for setup_type in ("borewell_upper", "municipal_direct"):
        stale_config = dict(server.DEVICE_SETUP_TYPE_FEATURES[setup_type], source_tank_monitoring_enabled=True)
        commands = server.automatic_simulator_commands_for_setup(setup_type, stale_config)
        assert commands[0] == "SIMULATOR_ON"
        assert not any(command.startswith("set:simulator_lower_level:") for command in commands)


def test_configuration_change_queues_saved_setup_instead_of_old_live_settings(monkeypatch):
    import inspect

    device_id = "swt-test-000-000-008"
    old_snapshot = {"device_id": device_id, "simulator": True, "lower_tank_service": "ON"}
    saved = dict(server.DEVICE_SETUP_TYPE_FEATURES["borewell_upper"], device_id=device_id)
    queued = []
    monkeypatch.setattr(server, "current_scope_device_id", lambda value: value)
    monkeypatch.setattr(server, "fetch_device_snapshot", lambda _: old_snapshot)
    monkeypatch.setattr(server, "upsert_device_service_config", lambda *args, **kwargs: saved.update(kwargs) or saved)
    monkeypatch.setattr(server, "fetch_device_service_config", lambda _id, snapshot=None:
                        dict(saved, source_tank_monitoring_enabled=True) if snapshot else dict(saved))
    monkeypatch.setattr(server, "set_device_multi_tank_enabled", lambda *args: None)
    monkeypatch.setattr(server, "save_device_destination_tanks", lambda *args: None)
    monkeypatch.setattr(server, "enforce_active_platform_session_limit", lambda *args, **kwargs: 0)
    monkeypatch.setattr(server, "safe_queue_device_detail_command", lambda command, *_args:
                        (queued.append(command) or {"command": command}, ""))
    monkeypatch.setattr(server, "disable_orphaned_device_simulators", lambda *args: ([], []))
    monkeypatch.setattr(server, "log_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(server, "current_actor_username", lambda: "test-admin")
    with server.app.test_request_context(
        f"/devices/{device_id}/configuration", method="POST",
        data={"device_setup_type": "borewell_upper", "auto_mode_enabled": "1"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        response, code = inspect.unwrap(server.admin_device_detail_configuration)(device_id)
    assert code == 200
    assert response.get_json()["service_config"]["source_tank_monitoring_enabled"] is False
    assert server.build_device_service_command(saved) in queued
    assert not any(item.startswith("set:simulator_lower_level:") for item in queued)


def test_setup_type_is_serialized_and_persistable():
    payload = server.serialize_device_service_config(
        "swt-000-000-000-001",
        {"device_setup_type": "borewell_upper"},
    )
    assert payload["device_setup_type"] == "borewell_upper"


def test_device_detail_setup_selection_does_not_enable_auto_mode():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    for setup_type in EXPECTED_SETUPS:
        assert f'value="{setup_type}"' in template
        assert f"{setup_type}:" in template
    assert 'setChecked("autoModeOption",preset.auto);' not in template
    assert "borewell_upper:{source:false,municipal:false,inlet:false,outlet:false}" in template


def test_new_survey_registration_keeps_auto_mode_off_until_device_detail():
    registration = (PROJECT_ROOT / "flask_app" / "templates" / "admin_survey_response_detail.html").read_text(encoding="utf-8")
    assert 'name="auto_mode_enabled"' not in registration
    assert "Auto Start/Stop stays disabled at registration." in registration


def test_configuration_route_persists_setup_and_queues_scenario_for_test_firmware():
    source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    route_start = source.index("def admin_device_detail_configuration(device_id):")
    route_end = source.index("@app.route", route_start)
    route = source[route_start:route_end]
    assert "device_setup_type=setup_type" in route
    assert "simulator_firmware_detected" in route
    assert "automatic_simulator_commands_for_setup(setup_type, updated_config)" in route
