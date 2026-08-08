from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
SERVER = (ROOT / "swt_flask_project/flask_app/server.py").read_text(encoding="utf-8")
FIRMWARE = (ROOT / "swt_firmware_project/src/two_node_udp.cpp").read_text(encoding="utf-8")
TEMPLATE = (ROOT / "swt_flask_project/flask_app/templates/index.html").read_text(encoding="utf-8")
DEVICE_DETAIL_TEMPLATE = (ROOT / "swt_flask_project/flask_app/templates/device_detail.html").read_text(encoding="utf-8")


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


def test_scoped_pump_start_preserves_existing_duration_query():
    assert 'const separator=String(path).includes("?")?"&":"?"' in TEMPLATE
    assert 'path=`${path}?duration_minutes=${duration}`' in TEMPLATE


def test_device_detail_shows_optional_pump_confirmation_sensors():
    assert 'id="pumpConfirmationSensors"' in DEVICE_DETAIL_TEMPLATE
    assert "Starter-contactor auxiliary sensor" in DEVICE_DETAIL_TEMPLATE
    assert "Motor-current sensor" in DEVICE_DETAIL_TEMPLATE
    assert "Water-flow sensor" in DEVICE_DETAIL_TEMPLATE
    assert "Water-pressure sensor" in DEVICE_DETAIL_TEMPLATE
    assert '{label:"Starter Contactor Sensor"' in DEVICE_DETAIL_TEMPLATE
    assert '{label:"Motor Current Sensor"' in DEVICE_DETAIL_TEMPLATE
    assert '{label:"Water Flow Sensor"' in DEVICE_DETAIL_TEMPLATE
    assert '{label:"Water Pressure Sensor"' in DEVICE_DETAIL_TEMPLATE


def test_mysql_local_port_refusal_falls_back_to_standard_port(monkeypatch):
    flask_app_path = str(ROOT / "swt_flask_project/flask_app")
    if flask_app_path not in sys.path:
        sys.path.insert(0, flask_app_path)
    import server

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            return self

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    attempted_ports = []

    def fake_connect(**kwargs):
        attempted_ports.append(kwargs["port"])
        if kwargs["port"] == 3307:
            raise server.pymysql.err.OperationalError(2003, "connection refused")
        return FakeConnection()

    monkeypatch.setattr(server, "mysql_connection_config", lambda: {
        "host": "localhost", "port": 3307, "user": "test", "password": "test", "database": "test"
    })
    monkeypatch.setattr(server.pymysql, "connect", fake_connect)
    monkeypatch.setattr(server, "_MYSQL_RESOLVED_LOCAL_PORT", None)

    server.connect_mysql()
    server.connect_mysql()

    assert attempted_ports == [3307, 3306, 3306]
