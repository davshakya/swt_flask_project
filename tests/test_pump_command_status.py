from datetime import datetime

import pytest

from flask_app.pump_command_status import pump_confirmation


def command(**updates):
    return dict({"command": "ON", "status": "accepted", "queued_at": "2026-09-25 16:00:00"}, **updates)


@pytest.mark.parametrize("desired,actual", [("ON", "ON"), ("OFF", "OFF")])
def test_new_firmware_sample_confirms_both_directions(desired, actual):
    result = pump_confirmation(command(command=desired), {"motor": actual, "last_sync_at": "2026-09-25 16:00:02"})
    assert result["pump_confirmed"] is True
    assert result["pump_state"] == actual


@pytest.mark.parametrize("phase", ["queued", "rejected", "timed_out"])
def test_queued_or_failed_command_is_not_confirmed_by_matching_telemetry(phase):
    assert not pump_confirmation(command(status=phase), {"motor": "ON", "last_sync_at": "2026-09-25 16:00:02"})["pump_confirmed"]


def test_cached_snapshot_cannot_confirm_new_request():
    assert not pump_confirmation(command(), {"motor": "ON", "last_sync_at": "2026-09-25 15:59:59"})["pump_confirmed"]


def test_acceptance_without_motor_state_does_not_mean_running():
    assert not pump_confirmation(command(device_result={"status": "accepted"}), {})["pump_confirmed"]
    assert not pump_confirmation(command(device_result={"motor_state": "RELAY_ON"}), {})["pump_confirmed"]


def test_request_specific_physical_ack_does_not_depend_on_phone_clock():
    result = pump_confirmation(command(device_result={"motor_state": "RUNNING"}, accepted_at="2026-09-25 16:00:01"), {})
    assert result["pump_confirmed"] is True


def test_newer_stopped_sample_overrides_older_running_ack():
    result = pump_confirmation(command(device_result={"motor_state": "RUNNING"}, accepted_at="2026-09-25 16:00:01"),
                               {"motor": "OFF", "last_sync_at": "2026-09-25 16:00:02"})
    assert result["pump_state"] == "OFF"
    assert not result["pump_confirmed"]


def test_physical_feedback_takes_precedence_over_relay_or_motor_field():
    result = pump_confirmation(command(), {"motor": "ON", "physical_pump_running": False,
        "pump_state_confirmed": True, "last_sync_at": "2026-09-25 16:00:02"})
    assert result["pump_state"] == "OFF"
    assert not result["pump_confirmed"]


def test_timestamp_formats_can_be_mixed():
    result = pump_confirmation(command(queued_at=datetime(2026, 9, 25, 16)),
                               {"motor": "ON", "last_sync_at": "2026-09-25T16:00:02Z"})
    assert result["pump_confirmed"]


def test_mobile_endpoint_requires_auth_and_scopes_queries_to_assigned_device(monkeypatch):
    from flask_app import server

    client = server.app.test_client()
    monkeypatch.setattr(server, "resolve_mobile_user", lambda: None)
    assert client.get("/api/mobile/motor/command-status/request-1").status_code == 401
    monkeypatch.setattr(server, "resolve_mobile_user", lambda: {"role": "customer", "device_id": "swt-test-000-000-008"})
    calls = []
    monkeypatch.setattr(server, "device_motor_command_status", lambda device, request_id: calls.append((device, request_id)) or {"status": "accepted"})
    assert client.get("/api/mobile/motor/command-status/request-1?device_id=swt-test-000-000-009").status_code == 403
    assert not calls
    assert client.get("/api/mobile/motor/command-status/request-1").status_code == 200
    assert calls == [("swt-test-000-000-008", "request-1")]


def test_command_status_reads_firmware_ack_and_scopes_expiry_update(monkeypatch):
    import sqlite3
    from contextlib import contextmanager
    from flask_app import server

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE device_command_queue (
        target_device TEXT, request_id TEXT, command TEXT, status TEXT, created_at TEXT,
        delivered_at TEXT, accepted_at TEXT, completed_at TEXT, result_reason TEXT,
        result_json TEXT, expires_at TEXT)""")
    db.execute("""INSERT INTO device_command_queue VALUES
        ('device-a', 'same-request', 'ON', 'accepted', '2026-09-25 16:00:00',
         '2026-09-25 16:00:01', '2026-09-25 16:00:01', NULL, NULL,
         '{"motor_state":"RUNNING"}', '2999-01-01 00:00:00')""")
    db.execute("""INSERT INTO device_command_queue VALUES
        ('device-b', 'same-request', 'ON', 'queued', '2026-09-25 16:00:00',
         NULL, NULL, NULL, NULL, '{}', '2000-01-01 00:00:00')""")

    @contextmanager
    def database():
        yield db
        db.commit()

    monkeypatch.setattr(server, "get_db", database)
    monkeypatch.setattr(server, "load_dashboard_snapshot", lambda *args, **kwargs: {})
    with server.app.test_request_context():
        confirmed = server.device_motor_command_status("device-a", "same-request").get_json()
        assert confirmed["pump_confirmed"] is True
        assert confirmed["pump_state"] == "ON"
        expired = server.device_motor_command_status("device-b", "same-request").get_json()
        assert expired["status"] == "timed_out"
        assert not expired["pump_confirmed"]
    assert db.execute("SELECT status FROM device_command_queue WHERE target_device='device-a'").fetchone()[0] == "accepted"
    db.close()
