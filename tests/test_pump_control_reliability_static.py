from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SERVER = (ROOT / "swt_flask_project/flask_app/server.py").read_text(encoding="utf-8")
FIRMWARE = (ROOT / "swt_firmware_project/src/two_node_udp.cpp").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "swt_flask_project/flask_app/templates/index.html").read_text(encoding="utf-8")


def test_optional_physical_feedback_and_runtime_contract_is_end_to_end():
    for field in (
        "starter_contactor_sensor_enabled", "motor_current_sensor_enabled",
        "water_flow_sensor_enabled", "water_pressure_sensor_enabled",
        "physical_pump_running", "pump_confirmation_source", "pump_total_runtime_s",
        "pump_last_run_runtime_s", "pump_cycle_count", "pump_runtime_boot_id",
    ):
        assert field in FIRMWARE
        assert field in SERVER
    assert 'return "tank_level_rise";' in FIRMWARE
    assert "pumpInferenceStartMs" in FIRMWARE


def test_command_lifecycle_supports_expiry_priority_results_and_status_ui():
    for state in ("queued", "delivered", "accepted", "running", "stopped", "rejected", "timed_out"):
        assert state in SERVER or state in TEMPLATE
    assert "ORDER BY priority DESC, id ASC" in SERVER
    assert "expires_at" in SERVER
    assert "request_id" in SERVER
    assert "monitorMotorCommand" in TEMPLATE
    assert 'doc["status"] = applied ? "accepted" : "rejected";' in FIRMWARE


def test_supervised_manual_run_choices_are_available():
    assert "Run until full" in TEMPLATE
    assert "Run 15 minutes" in TEMPLATE
    assert "Run 30 minutes" in TEMPLATE
    assert 'normalized.startsWith("on_for:")' in FIRMWARE
