from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
DASHBOARD_TEMPLATE = PROJECT_ROOT / "flask_app" / "templates" / "index.html"


def test_telemetry_accepts_main_sensor_aliases_before_alert_evaluation():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "MAIN_TANK_ALIAS_FIELDS" in server_source
    assert '"sensor": ("main_sensor", "main_tank_sensor")' in server_source
    assert "apply_main_tank_aliases(cleaned)" in server_source
    assert "apply_main_tank_aliases(data, include_aliases=True)" in server_source


def test_alert_resolution_cannot_be_skipped_or_leave_duplicate_active_rows():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "if not active:\n        return False" in server_source
    assert "WHERE kind = ? AND COALESCE(device_id, '') = COALESCE(?, '') AND active = 1" in server_source
    assert "SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP" in server_source


def test_dashboard_sensor_warnings_are_specific_and_customer_safe():
    template = DASHBOARD_TEMPLATE.read_text(encoding="utf-8")

    assert "HC-SR04 sensor is not responding." not in template
    assert "Main tank sensor is ${sensorRaw}." in template
    assert "Source tank sensor is ${sourceState.lowerSensorRaw}." in template
    assert "Last synced ${localizedLastSync}" in template
