from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

import pytest

from flask_app.background_lease import background_lease
from flask_app.history_sampling import HISTORY_TRANSITION_FIELDS, legacy_history_sample_due


def test_background_cooldown_is_shared_and_transitions_bypass_it(tmp_path):
    with background_lease("device", "OFF", directory=tmp_path, clock=lambda: 100) as claim:
        assert claim == (True, 0)
        with background_lease("device", "OFF", directory=tmp_path, clock=lambda: 100) as busy:
            assert busy[0] is False
    with background_lease("device", "OFF", directory=tmp_path, clock=lambda: 110) as claim:
        assert claim == (False, 20)
    with background_lease("device", "ON", directory=tmp_path, clock=lambda: 110) as claim:
        assert claim == (True, 0)
    with background_lease("device", "ON", directory=tmp_path, clock=lambda: 140) as claim:
        assert claim == (True, 0)


def test_failed_background_work_can_be_retried(tmp_path):
    with pytest.raises(RuntimeError):
        with background_lease("device", "state", directory=tmp_path, clock=lambda: 100):
            raise RuntimeError("database unavailable")
    with background_lease("device", "state", directory=tmp_path, clock=lambda: 101) as claim:
        assert claim[0]


def test_background_lease_excludes_another_process(tmp_path):
    code = (
        "import json; from flask_app.background_lease import background_lease; "
        "lease=background_lease('device', 'state', directory=" + repr(str(tmp_path)) + "); "
        "print(json.dumps(lease.__enter__())); lease.__exit__(None, None, None)"
    )
    with background_lease("device", "state", directory=tmp_path):
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=Path(__file__).resolve().parents[1])
        assert json.loads(result.stdout)[0] is False


@pytest.mark.parametrize("field", HISTORY_TRANSITION_FIELDS)
def test_history_keeps_every_important_transition(field):
    previous = {"created_at": "2026-10-05 12:00:00", "level": 50, "lower_tank_level": 40, field: "OFF"}
    current = {**previous, field: "ON"}
    assert legacy_history_sample_due(previous, current, "2026-10-05 12:00:01")


def test_history_keeps_interval_level_change_and_first_report():
    previous = {"created_at": "2026-10-05 12:00:00", "level": 50, "lower_tank_level": 40}
    assert not legacy_history_sample_due(previous, {**previous, "level": 50.1}, "2026-10-05 12:00:05")
    assert legacy_history_sample_due(previous, previous, "2026-10-05 12:00:30")
    assert legacy_history_sample_due(previous, {**previous, "lower_tank_level": 39}, "2026-10-05 12:00:01")
    assert legacy_history_sample_due(None, previous, "2026-10-05 12:00:01")
    assert legacy_history_sample_due(previous, previous, "2026-10-05 12:00:01", interval=0)


def test_unchanged_configuration_does_not_write_or_issue_ddl(monkeypatch):
    from flask_app import server
    device = "swt-pressure-config-001"
    try:
        server.upsert_device_service_config(device, tank_capacity_liters=1000)
        calls = []
        original = server.MySqlCursorAdapter.execute

        def record(self, sql, params=None):
            calls.append(sql)
            return original(self, sql, params)

        monkeypatch.setattr(server.MySqlCursorAdapter, "execute", record)
        saved = server.upsert_device_service_config(device, tank_capacity_liters=1000)
        assert saved["tank_capacity_liters"] == 1000
        assert not any("INSERT INTO device_service_configs" in sql for sql in calls)
        assert not any("CREATE TABLE" in sql for sql in calls)
        calls.clear()
        server.upsert_device_service_config(device, tank_capacity_liters=1200)
        assert any("INSERT INTO device_service_configs" in sql for sql in calls)
        assert server.fetch_device_service_config(device)["tank_capacity_liters"] == 1200
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device,))


