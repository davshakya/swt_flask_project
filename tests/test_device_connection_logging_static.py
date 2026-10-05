from pathlib import Path


SERVER_SOURCE = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(
    encoding="utf-8"
)


def test_device_connection_logging_reports_reachable_and_unreachable_without_sample_spam():
    assert 'env_int("DEVICE_CONNECTION_LOG_HEARTBEAT_SECONDS", 300)' in SERVER_SOURCE
    assert 'device_connection_logger.setLevel(logging.INFO)' in SERVER_SOURCE
    assert "def log_device_connection_status(" in SERVER_SOURCE
    assert 'status = "reachable" if bool(reachable) else "unreachable"' in SERVER_SOURCE
    assert '"Device connection status: device=%s status=%s telemetry=%s seconds_since_sync=%s"' in SERVER_SOURCE
    assert "if not changed and not heartbeat_due:" in SERVER_SOURCE


def test_ingestion_and_status_poll_update_connection_log():
    assert 'telemetry_status="live",\n        seconds_since_sync=0,' in SERVER_SOURCE
    assert 'current_system_status.get("device") == "online"' in SERVER_SOURCE