def test_sampled_history_preserves_live_state_and_pump_transitions(monkeypatch):
    from flask_app import server
    from flask_app.capacity_features import CapacityFeatureRegistry
    from flask_app.capacity_schema import ensure_capacity_schema

    device = "swt-pressure-live-001"
    registry = CapacityFeatureRegistry(environ={"FEATURE_CAPACITY_SCHEMA": "true", "FEATURE_LATEST_STATE_WRITES": "true"})
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry)
    monkeypatch.setattr(server, "postprocess_telemetry_payload", lambda *args: None)
    with server.get_db() as db:
        ensure_capacity_schema(db.cursor())
    payload = {"device_id": device, "device_source": "virtual", "level": 50, "motor": "OFF", "mode": "AUTO", "sensor": "OK"}
    instant = datetime(2026, 10, 5, 12)
    try:
        monkeypatch.setattr(server, "now_utc", lambda: instant)
        server.process_telemetry_payload(payload, transport="pressure-first")
        monkeypatch.setattr(server, "now_utc", lambda: instant + timedelta(seconds=5))
        server.process_telemetry_payload({**payload, "level": 50.1}, transport="pressure-second")
        with server.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM tank_data WHERE device_id = ?", (device,)).fetchone()["n"]
            latest = db.execute("SELECT state_json FROM device_latest_state WHERE device_id = ?", (device,)).fetchone()
        assert count == 1
        assert json.loads(latest["state_json"])["level"] == 50.1
        monkeypatch.setattr(server, "now_utc", lambda: instant + timedelta(seconds=6))
        server.process_telemetry_payload({**payload, "motor": "ON", "level": 50.1}, transport="pressure-transition")
        with server.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS n FROM tank_data WHERE device_id = ?", (device,)).fetchone()["n"]
        assert count == 2
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device,))
            db.execute("DELETE FROM device_latest_state WHERE device_id = ?", (device,))


def test_fresh_persisted_analytics_is_reused_across_workers_and_expires(monkeypatch):
    from flask_app import server
    start = datetime(2026, 10, 5)
    key = server.build_analytics_cache_key(start, start + timedelta(days=1), "pressure-cache")
    payload = server.build_empty_analytics(start, start + timedelta(days=1), "Today", "pressure-cache")
    payload["levels"] = {"time": ["2026-10-05 12:00:00"], "values": [50]}
    payload["analysis"] = {"quality": {"row_count": 2}}
    record = {"schema": server.ANALYTICS_LAST_VALID_SCHEMA_VERSION,
              "cache_key": [str(part) for part in key],
              "saved_at": "2026-10-05 12:00:00", "payload": payload}
    monkeypatch.setattr(server, "ANALYTICS_CACHE_TTL_SECONDS", 300)
    monkeypatch.setattr(server, "get_app_setting", lambda *args: json.dumps(record))
    monkeypatch.setattr(server, "now_utc", lambda: datetime(2026, 10, 5, 12, 1))
    try:
        assert server.read_recent_persisted_analytics(key)["levels"]["values"] == [50]
        monkeypatch.setattr(server, "now_utc", lambda: datetime(2026, 10, 5, 12, 6))
        assert server.read_recent_persisted_analytics(key) is None
        record["cache_key"][-1] = "another-version"
        assert server.read_recent_persisted_analytics(key) is None
    finally:
        server.analytics_cache.pop(key, None)


def test_summary_failure_uses_three_attempts_without_nested_retries(monkeypatch):
    from contextlib import contextmanager
    from flask_app import server
    calls = []

    class Database:
        def execute(self, *args):
            calls.append(args)
            raise server.pymysql.err.OperationalError(2006, "MySQL server has gone away")

    @contextmanager
    def database():
        yield Database()

    monkeypatch.setattr(server, "get_db", database)
    monkeypatch.setattr(server, "get_device_source_mode", lambda: "real")
    monkeypatch.setattr(server, "build_dashboard_summary_payload", lambda device: {"snapshot": {}})
    monkeypatch.setattr(server.time, "sleep", lambda *args: None)
    assert server.refresh_dashboard_summary("pressure-summary") is None
    assert len(calls) == 3


def test_unavailable_lease_directory_does_not_disable_background_work(tmp_path):
    directory = tmp_path / "missing"
    with background_lease("fallback-device", "OFF", directory=directory, clock=lambda: 100) as claim:
        assert claim[0]
    with background_lease("fallback-device", "OFF", directory=directory, clock=lambda: 110) as claim:
        assert claim == (False, 20)
    with background_lease("fallback-device", "FAULT", directory=directory, clock=lambda: 110) as claim:
        assert claim[0]
