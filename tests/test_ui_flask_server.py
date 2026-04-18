from datetime import datetime, timedelta, timezone
import base64
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as server_module
from server import (
    DEVICE,
    LOGIN_USERNAME,
    app,
    drain_relay_queue,
    get_db,
    queue_command,
    set_dashboard_password,
    upsert_customer_account,
)
from tests.ui_test_support import (
    BASE_URL,
    TEST_CSRF_TOKEN,
    build_status_payload,
    client,
    csrf_headers,
    default_test_device_id,
    device_headers,
    ensure_device_snapshot,
    ensure_known_admin_password,
    ensure_logged_in,
    fetch_csrf_token,
    get_json,
    get_response,
    make_admin_client,
    make_customer_client,
    mobile_auth_headers,
)


class DummyForecastModel:
    def __init__(self, delta_percent=-4.5):
        self.delta_percent = float(delta_percent)

    def predict(self, frame):
        base_level = float(frame.iloc[0]["level"])
        return [base_level + self.delta_percent]


def make_fake_forecast_loader(delta_percent=-4.5):
    artifact = {
        "model": DummyForecastModel(delta_percent=delta_percent),
        "feature_columns": [
            "level",
            "tank_health",
            "ai_usage_rate",
            "lower_tank_level",
            "wifi_rssi",
            "tank_capacity_liters",
            "motor_on",
            "mode_auto",
            "alert_flag",
            "simulator_flag",
            "level_delta_1",
            "level_delta_4",
            "usage_roll_mean_4",
            "usage_roll_mean_24",
            "level_roll_mean_4",
            "level_roll_mean_24",
            "pump_on_roll_mean_4",
            "pump_on_roll_mean_24",
            "level_lag_1",
            "level_lag_2",
            "level_lag_4",
            "level_lag_8",
            "level_lag_24",
            "tank_health_lag_1",
            "tank_health_lag_4",
            "tank_health_lag_24",
            "usage_lag_1",
            "usage_lag_4",
            "usage_lag_24",
            "motor_on_lag_1",
            "motor_on_lag_4",
            "motor_on_lag_24",
            "hour_sin",
            "hour_cos",
            "dow_sin",
            "dow_cos",
        ],
        "metrics": {
            "mae_level_percent": 2.75,
            "rmse_level_percent": 3.42,
            "r2": 0.71,
        },
        "metadata": {
            "model_family": "HistGradientBoostingRegressor",
            "target": "future tank level percent",
            "horizon_hours": 1,
            "resample_minutes": 15,
        },
        "train_rows": 120,
        "test_rows": 31,
    }
    loaded_at = datetime(2026, 4, 1, 9, 0, 0)
    artifact_path = Path("artifacts/level_forecast_model.pkl")

    def loader(force_reload=False):
        return artifact, artifact_path, loaded_at

    return loader


def seed_forecast_history(device_id, rows=36, step_minutes=15, base_level=78.0, capacity_liters=1000.0):
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=step_minutes * rows)
    with get_db() as db:
        for index in range(rows):
            created_at = (start + timedelta(minutes=step_minutes * index)).strftime(server_module.TIMESTAMP_FORMAT)
            level = round(base_level - (index * 0.6), 2)
            lower_tank_level = round(max(0.0, level - 26.0), 2)
            ai_usage_rate = round(0.45 + ((index % 5) * 0.08), 2)
            tank_health = round(92.0 - ((index % 4) * 0.6), 2)
            motor = "ON" if index % 8 in {0, 1} else "OFF"
            db.execute(
                """
                INSERT INTO tank_data(
                    level,
                    motor,
                    mode,
                    ai_usage_rate,
                    lower_tank_level,
                    wifi_rssi,
                    tank_capacity_liters,
                    tank_health,
                    pipe_leak,
                    slow_leak,
                    drip,
                    abnormal,
                    dry_run,
                    simulator,
                    device_id,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    level,
                    motor,
                    "AUTO",
                    ai_usage_rate,
                    lower_tank_level,
                    -55 + (index % 3),
                    capacity_liters,
                    tank_health,
                    "NO",
                    "NO",
                    "NO",
                    "NO",
                    "NO",
                    "OFF",
                    device_id,
                    created_at,
                ),
            )
    server_module.clear_runtime_caches(device_id)


def test_load_dashboard_snapshot_uses_short_cache(monkeypatch):
    device_id = default_test_device_id()
    server_module.dashboard_snapshot_cache.clear()
    calls = {"count": 0}

    def fake_fetch(target_device_id):
        calls["count"] += 1
        return {"device_id": target_device_id, "level": 51.0, "telemetry_status": "live"}

    monkeypatch.setitem(server_module.load_dashboard_snapshot.__globals__, "fetch_device_snapshot", fake_fetch)

    first = server_module.load_dashboard_snapshot(device_id)
    second = server_module.load_dashboard_snapshot(device_id)

    assert first["device_id"] == device_id
    assert second["device_id"] == device_id
    assert calls["count"] == 1


def test_device_status_clears_snapshot_cache_after_new_telemetry():
    device_id = default_test_device_id()
    active_mode = server_module.get_device_source_mode()
    server_module.dashboard_snapshot_cache.clear()
    server_module.dashboard_snapshot_cache[f"{active_mode}:{device_id}"] = {
        "created_at": time.time(),
        "payload": {"device_id": device_id, "level": 10.0},
    }
    server_module.dashboard_snapshot_cache[f"{active_mode}:__latest__"] = {
        "created_at": time.time(),
        "payload": {"device_id": device_id, "level": 10.0},
    }

    payload = build_status_payload(device_id=device_id, level=52.5)
    response = client.post("/status", json=payload, headers=device_headers(device_id))

    assert response.status_code == 200
    assert f"{active_mode}:{device_id}" not in server_module.dashboard_snapshot_cache
    assert f"{active_mode}:__latest__" not in server_module.dashboard_snapshot_cache


def test_public_home_page_loads_marketing_landing():
    fresh_client = app.test_client()
    response = fresh_client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Request Pricing" in body
    assert "Talk To Sales" in body
    assert "Customer Login" in body
    assert "support@salewell.co.in" in body
    assert "smart-water-tank-hero-ai.png" in body
    assert "smart-water-tank-controls-ai.png" in body
    assert "smart-water-tank-lifestyle-ai.png" in body
    assert "smart-water-tank-service-ai.png" in body
    assert 'mailto:support@salewell.co.in' in body
    assert "Need the other login?" not in body


def test_customer_dashboard_requires_login_redirect():
    fresh_client = app.test_client()
    response = fresh_client.get("/customer/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/login/customer" in response.headers["Location"]


def test_customer_login_page_loads():
    fresh_client = app.test_client()
    response = fresh_client.get("/login/customer")
    assert response.status_code == 200
    assert b"Customer Login" in response.data
    assert b"Admin Login" in response.data
    assert b"Request Pricing" in response.data
    assert b"support@salewell.co.in" in response.data
    assert b"device_id" in response.data
    assert b"Need the other login?" not in response.data


def test_admin_login_page_loads():
    fresh_client = app.test_client()
    response = fresh_client.get("/login/admin")
    assert response.status_code == 200
    assert b"Admin Login" in response.data
    assert b"Customer Login" in response.data


def test_login_page_exposes_pwa_install_assets():
    fresh_client = app.test_client()
    response = fresh_client.get("/login/customer")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert '/manifest.webmanifest' in body
    assert '/service-worker.js' in body
    assert 'Install App' in body


def test_sales_enquiry_logs_public_lead():
    fresh_client = app.test_client()
    unique_name = "Marketing Lead Test"

    with get_db() as db:
        db.execute(
            "DELETE FROM ops_audit_log WHERE actor = ? AND action = ? AND details LIKE ?",
            ("public-lead", "sales_enquiry_submitted", f"%{unique_name}%"),
        )

    response = fresh_client.get("/")
    assert response.status_code == 200
    with fresh_client.session_transaction() as session_data:
        csrf_token = session_data["csrf_token"]

    response = fresh_client.post(
        "/sales/enquiry",
        data={
            "csrf_token": csrf_token,
            "landing_mode": "customer",
            "name": unique_name,
            "phone": "+91 9876543210",
            "city": "Pune",
            "segment": "Apartment / Hostel",
            "device_count": "6",
            "message": "Need a quote for a hostel deployment.",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Thanks for your enquiry." in body

    with get_db() as db:
        row = db.execute(
            """
            SELECT actor, action, target_type, details
            FROM ops_audit_log
            WHERE actor = ? AND action = ? AND details LIKE ?
            ORDER BY id DESC
            LIMIT 1
            """,
            ("public-lead", "sales_enquiry_submitted", f"%{unique_name}%"),
        ).fetchone()

    assert row is not None
    assert row["target_type"] == "sales_enquiry"
    details = json.loads(row["details"])
    assert details["name"] == unique_name
    assert details["phone"] == "+91 9876543210"
    assert details["city"] == "Pune"
    assert details["segment"] == "Apartment / Hostel"
    assert details["device_count"] == "6"


def test_pwa_routes_are_available():
    fresh_client = app.test_client()

    manifest = fresh_client.get('/manifest.webmanifest')
    assert manifest.status_code == 200
    assert 'application/manifest+json' in manifest.headers.get('Content-Type', '')
    manifest_body = manifest.get_data(as_text=True)
    assert 'Smart Water Tank' in manifest_body
    assert '/static/pwa/icon-192.png' in manifest_body

    worker = fresh_client.get('/service-worker.js')
    assert worker.status_code == 200
    assert 'application/javascript' in worker.headers.get('Content-Type', '')
    assert worker.headers.get('Service-Worker-Allowed') == '/'
    assert 'CACHE_NAME' in worker.get_data(as_text=True)


def test_login_rejects_external_redirect_target():
    fresh_client = app.test_client()
    response = fresh_client.post(
        "/login/admin?next=https://example.com",
        data={"username": LOGIN_USERNAME, "password": ensure_known_admin_password()},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/admin/customers")


def test_device_status_rejects_missing_credentials():
    response = client.post("/status", json={"level": 50})
    assert response.status_code == 401


def test_device_status_rejects_invalid_credentials():
    response = client.post(
        "/status",
        json={"level": 50, "device_id": "swt-node-01"},
        headers=device_headers(device_key="wrong-key"),
    )
    assert response.status_code == 403


def test_device_status_rejects_mismatched_header_and_payload_device_ids():
    response = client.post(
        "/status",
        json={"level": 50, "device_id": "swt-node-other"},
        headers=device_headers(device_id="swt-node-01"),
    )
    assert response.status_code == 403
    assert response.get_json()["error"] == "device_id does not match X-Device-Id header"


def test_device_status_accepts_wildcard_registered_device_id(monkeypatch):
    if BASE_URL:
        pytest.skip("Wildcard device registration test is skipped against shared BASE_URL deployments.")

    device_id = "swt-node-chip123"
    device_key = "WildcardKey2026!"
    with get_db() as db:
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (device_id,))

    monkeypatch.setattr(server_module, "DEVICE_KEY_MAP", {})
    monkeypatch.setattr(
        server_module,
        "DEVICE_KEY_WILDCARD_RULES",
        [{"pattern": "swt-node-*", "prefix": "swt-node-", "key": device_key}],
    )

    payload = build_status_payload(
        level=54.0,
        ai_usage_rate=0.4,
        tomorrow_prediction=11.0,
        wifi_rssi=-50,
        sensor_distance_cm=59.0,
        tank_health=92.0,
        free_heap=30500,
        uptime_s=111,
        reset_reason="Power on",
    )
    response = client.post(
        "/status",
        json=payload,
        headers={"X-Device-Id": device_id, "X-Device-Key": device_key},
    )

    assert response.status_code == 200
    with get_db() as db:
        row = db.execute(
            "SELECT device_id, registration_source, key_rule FROM registered_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    assert row is not None
    assert row["device_id"] == device_id
    assert row["registration_source"] == "device_keys_wildcard"
    assert row["key_rule"] == "swt-node-*"
    assert server_module.relay_headers_for_device(device_id)["X-Device-Key"] == device_key


def test_device_command_accepts_virtual_device_env_auth(tmp_path, monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual-device auth test is skipped against shared BASE_URL deployments.")

    device_id = "swt-virtual-auth-011"
    device_key = "VirtualKey2026!"
    virtual_devices_dir = tmp_path / "tests" / "virtual_devices"
    virtual_devices_dir.mkdir(parents=True)
    env_path = virtual_devices_dir / "device-011.env"
    env_path.write_text(
        f"SWT_VIRTUAL_DEVICE_ID={device_id}\n"
        f"SWT_VIRTUAL_DEVICE_KEY={device_key}\n",
        encoding="utf-8",
    )

    with get_db() as db:
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (device_id,))

    original_entries = {
        configured_id: dict(entry)
        for configured_id, entry in server_module.LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES.items()
    }
    original_map = dict(server_module.LOCAL_VIRTUAL_DEVICE_AUTH_MAP)
    original_key_rules = dict(server_module.LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES)

    monkeypatch.setattr(server_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server_module, "SEED_VIRTUAL_DEVICE_ENVS", True)

    try:
        assert server_module.LOCAL_VIRTUAL_DEVICE_AUTH_MAP.get(device_id) is None
        assert device_id not in server_module.DEVICE_KEY_MAP

        response = client.get(
            "/device/command",
            headers={
                "X-Device-Id": device_id,
                "X-Device-Key": device_key,
                "X-Device-Source": "virtual",
            },
        )

        assert response.status_code == 200
        assert response.get_json()["command"] is None
        assert server_module.LOCAL_VIRTUAL_DEVICE_AUTH_MAP.get(device_id) == device_key
        matched_rule = server_module.find_matching_device_key_rule(device_id)
        assert matched_rule is not None
        assert matched_rule["kind"] == "virtual_env"
        assert matched_rule["pattern"] == str(Path("tests/virtual_devices/device-011.env"))

        with get_db() as db:
            row = db.execute(
                "SELECT registration_source, key_rule FROM registered_devices WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        assert row is not None
        assert row["registration_source"] == "device_keys_virtual_env"
        assert row["key_rule"] == str(Path("tests/virtual_devices/device-011.env"))
    finally:
        server_module.LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES = original_entries
        server_module.LOCAL_VIRTUAL_DEVICE_AUTH_MAP = original_map
        server_module.LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES = original_key_rules


def test_remember_registered_device_throttles_repeated_updates(monkeypatch):
    class RecordingDb:
        def __init__(self):
            self.execute_calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params):
            self.execute_calls.append((query, params))
            return self

    device_id = "swt-node-touch-test"
    recorder = RecordingDb()
    monkeypatch.setattr(server_module, "REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "device_is_ignored", lambda device_id, ignored_device_ids=None: False)
    monkeypatch.setattr(server_module, "get_db", lambda: recorder)
    server_module.forget_registered_device_touch(device_id)

    server_module.remember_registered_device(device_id, "device_keys_exact", key_rule=device_id)
    server_module.remember_registered_device(device_id, "device_keys_exact", key_rule=device_id)
    server_module.remember_registered_device(device_id, "device_keys_wildcard", key_rule="swt-node-*")

    assert len(recorder.execute_calls) == 2
    assert recorder.execute_calls[0][1] == (device_id, "device_keys_exact", device_id)
    assert recorder.execute_calls[1][1] == (device_id, "device_keys_wildcard", "swt-node-*")
    server_module.forget_registered_device_touch(device_id)


def test_remember_registered_device_best_effort_ignores_locked_database(monkeypatch):
    class LockedDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params):
            raise sqlite3.OperationalError("database is locked")

    device_id = "swt-node-lock-test"
    monkeypatch.setattr(server_module, "REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "device_is_ignored", lambda device_id, ignored_device_ids=None: False)
    monkeypatch.setattr(server_module, "get_db", lambda: LockedDb())
    server_module.forget_registered_device_touch(device_id)

    server_module.remember_registered_device(
        device_id,
        "device_keys_exact",
        key_rule=device_id,
        best_effort=True,
    )

    cached = server_module.registered_device_touch_cache.get(device_id)
    assert cached is not None
    assert cached["registration_source"] == "device_keys_exact"
    assert cached["key_rule"] == device_id
    server_module.forget_registered_device_touch(device_id)


def test_set_alert_throttles_repeated_identical_updates(monkeypatch):
    class DummyCursor:
        def __init__(self, row=None, lastrowid=None):
            self._row = row
            self.lastrowid = lastrowid

        def fetchone(self):
            return self._row

    class RecordingDb:
        def __init__(self):
            self.execute_calls = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, query, params):
            self.execute_calls.append((query, params))
            compact_query = " ".join(str(query).split())
            if "SELECT id, active, message, severity FROM ops_alerts" in compact_query:
                return DummyCursor(row=None)
            if "INSERT INTO ops_alerts" in compact_query:
                return DummyCursor(lastrowid=101)
            return DummyCursor()

    device_id = "swt-node-alert-touch"
    recorder = RecordingDb()
    monkeypatch.setattr(server_module, "ALERT_TOUCH_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "get_db", lambda: recorder)
    monkeypatch.setattr(server_module, "send_alert_webhook", lambda payload: None)
    server_module.forget_alert_touches_for_device(device_id)

    server_module.set_alert(
        "telemetry_stale",
        "danger",
        "Telemetry is stale for device swt-node-alert-touch.",
        device_id=device_id,
        active=True,
    )
    server_module.set_alert(
        "telemetry_stale",
        "danger",
        "Telemetry is stale for device swt-node-alert-touch.",
        device_id=device_id,
        active=True,
    )

    assert len(recorder.execute_calls) == 2
    assert recorder.execute_calls[0][1] == ("telemetry_stale", device_id)
    assert recorder.execute_calls[1][1] == (device_id, "telemetry_stale", "danger", "Telemetry is stale for device swt-node-alert-touch.")
    server_module.forget_alert_touches_for_device(device_id)


def test_evaluate_snapshot_alerts_best_effort_ignores_locked_database(monkeypatch):
    class LockedDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, _query, _params):
            raise sqlite3.OperationalError("database is locked")

    device_id = "swt-node-alert-lock"
    monkeypatch.setattr(server_module, "ALERT_TOUCH_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "get_db", lambda: LockedDb())
    monkeypatch.setattr(server_module, "send_alert_webhook", lambda payload: None)
    server_module.forget_alert_touches_for_device(device_id)

    server_module.evaluate_snapshot_alerts(
        {
            "device_id": device_id,
            "telemetry_status": "stale",
            "pump_failure": "NO",
            "dry_run": "NO",
            "leak": "NO",
            "drip": "NO",
            "slow_leak": "NO",
            "pipe_leak": "NO",
            "sensor": "OK",
        }
    )

    cached = server_module.alert_touch_cache.get(("telemetry_stale", device_id))
    assert cached is not None
    assert cached["active"] is True
    assert cached["severity"] == "danger"
    server_module.forget_alert_touches_for_device(device_id)


def test_maybe_prune_retained_rows_throttles_repeated_runs(monkeypatch):
    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return object()

    prune_calls = []
    maintenance_calls = []
    original_state = dict(server_module.db_prune_state)
    monkeypatch.setattr(server_module, "DB_PRUNE_MIN_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "get_db", lambda: DummyDb())
    monkeypatch.setattr(
        server_module,
        "prune_retained_rows",
        lambda cursor, device_id=None, latest_row_id=None: (
            prune_calls.append((device_id, latest_row_id)) or {"tank_data_retention": 1}
        ),
    )
    monkeypatch.setattr(
        server_module,
        "maybe_maintain_database",
        lambda reason="periodic", pruned_rows=0, force=False: maintenance_calls.append((reason, pruned_rows, force)),
    )
    server_module.db_prune_state.update({"last_run_at": 0.0, "last_pruned_rows": 0, "last_error": None})

    first = server_module.maybe_prune_retained_rows(device_id="swt-node-01", latest_row_id=101)
    second = server_module.maybe_prune_retained_rows(device_id="swt-node-01", latest_row_id=102)

    assert first == {"tank_data_retention": 1}
    assert second == {}
    assert prune_calls == [("swt-node-01", 101)]
    assert maintenance_calls == [("telemetry-retention", 1, False)]
    server_module.db_prune_state.update(original_state)


def test_maybe_prune_retained_rows_skips_noop_maintenance_when_temp_size_guard_enabled(monkeypatch):
    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return object()

    maintenance_calls = []
    original_prune_state = dict(server_module.db_prune_state)
    original_maintenance_state = dict(server_module.db_maintenance_state)
    monkeypatch.setattr(server_module, "TEMP_DB_SIZE_GUARD_ENABLED", True)
    monkeypatch.setattr(server_module, "DB_TARGET_SIZE_BYTES", 1024)
    monkeypatch.setattr(server_module, "DB_PRUNE_MIN_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "get_db", lambda: DummyDb())
    monkeypatch.setattr(
        server_module,
        "prune_retained_rows",
        lambda cursor, device_id=None, latest_row_id=None: {"tank_data_retention": 0},
    )
    monkeypatch.setattr(
        server_module,
        "collect_database_file_sizes",
        lambda db_path=None: {"main_bytes": 128, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 128},
    )
    monkeypatch.setattr(
        server_module,
        "maybe_maintain_database",
        lambda reason="periodic", pruned_rows=0, force=False: maintenance_calls.append((reason, pruned_rows, force)),
    )
    server_module.db_prune_state.update({"last_run_at": 0.0, "last_pruned_rows": 0, "last_error": None})
    server_module.db_maintenance_state.update({"last_skip_at": 0.0, "last_skip_reason": None})

    result = server_module.maybe_prune_retained_rows(device_id="swt-node-01", latest_row_id=101)

    assert result == {"tank_data_retention": 0}
    assert maintenance_calls == []
    assert server_module.db_maintenance_state["last_skip_reason"] == "retention-noop-under-target-size"
    assert server_module.db_maintenance_state["last_skip_at"] > 0
    server_module.db_prune_state.update(original_prune_state)
    server_module.db_maintenance_state.update(original_maintenance_state)


def test_maybe_prune_telemetry_size_cap_drops_oldest_rows_and_preserves_latest_snapshot(monkeypatch):
    if BASE_URL:
        pytest.skip("Telemetry size-cap DB mutation test is skipped against shared BASE_URL deployments.")

    primary_device_id = f"{default_test_device_id()}-sizecap-primary"
    secondary_device_id = f"{default_test_device_id()}-sizecap-secondary"
    maintenance_calls = []
    temp_db_path = server_module.PROJECT_ROOT / "data" / f"size-cap-test-{time.time_ns()}.db"

    def open_temp_db():
        conn = sqlite3.connect(temp_db_path, timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    try:
        with open_temp_db() as db:
            db.execute(
                """
                CREATE TABLE tank_data(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level REAL,
                    motor TEXT,
                    mode TEXT,
                    device_source TEXT,
                    device_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for created_at, device_id, level in (
                ("2026-01-01 00:00:01", primary_device_id, 10.0),
                ("2026-01-01 00:00:02", primary_device_id, 20.0),
                ("2026-01-01 00:00:03", primary_device_id, 30.0),
                ("2026-01-01 00:00:04", secondary_device_id, 40.0),
                ("2026-01-01 00:00:05", secondary_device_id, 50.0),
            ):
                db.execute(
                    """
                    INSERT INTO tank_data(level, motor, mode, device_source, device_id, created_at)
                    VALUES (?, 'OFF', 'AUTO', ?, ?, ?)
                    """,
                    (level, server_module.DEVICE_SOURCE_REAL, device_id, created_at),
                )

        size_samples = iter(
            [
                {"main_bytes": 8192, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 8192},
                {"main_bytes": 256, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 256},
            ]
        )
        monkeypatch.setattr(server_module, "get_db", open_temp_db)
        monkeypatch.setattr(server_module, "TEMP_HARD_DB_CAP_ENABLED", True)
        monkeypatch.setattr(server_module, "DB_TARGET_SIZE_BYTES", 1024)
        monkeypatch.setattr(server_module, "TEMP_HARD_DB_CAP_BATCH_ROWS", 10)
        monkeypatch.setattr(server_module, "TEMP_HARD_DB_CAP_MAX_BATCHES", 2)
        monkeypatch.setattr(
            server_module,
            "collect_database_file_sizes",
            lambda db_path=None: next(size_samples, {"main_bytes": 256, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 256}),
        )
        monkeypatch.setattr(
            server_module,
            "maybe_maintain_database",
            lambda reason="periodic", pruned_rows=0, force=False: maintenance_calls.append((reason, pruned_rows, force)) or True,
        )

        result = server_module.maybe_prune_telemetry_size_cap()

        with open_temp_db() as db:
            remaining_rows = db.execute(
                """
                SELECT device_id, level, created_at
                FROM tank_data
                WHERE device_id IN (?, ?)
                ORDER BY device_id ASC, created_at ASC, id ASC
                """,
                (primary_device_id, secondary_device_id),
            ).fetchall()

        assert result == {"rows": 3, "batches": 1, "remaining_pressure": False}
        assert maintenance_calls == [("telemetry-size-cap", 3, True)]
        assert [(row["device_id"], row["level"], row["created_at"]) for row in remaining_rows] == [
            (primary_device_id, 30.0, "2026-01-01 00:00:03"),
            (secondary_device_id, 50.0, "2026-01-01 00:00:05"),
        ]
    finally:
        for suffix in ("", "-shm", "-wal"):
            candidate = Path(f"{temp_db_path}{suffix}")
            if candidate.exists():
                try:
                    candidate.unlink()
                except OSError:
                    pass


def test_maybe_prune_retained_rows_skips_extra_maintenance_after_size_cap(monkeypatch):
    class DummyDb:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return object()

    maintenance_calls = []
    original_prune_state = dict(server_module.db_prune_state)
    monkeypatch.setattr(server_module, "DB_PRUNE_MIN_INTERVAL_SECONDS", 60)
    monkeypatch.setattr(server_module, "get_db", lambda: DummyDb())
    monkeypatch.setattr(
        server_module,
        "prune_retained_rows",
        lambda cursor, device_id=None, latest_row_id=None: {"tank_data_retention": 0},
    )
    monkeypatch.setattr(
        server_module,
        "maybe_prune_telemetry_size_cap",
        lambda force=False: {"rows": 5, "batches": 1, "remaining_pressure": False},
    )
    monkeypatch.setattr(
        server_module,
        "maybe_maintain_database",
        lambda reason="periodic", pruned_rows=0, force=False: maintenance_calls.append((reason, pruned_rows, force)),
    )
    server_module.db_prune_state.update({"last_run_at": 0.0, "last_pruned_rows": 0, "last_error": None})

    result = server_module.maybe_prune_retained_rows(device_id="swt-node-01", latest_row_id=101)

    assert result == {"tank_data_retention": 0, "tank_data_size_cap": 5}
    assert maintenance_calls == []
    assert server_module.db_prune_state["last_size_cap_rows"] == 5
    assert server_module.db_prune_state["last_size_cap_batches"] == 1
    server_module.db_prune_state.update(original_prune_state)


def test_prune_tank_data_retention_keeps_only_last_seven_days_per_device():
    device_ids = (
        "swt-retention-old-device-001",
        "swt-retention-old-device-002",
        "swt-retention-fresh-device-001",
        "swt-retention-fresh-device-002",
    )
    with get_db() as db:
        db.execute(
            "DELETE FROM tank_data WHERE device_id IN (?,?,?,?)",
            device_ids,
        )
        db.execute(
            "INSERT INTO tank_data(device_id, created_at) VALUES (?, datetime('now', '-8 day'))",
            (device_ids[0],),
        )
        db.execute(
            "INSERT INTO tank_data(device_id, created_at) VALUES (?, datetime('now', '-9 day'))",
            (device_ids[1],),
        )
        db.execute(
            "INSERT INTO tank_data(device_id, created_at) VALUES (?, datetime('now', '-6 day'))",
            (device_ids[2],),
        )
        db.execute(
            "INSERT INTO tank_data(device_id, created_at) VALUES (?, datetime('now', '-1 day'))",
            (device_ids[3],),
        )

        pruned = server_module.prune_retained_rows(db.cursor())
        remaining = db.execute(
            """
            SELECT device_id
            FROM tank_data
            WHERE device_id IN (?,?,?,?)
            ORDER BY device_id
            """,
            device_ids,
        ).fetchall()

    assert pruned["tank_data_retention"] == 2
    assert [row["device_id"] for row in remaining] == sorted(device_ids[2:])


def test_device_status_accepts_valid_credentials_and_persists_device_metadata():
    device_id = default_test_device_id()
    payload = build_status_payload(
        device_id=device_id,
        level=51.2,
        ai_usage_rate=0.7,
        tomorrow_prediction=12.3,
        wifi_rssi=-48,
        sensor_distance_cm=58.2,
        tank_health=91.0,
        free_heap=31000,
        uptime_s=123,
        channel_mode="both",
        telemetry_service="ON",
        command_service="ON",
        ota_service="ON",
        lower_tank_service="OFF",
    )
    response = client.post("/status", json=payload, headers=device_headers(device_id))
    assert response.status_code == 200

    with get_db() as db:
        data = db.execute(
            """
            SELECT device_id, firmware_version, reset_reason, channel_mode,
                   telemetry_service, command_service, ota_service,
                   lower_tank_service, device_local_url
            FROM tank_data
            WHERE device_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
    assert data["device_id"] == device_id
    assert data["firmware_version"] == "1.1.0"
    assert data["reset_reason"] == "Software/System restart"
    assert data["channel_mode"] == "both"
    assert data["telemetry_service"] == "ON"
    assert data["command_service"] == "ON"
    assert data["ota_service"] == "ON"
    assert data["lower_tank_service"] == "OFF"
    assert data["device_local_url"] == "http://192.168.1.50"


def test_dashboard_bootstrap_preserves_device_local_url_for_authenticated_dashboard():
    ensure_device_snapshot()
    session_client = ensure_logged_in()
    response = session_client.get("/dashboard/bootstrap")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["snapshot"]["device_local_url"] == "http://192.168.1.50"


def test_mobile_last_redacts_private_network_fields():
    ensure_device_snapshot()
    response = client.get("/api/mobile/last", headers=mobile_auth_headers())

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["source_ip"] is None
    assert payload["device_local_url"] is None


def test_device_history_is_retained_across_firmware_updates_for_same_device():
    device_id = default_test_device_id()
    first_payload = build_status_payload(
        device_id=device_id,
        level=42.5,
        tomorrow_prediction=9.0,
        wifi_rssi=-50,
        sensor_distance_cm=60.0,
        tank_health=92.0,
        uptime_s=100,
    )
    second_payload = dict(first_payload)
    second_payload.update(
        {
            "level": 43.0,
            "uptime_s": 200,
            "firmware_version": "1.2.0",
            "reset_reason": "OTA update",
        }
    )

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    first_response = client.post("/status", json=first_payload, headers=device_headers(device_id))
    second_response = client.post("/status", json=second_payload, headers=device_headers(device_id))

    assert first_response.status_code == 200
    assert second_response.status_code == 200

    with get_db() as db:
        rows = db.execute(
            """
            SELECT firmware_version, level
            FROM tank_data
            WHERE device_id = ?
            ORDER BY id ASC
            """,
            (device_id,),
        ).fetchall()

    assert len(rows) == 2
    assert [row["firmware_version"] for row in rows] == ["1.1.0", "1.2.0"]
    assert [row["level"] for row in rows] == [42.5, 43.0]


def test_device_command_requires_valid_credentials():
    response = client.get(
        "/device/command",
        headers={"X-Device-Source": server_module.get_device_source_mode()},
    )
    assert response.status_code == 401

    queue_command("AUTO", target_device=default_test_device_id())
    good = client.get("/device/command", headers=device_headers())
    assert good.status_code == 200
    assert good.get_json()["command"] == "AUTO"
    assert good.get_json()["command_source"] == "queue"
    assert good.get_json()["command_id"] is not None


def test_virtual_device_command_skips_cloud_relay(monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual device command test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    original_mode = server_module.get_device_source_mode()
    with get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    def fail_requests_get(*args, **kwargs):
        raise AssertionError("virtual devices should not poll cloud relay commands")

    monkeypatch.setattr(server_module, "RELAY_COMMAND_URL_LIST", ["https://example.invalid/device/command"])
    monkeypatch.setattr(server_module.requests, "get", fail_requests_get)

    try:
        server_module.set_device_source_mode(server_module.DEVICE_SOURCE_VIRTUAL)
        response = client.get(
            "/device/command",
            headers={**device_headers(device_id), "X-Device-Source": "virtual"},
        )
    finally:
        server_module.set_device_source_mode(original_mode)

    assert response.status_code == 200
    assert response.get_json()["command"] is None
    assert response.get_json()["command_source"] is None


def test_virtual_device_command_ack_skips_cloud_relay(monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual device command ack test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    original_mode = server_module.get_device_source_mode()

    def fail_requests_post(*args, **kwargs):
        raise AssertionError("virtual devices should not acknowledge cloud relay commands")

    monkeypatch.setattr(server_module, "RELAY_COMMAND_URL_LIST", ["https://example.invalid/device/command"])
    monkeypatch.setattr(server_module.requests, "post", fail_requests_post)

    try:
        server_module.set_device_source_mode(server_module.DEVICE_SOURCE_VIRTUAL)
        response = client.post(
            "/device/command/ack",
            json={"command_id": 123, "command_source": "relay", "device_source": "virtual"},
            headers={**device_headers(device_id), "X-Device-Source": "virtual"},
        )
    finally:
        server_module.set_device_source_mode(original_mode)

    assert response.status_code == 404
    assert response.get_json()["acknowledged"] is False
    assert response.get_json()["command_source"] == "relay"


def test_device_command_queue_keeps_latest_pending_command_per_device():
    target_device = default_test_device_id()
    with get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (target_device,))

    queue_command("ON", target_device=target_device)
    queue_command("AUTO", target_device=target_device)

    with get_db() as db:
        pending_rows = db.execute(
            """
            SELECT command
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL
            ORDER BY id DESC
            """,
            (target_device,),
        ).fetchall()

    assert [row["command"] for row in pending_rows] == ["AUTO"]

    response = client.get("/device/command", headers=device_headers(target_device))
    assert response.status_code == 200
    assert response.get_json()["command"] == "AUTO"
    assert response.get_json()["command_source"] == "queue"
    command_id = response.get_json()["command_id"]
    assert isinstance(command_id, int)

    with get_db() as db:
        queued = db.execute(
            """
            SELECT command, delivered_at
            FROM device_command_queue
            WHERE target_device = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (target_device,),
        ).fetchone()

    assert queued["command"] == "AUTO"
    assert queued["delivered_at"] is None

    ack = client.post(
        "/device/command/ack",
        json={"command_id": command_id, "command_source": "queue"},
        headers=device_headers(target_device),
    )
    assert ack.status_code == 200
    assert ack.get_json()["acknowledged"] is True

    with get_db() as db:
        delivered = db.execute(
            """
            SELECT command, delivered_at
            FROM device_command_queue
            WHERE target_device = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (target_device,),
        ).fetchone()

    assert delivered["command"] == "AUTO"
    assert delivered["delivered_at"] is not None


def test_basic_fields():
    data = get_response()

    assert isinstance(data["id"], int)
    assert isinstance(data["level"], float)
    assert isinstance(data["capacity_liters"], float)
    assert isinstance(data["remaining_liters"], float)


def test_level_range():
    data = get_response()

    assert 0 <= data["level"] <= 100


def test_api_status_endpoint():
    data = get_json("/status")
    assert data.get("server") == "Smart Water Tank API"
    assert data.get("status") == "running"
    assert "version" in data
    assert "swt_version" in data


def test_system_status_endpoint():
    data = get_json("/system/status")
    assert data.get("server") in {"online", "offline"}
    assert data.get("database") in {"online", "offline"}
    assert data.get("device") in {"online", "offline"}
    assert "telemetry_status" in data
    assert "signal_quality" in data
    assert "active_alert_count" in data
    assert "swt_version" in data


def test_build_swt_version_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("SWT_VERSION", "SWT-custom-build")
    monkeypatch.delenv("RENDER_GIT_BRANCH", raising=False)
    monkeypatch.delenv("GIT_BRANCH", raising=False)
    monkeypatch.delenv("BRANCH_NAME", raising=False)
    monkeypatch.delenv("SWT_BUILD_NUMBER", raising=False)
    monkeypatch.delenv("RENDER_DEPLOY_ID", raising=False)
    monkeypatch.delenv("BUILD_NUMBER", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("COMMIT_SHA", raising=False)

    assert server_module.build_swt_version() == "SWT-custom-build"


def test_build_swt_version_uses_requested_format_with_build_number(monkeypatch):
    monkeypatch.delenv("SWT_VERSION", raising=False)
    monkeypatch.delenv("RENDER_DEPLOY_ID", raising=False)
    monkeypatch.delenv("CI_PIPELINE_IID", raising=False)
    monkeypatch.delenv("CI_PIPELINE_ID", raising=False)
    monkeypatch.delenv("CI_JOB_ID", raising=False)
    monkeypatch.delenv("CI_JOB_IID", raising=False)
    monkeypatch.delenv("BUILD_NUMBER", raising=False)
    monkeypatch.setenv("RENDER_GIT_BRANCH", "master")
    monkeypatch.setenv("SWT_BUILD_NUMBER", "42")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")

    version = server_module.build_swt_version()
    expected_prefix = f"v.{datetime.now(timezone.utc).strftime('%y')}.42."
    assert re.fullmatch(rf"{re.escape(expected_prefix)}\d{{1,3}}", version)


def test_build_swt_version_keeps_requested_format_on_feature_branch(monkeypatch):
    monkeypatch.delenv("SWT_VERSION", raising=False)
    monkeypatch.delenv("CI_JOB_ID", raising=False)
    monkeypatch.delenv("CI_JOB_IID", raising=False)
    monkeypatch.setenv("RENDER_GIT_BRANCH", "feature/ota-ui")
    monkeypatch.setenv("SWT_BUILD_NUMBER", "42")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")

    version = server_module.build_swt_version()
    expected_prefix = f"v.{datetime.now(timezone.utc).strftime('%y')}.42."
    assert re.fullmatch(rf"{re.escape(expected_prefix)}\d{{1,3}}", version)


def test_build_swt_version_uses_explicit_sequence_number(monkeypatch):
    monkeypatch.delenv("SWT_VERSION", raising=False)
    monkeypatch.setenv("SWT_BUILD_NUMBER", "13")
    monkeypatch.setenv("SWT_SEQUENCE_NUMBER", "7")

    version = server_module.build_swt_version()
    expected_prefix = f"v.{datetime.now(timezone.utc).strftime('%y')}.13."
    assert version == f"{expected_prefix}7"


def test_build_swt_version_caps_numeric_groups(monkeypatch):
    monkeypatch.delenv("SWT_VERSION", raising=False)
    monkeypatch.setenv("SWT_BUILD_NUMBER", "1234")
    monkeypatch.setenv("SWT_SEQUENCE_NUMBER", "68508735654754")

    version = server_module.build_swt_version()
    expected_year = datetime.now(timezone.utc).strftime("%y")
    assert version == f"v.{expected_year}.34.754"


def test_device_status_endpoint():
    data = get_json("/device/status")
    assert data.get("device") in {"online", "offline"}
    assert "source" in data


def test_events_endpoint():
    data = get_json("/events", params={"limit": 5})
    assert isinstance(data, list)


def test_monitoring_summary_endpoint():
    data = get_json("/monitoring/summary")
    assert "relay" in data
    assert "alerts" in data
    assert "devices" in data


def test_build_admin_device_summary_counts_online_offline_and_alert_devices():
    if BASE_URL:
        pytest.skip("Admin device summary helper test is skipped against shared BASE_URL deployments.")

    device_ids = [
        "swt-summary-live-001",
        "swt-summary-recent-002",
        "swt-summary-stale-003",
        "swt-summary-empty-004",
    ]

    try:
        with get_db() as db:
            for device_id in device_ids:
                db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM ops_alerts WHERE kind = ?", ("summary_system_demo",))
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (?, 'summary_warning_demo', 'warning', 'demo warning', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (device_ids[0],),
            )
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (?, 'summary_danger_demo', 'danger', 'demo danger', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (device_ids[2],),
            )
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (?, 'summary_inactive_demo', 'warning', 'inactive warning', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (device_ids[3],),
            )
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (NULL, 'summary_system_demo', 'warning', 'system warning', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )

        summary = server_module.build_admin_device_summary(
            [
                {"device_id": device_ids[0], "telemetry_status": "live"},
                {"device_id": device_ids[1], "telemetry_status": "recent"},
                {"device_id": device_ids[2], "telemetry_status": "stale"},
                {"device_id": device_ids[3], "telemetry_status": "no-data"},
            ]
        )

        assert summary == {
            "total_registered_devices": 4,
            "online_devices": 2,
            "offline_devices": 2,
            "warning_alert_devices": 2,
        }
    finally:
        with get_db() as db:
            for device_id in device_ids:
                db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM ops_alerts WHERE kind = ?", ("summary_system_demo",))


def test_build_admin_known_devices_adds_status_and_alert_metadata():
    if BASE_URL:
        pytest.skip("Admin known-device metadata helper test is skipped against shared BASE_URL deployments.")

    device_id = "swt-admin-table-demo-001"

    try:
        with get_db() as db:
            db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (?, 'table_warning_demo', 'warning', 'Check inlet valve.', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (device_id,),
            )
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
                VALUES (?, 'table_danger_demo', 'danger', 'Motor dry run risk.', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (device_id,),
            )

        devices = server_module.build_admin_known_devices(
            accounts=[{"device_id": device_id, "display_name": "Admin Table Demo"}],
            available_devices=[{"device_id": device_id, "telemetry_status": "recent", "level": 57.4, "mode": "AUTO", "motor": "OFF"}],
        )

        assert len(devices) == 1
        assert devices[0]["admin_status"] == "online"
        assert devices[0]["admin_status_label"] == "Online"
        assert devices[0]["telemetry_status_label"] == "Recent"
        assert devices[0]["active_alert_count"] == 2
        assert devices[0]["warning_alert_label"] == "2 active"
        assert devices[0]["latest_alert_severity"] == "danger"
        assert devices[0]["latest_alert_message"]
    finally:
        with get_db() as db:
            db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))


def test_dashboard_bootstrap_endpoint():
    data = get_json("/dashboard/bootstrap", params={"event_limit": 5, "audit_limit": 5})
    assert "snapshot" in data
    assert "system_status" in data
    assert "monitoring_summary" in data
    assert "events" in data
    assert "audit" in data
    assert "generated_at" in data


def test_monitoring_alerts_endpoint():
    data = get_json("/monitoring/alerts", params={"limit": 5})
    assert isinstance(data, list)


def test_monitoring_alert_filters_and_resolve():
    session_client = ensure_logged_in()
    summary = get_json("/monitoring/summary")
    alerts = summary.get("alerts", [])
    if not alerts:
        pytest.skip("No active alerts available to filter/resolve.")

    first = alerts[0]
    filtered = get_json("/monitoring/alerts", params={"severity": first["severity"], "limit": 20})
    assert any(alert["id"] == first["id"] for alert in filtered)

    if BASE_URL:
        response = session_client.post(f"{BASE_URL}/monitoring/alerts/{first['id']}/resolve", headers={"X-CSRF-Token": fetch_csrf_token(session_client)})
        assert response.status_code == 200
        payload = response.json()
    else:
        response = session_client.post(f"/monitoring/alerts/{first['id']}/resolve", headers=csrf_headers())
        assert response.status_code == 200
        payload = response.get_json()
    assert payload["status"] == "resolved"


def test_monitoring_audit_endpoint():
    data = get_json("/monitoring/audit", params={"limit": 5})
    assert isinstance(data, list)


def test_device_detail_page_and_status():
    device_id = ensure_device_snapshot()

    session_client = ensure_logged_in()
    if BASE_URL:
        page = session_client.get(f"{BASE_URL}/devices/{device_id}")
        assert page.status_code == 200
        page_body = page.text
        detail = session_client.get(f"{BASE_URL}/devices/{device_id}/status")
        assert detail.status_code == 200
        payload = detail.json()
    else:
        page = session_client.get(f"/devices/{device_id}")
        assert page.status_code == 200
        page_body = page.get_data(as_text=True)
        detail = session_client.get(f"/devices/{device_id}/status")
        assert detail.status_code == 200
        payload = detail.get_json()

    assert payload["device_id"] == device_id
    assert "snapshot" in payload
    assert "system_status" in payload
    assert "monitoring_summary" in payload
    assert "history" in payload
    assert "audit" in payload
    assert "events" in payload
    assert len(payload["history"]) <= 10
    assert len(payload["audit"]) <= 10
    assert len(payload["alerts"]) <= 10
    assert len(payload["events"]) <= 10
    assert "Current Warnings" in page_body
    assert "Recent Events" in page_body
    assert "Bridge Services" not in page_body
    assert "Audit Trail" not in page_body
    assert "locale-datetime.js" in page_body


def test_dashboard_contains_firmware_update_link():
    device_id = ensure_device_snapshot()
    session_client = ensure_logged_in()
    if BASE_URL:
        page = session_client.get(f"{BASE_URL}/devices/{device_id}")
        assert page.status_code == 200
        body = page.text
    else:
        page = session_client.get(f"/devices/{device_id}")
        assert page.status_code == 200
        body = page.get_data(as_text=True)

    assert "Firmware Update" in body
    assert f"/devices/{device_id}/firmware/update" in body


def test_dashboard_password_reset_flow():
    if BASE_URL:
        pytest.skip("Password reset mutation test is skipped against shared BASE_URL deployments.")

    original_password = ensure_known_admin_password()
    new_password = "AdminReset2026!" if original_password != "AdminReset2026!" else "AdminReset2026!x"
    session_client = make_admin_client()

    try:
        response = session_client.post(
            "/account/password",
            data={
                "current_password": original_password,
                "new_password": new_password,
                "confirm_password": new_password,
            },
            headers=csrf_headers(),
        )
        assert response.status_code == 200
        assert b"updated successfully" in response.data

        fresh_client = app.test_client()
        failed = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": original_password},
        )
        assert failed.status_code == 200
        assert b"Invalid username or password" in failed.data

        success = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": new_password},
            follow_redirects=False,
        )
        assert success.status_code in (302, 303)
    finally:
        reset_client = make_admin_client()
        reset_response = reset_client.post(
            "/account/password",
            data={
                "current_password": new_password,
                "new_password": original_password,
                "confirm_password": original_password,
            },
            headers=csrf_headers(),
        )
        assert reset_response.status_code == 200


def test_boot_flag_can_reset_dashboard_password(monkeypatch):
    if BASE_URL:
        pytest.skip("Boot-time admin password reset test is skipped against shared BASE_URL deployments.")

    original_password = ensure_known_admin_password()
    interim_password = "AdminInterim2026!"
    boot_password = "AdminBoot2026!"

    try:
        set_dashboard_password(interim_password)
        monkeypatch.setattr("flask_app.server.LOGIN_PASSWORD", boot_password)
        monkeypatch.setattr("flask_app.server.RESET_ADMIN_PASSWORD_ON_BOOT", True)
        server_module.maybe_reset_admin_password_on_boot()

        fresh_client = app.test_client()
        failed = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": interim_password},
        )
        assert failed.status_code == 200
        assert b"Invalid username or password" in failed.data

        success = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": boot_password},
            follow_redirects=False,
        )
        assert success.status_code in (302, 303)
    finally:
        set_dashboard_password(original_password)


def test_boot_flag_trims_env_admin_password(monkeypatch):
    if BASE_URL:
        pytest.skip("Boot-time admin password trim test is skipped against shared BASE_URL deployments.")

    original_password = ensure_known_admin_password()
    trimmed_password = "AdminTrim2026!"

    try:
        monkeypatch.setattr("flask_app.server.LOGIN_PASSWORD", f"  {trimmed_password}  ")
        monkeypatch.setattr("flask_app.server.RESET_ADMIN_PASSWORD_ON_BOOT", True)
        server_module.maybe_reset_admin_password_on_boot()

        fresh_client = app.test_client()
        failed = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": f"  {trimmed_password}  "},
        )
        assert failed.status_code == 200
        assert b"Invalid username or password" in failed.data

        success = fresh_client.post(
            "/login/admin",
            data={"username": LOGIN_USERNAME, "password": trimmed_password},
            follow_redirects=False,
        )
        assert success.status_code in (302, 303)
    finally:
        set_dashboard_password(original_password)


def test_admin_can_register_customer_account():
    if BASE_URL:
        pytest.skip("Customer account mutation test is skipped against shared BASE_URL deployments.")

    admin_client = make_admin_client()
    response = admin_client.post(
        "/admin/customers",
        data={
            "device_id": "swt-node-customer",
            "display_name": "Customer Demo",
            "password": "CustomerFixturePass2026!",
        },
        headers=csrf_headers(),
    )
    assert response.status_code == 200
    assert b"Customer account saved for swt-node-customer." in response.data


def test_admin_can_reset_customer_password_from_customer_list():
    if BASE_URL:
        pytest.skip("Customer password reset test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    upsert_customer_account(device_id, "OriginalPass2026!", display_name="Tank Owner")

    admin_client = make_admin_client()
    response = admin_client.post(
        f"/admin/customers/{device_id}/password",
        data={"password": "ResetPass2026!"},
        headers=csrf_headers(),
    )
    assert response.status_code == 200
    assert f"Customer password reset for {device_id}.".encode() in response.data

    fresh_client = app.test_client()
    failed = fresh_client.post(
        "/login/customer",
        data={"username": device_id, "password": "OriginalPass2026!"},
    )
    assert failed.status_code == 200
    assert b"Invalid username or password" in failed.data

    success = fresh_client.post(
        "/login/customer",
        data={"username": device_id, "password": "ResetPass2026!"},
        follow_redirects=False,
    )
    assert success.status_code in (302, 303)
    assert success.headers["Location"].endswith("/customer/dashboard")


def test_admin_customer_page_keeps_reset_password_form_visible():
    if BASE_URL:
        pytest.skip("Customer reset-password UI test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    upsert_customer_account(device_id, "CustomerFixturePass2026!", display_name="Tank Owner")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert f'action="/admin/customers/{device_id}/password"' in body
    assert "Reset Password" in body


def test_customer_login_uses_device_id_username():
    if BASE_URL:
        pytest.skip("Customer auth test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    upsert_customer_account(device_id, "CustomerFixturePass2026!", display_name="Tank Owner")

    fresh_client = app.test_client()
    response = fresh_client.post(
        "/login/customer",
        data={"username": device_id, "password": "CustomerFixturePass2026!"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/customer/dashboard")


def test_customer_is_scoped_to_own_device_only():
    if BASE_URL:
        pytest.skip("Customer scope test is skipped against shared BASE_URL deployments.")

    own_device_id = ensure_device_snapshot()
    upsert_customer_account(own_device_id, "CustomerFixturePass2026!", display_name="Tank Owner")

    other_payload = {
        "level": 21.0,
        "motor": "OFF",
        "mode": "AUTO",
        "simulator": "OFF",
        "runtime": "0h 0m",
        "current_runtime": "0m 0s",
        "last_runtime": "0m 0s",
        "fill_time": "--",
        "leak": "NO",
        "pump_failure": "NO",
        "abnormal": "NO",
        "drip": "NO",
        "slow_leak": "NO",
        "pipe_leak": "NO",
        "ai_usage_rate": 0.4,
        "tomorrow_prediction": 8.0,
        "dry_run": "NO",
        "wifi": "ONLINE",
        "wifi_rssi": -55,
        "sensor": "OK",
        "sensor_info": "HC-SR04 CONNECTED",
        "sensor_distance_cm": 80.0,
        "tank_height_cm": 120.0,
        "tank_capacity_liters": 1000.0,
        "auto_status": "Auto waiting",
        "auto_status_tone": "info",
        "auto_timer": "Waiting for start",
        "tank_health": 90.0,
        "free_heap": 32000,
        "uptime_s": 456,
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }
    other_headers = {
        "X-Device-Id": "swt-node-other",
        "X-Device-Key": "OtherDeviceKey2026!",
    }
    original_map = dict(server_module.DEVICE_KEY_MAP)
    server_module.DEVICE_KEY_MAP["swt-node-other"] = "OtherDeviceKey2026!"
    try:
        response = client.post("/status", json=other_payload, headers=other_headers)
        assert response.status_code == 200
    finally:
        server_module.DEVICE_KEY_MAP.clear()
        server_module.DEVICE_KEY_MAP.update(original_map)

    customer_client = make_customer_client(own_device_id)

    customer_dashboard = customer_client.get("/customer/dashboard")
    assert customer_dashboard.status_code == 200

    own_status = customer_client.get("/last")
    assert own_status.status_code == 200
    assert own_status.get_json()["device_id"] == own_device_id

    forbidden = customer_client.get("/devices/swt-node-other/status")
    assert forbidden.status_code == 403

    admin_only = customer_client.get("/admin/customers")
    assert admin_only.status_code == 403

    admin_dashboard = customer_client.get("/admin/dashboard")
    assert admin_dashboard.status_code == 403

    control = customer_client.post("/motor/on", headers=csrf_headers())
    assert control.status_code == 200
    assert control.get_json()["target_device"] == own_device_id


def test_customer_dashboard_hides_admin_only_panels():
    if BASE_URL:
        pytest.skip("Customer dashboard UI test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    customer_client = make_customer_client(device_id)

    response = customer_client.get("/customer/dashboard")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert 'class="customer-dashboard"' in body
    assert '.customer-dashboard .admin-only{display:none!important}' in body
    assert "locale-datetime.js" in body
    assert "Start Pump" in body
    assert "Return To Auto" in body
    assert "Main Tank" in body
    assert "Source Tank" in body
    assert "Sync status" in body
    assert "Pump mode" in body
    assert "Status Summary" in body
    assert "Current Use Trend" in body
    assert 'id="customerAvailabilityNow"' in body
    assert 'id="customerUsageNow"' in body
    assert 'id="customerConnectionNow"' in body
    assert "compact-action" in body
    assert 'id="actionHint"' in body
    assert "customer-home-wide" not in body
    assert "Home Status" not in body
    assert "Device Forecast" not in body
    assert "Enable Main Simulator" not in body
    assert "Disable Main Simulator" not in body
    assert "Enable Source Simulator" not in body
    assert "Disable Source Simulator" not in body
    assert "Calibrate Sensor" not in body
    assert "Save Tank Config" not in body
    assert "Firmware Update" not in body


def test_admin_dashboard_hides_customer_only_panels():
    if BASE_URL:
        pytest.skip("Admin dashboard UI test is skipped against shared BASE_URL deployments.")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/admin/customers")

    customers_page = admin_client.get("/admin/customers")
    assert customers_page.status_code == 200
    body = customers_page.get_data(as_text=True)
    assert "Admin Dashboard" in body
    assert "Back to Dashboard" not in body


def test_admin_customers_page_shows_reboot_action_for_known_device():
    if BASE_URL:
        pytest.skip("Admin customers UI test is skipped against shared BASE_URL deployments.")

    ensure_device_snapshot()
    admin_client = make_admin_client()

    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "Reboot Device" in body
    assert "/reboot" in body


def test_admin_can_queue_device_reboot_from_admin_customers_page():
    if BASE_URL:
        pytest.skip("Admin reboot action test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    admin_client = make_admin_client()

    with get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    response = admin_client.post(
        f"/admin/customers/{device_id}/reboot",
        data={"q": ""},
        headers=csrf_headers(),
    )

    assert response.status_code == 200
    assert f"Reboot command queued for {device_id}." in response.get_data(as_text=True)

    with get_db() as db:
        queued = db.execute(
            """
            SELECT command, delivered_at
            FROM device_command_queue
            WHERE target_device = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()

    assert queued is not None
    assert queued["command"] == "REBOOT"
    assert queued["delivered_at"] is None


def test_admin_root_redirects_to_customer_accounts():
    if BASE_URL:
        pytest.skip("Admin root redirect test is skipped against shared BASE_URL deployments.")

    admin_client = make_admin_client()
    response = admin_client.get("/", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith("/admin/customers")


def test_admin_db_summary_reports_live_table_counts():
    if BASE_URL:
        pytest.skip("Admin db-summary test is skipped against shared BASE_URL deployments.")

    summary_device_id = "db-summary-device"
    summary_customer_id = "db-summary-customer"

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (summary_device_id,))
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (summary_device_id,))
        db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (summary_customer_id,))
        db.execute(
            """
            INSERT INTO tank_data(level, device_id, created_at)
            VALUES (?, ?, ?), (?, ?, ?)
            """,
            (
                41.5,
                summary_device_id,
                "2026-04-04 10:00:00",
                44.0,
                summary_device_id,
                "2026-04-04 10:05:00",
            ),
        )
        db.execute(
            """
            INSERT INTO device_command_queue(target_device, command, created_at, delivered_at)
            VALUES (?, ?, ?, ?), (?, ?, ?, ?)
            """,
            (
                summary_device_id,
                "ON",
                "2026-04-04 10:06:00",
                None,
                summary_device_id,
                "OFF",
                "2026-04-04 10:07:00",
                "2026-04-04 10:08:00",
            ),
        )
    upsert_customer_account(summary_customer_id, "SummaryFixturePass2026!", display_name="Summary Fixture")

    with get_db() as db:
        tank_row = db.execute(
            "SELECT COUNT(*) AS row_count, MAX(created_at) AS latest_created_at FROM tank_data"
        ).fetchone()
        command_row = db.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END) AS pending_row_count,
                MAX(created_at) AS latest_created_at
            FROM device_command_queue
            """
        ).fetchone()
        account_row = db.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_row_count,
                MAX(updated_at) AS latest_updated_at
            FROM customer_accounts
            """
        ).fetchone()

    response = make_admin_client().get("/admin/db-summary")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["database"]["path"] == server_module.DB_FILE
    assert payload["database"]["path_source"] == server_module.DB_PATH_SOURCE
    assert "file_sizes_bytes" in payload["database"]
    assert payload["maintenance"]["target_size_bytes"] == server_module.DB_TARGET_SIZE_BYTES
    assert payload["maintenance"]["temporary_size_guard_enabled"] == server_module.TEMP_DB_SIZE_GUARD_ENABLED
    assert payload["maintenance"]["temporary_hard_size_cap_enabled"] == server_module.TEMP_HARD_DB_CAP_ENABLED
    assert payload["maintenance"]["device_command_retention_days"] == server_module.DEVICE_COMMAND_RETENTION_DAYS
    assert payload["tables"]["tank_data"]["rows"] == int(tank_row["row_count"])
    assert payload["tables"]["tank_data"]["latest_created_at"] == tank_row["latest_created_at"]
    assert payload["tables"]["device_command_queue"]["rows"] == int(command_row["row_count"])
    assert payload["tables"]["device_command_queue"]["pending_rows"] == int(command_row["pending_row_count"])
    assert payload["tables"]["device_command_queue"]["latest_created_at"] == command_row["latest_created_at"]
    assert payload["tables"]["customer_accounts"]["rows"] == int(account_row["row_count"])
    assert payload["tables"]["customer_accounts"]["active_rows"] == int(account_row["active_row_count"])
    assert payload["tables"]["customer_accounts"]["latest_updated_at"] == account_row["latest_updated_at"]


def test_admin_db_summary_forbids_customer_access():
    if BASE_URL:
        pytest.skip("Admin db-summary auth test is skipped against shared BASE_URL deployments.")

    response = make_customer_client("swt-node-01").get("/admin/db-summary")

    assert response.status_code == 403


def test_customer_login_page_warns_when_render_auth_state_is_volatile(monkeypatch):
    if BASE_URL:
        pytest.skip("Render auth warning UI test is skipped against shared BASE_URL deployments.")

    monkeypatch.setattr(server_module, "IS_RENDER", True)
    monkeypatch.setattr(server_module, "DB_PATH", Path("/tmp/smart-water-tank/tank.db"))
    monkeypatch.setattr(server_module, "DB_FILE", "/tmp/smart-water-tank/tank.db")
    monkeypatch.setattr(server_module, "APP_SECRET_KEY_SOURCE", "default")

    response = app.test_client().get("/login/customer")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "Customer passwords and dashboard data can disappear after redeploys or restarts." in body
    assert "Browser and mobile sessions can be invalidated after restarts." in body


def test_customer_login_page_warns_when_secure_cookie_is_served_over_http():
    if BASE_URL:
        pytest.skip("Secure-cookie warning UI test is skipped against shared BASE_URL deployments.")

    original_value = app.config["SESSION_COOKIE_SECURE"]
    try:
        app.config["SESSION_COOKIE_SECURE"] = True
        response = app.test_client().get("/login/customer")
    finally:
        app.config["SESSION_COOKIE_SECURE"] = original_value

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Browsers will refuse to keep the login cookie" in body
    assert "SESSION_COOKIE_SECURE=false for local/LAN HTTP deployments." in body


def test_admin_customer_page_links_device_ids_to_device_detail():
    if BASE_URL:
        pytest.skip("Admin customer device-link UI test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    upsert_customer_account(device_id, "CustomerFixturePass2026!", display_name="Tank Owner")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "locale-datetime.js" in body
    assert f'href="/devices/{device_id}"' in body


def test_admin_customer_page_warns_when_render_uses_ephemeral_auth_state(monkeypatch):
    if BASE_URL:
        pytest.skip("Render auth warning UI test is skipped against shared BASE_URL deployments.")

    volatile_db_path = Path("data/test-runtime/render-volatile-auth-warning.db")
    monkeypatch.setattr(server_module, "IS_RENDER", True)
    monkeypatch.setattr(server_module, "DB_PATH", volatile_db_path)
    monkeypatch.setattr(server_module, "DB_FILE", str(volatile_db_path))
    monkeypatch.setattr(server_module, "APP_SECRET_KEY_SOURCE", "generated-secret-file")
    server_module.init_db()

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "Customer passwords and dashboard data can disappear after redeploys or restarts." in body
    assert "APP_SECRET_KEY is not explicitly set in Render environment variables." in body


def test_admin_customers_page_shows_registered_device_in_known_devices_and_search():
    if BASE_URL:
        pytest.skip("Admin customer known-device UI test is skipped against shared BASE_URL deployments.")

    device_id = "swt-node-search-demo"
    upsert_customer_account(device_id, "CustomerFixturePass2026!", display_name="Search Demo")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert 'id="device_search"' in body
    assert device_id in body
    assert "Search Demo" in body
    assert "Known Devices" not in body
    assert "Alerts / Warnings" in body
    assert "Devices" in body


def test_admin_customers_search_supports_partial_device_id():
    if BASE_URL:
        pytest.skip("Admin customer search test is skipped against shared BASE_URL deployments.")

    upsert_customer_account("swt-node-search-demo", "CustomerFixturePass2026!", display_name="Search Demo")
    upsert_customer_account("swt-node-other-demo", "CustomerFixturePass2026!", display_name="Other Demo")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers?q=search-dem")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert 'value="search-dem"' in body
    assert "swt-node-search-demo" in body
    assert "Search Demo" in body
    assert "swt-node-other-demo" not in body
    assert "1 device match" in body


def test_resolve_app_secret_key_uses_database_persisted_secret(monkeypatch):
    db_dir = Path("data/test-runtime")
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"secret-persisted-{int(time.time() * 1000)}.db"
    try:
        with sqlite3.connect(db_path) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (server_module.APP_SECRET_KEY_SETTING, "db-backed-secret-2026"),
            )
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)

        secret, source = server_module.resolve_app_secret_key(db_path)

        assert secret == "db-backed-secret-2026"
        assert source == f"{db_path}:app_settings"
    finally:
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass


def test_resolve_app_secret_key_persists_generated_secret_to_database(monkeypatch):
    db_dir = Path("data/test-runtime")
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"secret-generated-{int(time.time() * 1000)}.db"
    try:
        monkeypatch.delenv("APP_SECRET_KEY", raising=False)

        secret, source = server_module.resolve_app_secret_key(db_path)

        assert secret
        assert source != "default"
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (server_module.APP_SECRET_KEY_SETTING,),
            ).fetchone()
        assert row
        assert row[0] == secret
    finally:
        secret_file = db_path.parent / ".app_secret_key"
        if db_path.exists():
            try:
                db_path.unlink()
            except OSError:
                pass
        if secret_file.exists():
            try:
                secret_file.unlink()
            except OSError:
                pass


def test_seed_bootstrap_customer_accounts_restores_hashes_from_env(monkeypatch):
    accounts = [{
        "device_id": "swt-node-bootstrap",
        "display_name": "Bootstrap Owner",
        "password_hash": generate_password_hash("BootstrapPass2026!"),
        "active": 1,
        "cloud_feed_enabled": 0,
    }]
    payload = base64.b64encode(json.dumps({"accounts": accounts}).encode("utf-8")).decode("ascii")
    monkeypatch.setenv(server_module.CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV, payload)

    with sqlite3.connect(":memory:") as db:
        cursor = db.cursor()
        server_module.ensure_customer_accounts_table(cursor)
        server_module.seed_bootstrap_customer_accounts(cursor)
        row = cursor.execute(
            "SELECT device_id, display_name, password_hash, active, cloud_feed_enabled FROM customer_accounts WHERE device_id = ?",
            ("swt-node-bootstrap",),
        ).fetchone()

    assert row
    assert row[0] == "swt-node-bootstrap"
    assert row[1] == "Bootstrap Owner"
    assert check_password_hash(row[2], "BootstrapPass2026!")
    assert row[3] == 1
    assert row[4] == 0


def test_seed_bootstrap_dashboard_password_uses_hash_from_env(monkeypatch):
    password_hash = generate_password_hash("AdminBootstrapPass2026!")
    monkeypatch.setenv(server_module.DASHBOARD_PASSWORD_HASH_ENV, password_hash)

    with sqlite3.connect(":memory:") as db:
        cursor = db.cursor()
        server_module.ensure_app_settings_table(cursor)
        server_module.seed_bootstrap_dashboard_password(cursor)
        row = cursor.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (server_module.DASHBOARD_PASSWORD_SETTING,),
        ).fetchone()

    assert row
    assert row[0] == password_hash


def test_admin_customers_search_shows_password_form_for_known_device_without_account():
    if BASE_URL:
        pytest.skip("Admin customer known-device search UI test is skipped against shared BASE_URL deployments.")

    device_id = "swt-known-search-ui"
    original_map = dict(server_module.DEVICE_KEY_MAP)
    server_module.DEVICE_KEY_MAP[device_id] = "ServerSideKey2026!"
    try:
        admin_client = make_admin_client()
        response = admin_client.get("/admin/customers?q=known-search")
        assert response.status_code == 200

        body = response.get_data(as_text=True)
        assert 'action="/admin/customers"' in body
        assert f'name="device_id" value="{device_id}"' in body
        assert "Set Customer Password" in body
        assert "Device Details" in body
        assert 'data-panel-toggle' in body
        assert 'class="inline-panel"' in body
        assert "Save Password" in body
        assert "Delete" in body
        assert "Customer account" not in body
        assert "Server registered" not in body
        assert "1 device match" in body
    finally:
        server_module.DEVICE_KEY_MAP.clear()
        server_module.DEVICE_KEY_MAP.update(original_map)


def test_admin_customers_page_removes_separate_known_devices_section():
    if BASE_URL:
        pytest.skip("Admin customer layout test is skipped against shared BASE_URL deployments.")

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "Add Customer" in body
    assert "Devices" in body
    assert "Known Devices" not in body
    assert "Total Registered Devices" in body
    assert "Online Devices" in body
    assert "Offline Devices" in body
    assert "Warning / Alert Devices" in body
    assert "Global Alerts" in body
    assert "Audit Trail" not in body
    assert "Alerts / Warnings" in body
    assert "Device ID" in body
    assert 'data-device-sort="device_id"' in body
    assert 'data-device-sort="status"' in body
    assert 'data-device-sort="alerts"' in body


def test_admin_customer_page_can_edit_name_and_manage_services(monkeypatch):
    if BASE_URL:
        pytest.skip("Admin customer service-management test is skipped against shared BASE_URL deployments.")

    device_id = "swt-admin-cloud-edit-001"
    upsert_customer_account(
        device_id,
        "CloudTogglePass2026!",
        display_name="Original Owner",
        cloud_feed_enabled=0,
    )
    admin_client = make_admin_client()

    page = admin_client.get(f"/admin/customers?q={device_id}")
    assert page.status_code == 200
    page_body = page.get_data(as_text=True)
    assert "Edit Name" in page_body
    assert "Manage Services" in page_body
    assert 'data-panel-mode="modal"' in page_body
    assert 'class="service-modal-title"' in page_body
    assert 'name="cloud_feed_disabled"' in page_body
    assert 'name="cloud_feed_mode"' not in page_body
    assert "Cloud Feed Without AI" not in page_body
    assert "Cloud Feed With Full Features" not in page_body

    edit_response = admin_client.post(
        f"/admin/customers/{device_id}/edit",
        data={
            "csrf_token": TEST_CSRF_TOKEN,
            "q": device_id,
            "display_name": "Updated Owner",
        },
    )
    assert edit_response.status_code == 200
    edit_body = edit_response.get_data(as_text=True)
    assert f"Customer name updated for {device_id}." in edit_body
    assert "Updated Owner" in edit_body

    queued_commands = []
    monkeypatch.setattr(
        server_module,
        "queue_command",
        lambda command, target_device=None: queued_commands.append((command, target_device)) or {"queued": True},
    )

    service_response = admin_client.post(
        f"/admin/customers/{device_id}/services",
        data={
            "csrf_token": TEST_CSRF_TOKEN,
            "q": device_id,
            "source_tank_monitoring_enabled": "1",
        },
    )
    assert service_response.status_code == 200
    service_body = service_response.get_data(as_text=True)
    assert f"Service settings saved for {device_id}." in service_body
    assert "Manage Services" in service_body

    account = server_module.fetch_customer_account(device_id)
    assert account
    assert account["display_name"] == "Updated Owner"
    assert int(account["cloud_feed_enabled"]) == 1

    service_config = server_module.fetch_device_service_config(device_id)
    assert service_config["cloud_feed_mode"] == server_module.DEVICE_SERVICE_CLOUD_FEED_BASIC
    assert service_config["source_tank_monitoring_enabled"] is True
    assert service_config["ai_analysis_enabled"] is False
    assert service_config["effective_ai_analysis_enabled"] is False
    assert service_config["buzzer_enabled"] is False
    assert service_config["led_display_enabled"] is False
    assert queued_commands == [("SERVICECFG:1:0:0", device_id)]


def test_admin_customer_services_checkbox_cloud_disabled_maps_to_off(monkeypatch):
    if BASE_URL:
        pytest.skip("Admin customer checkbox service-management test is skipped against shared BASE_URL deployments.")

    device_id = "swt-admin-cloud-checkbox-001"
    upsert_customer_account(
        device_id,
        "CloudCheckboxPass2026!",
        display_name="Checkbox Owner",
        cloud_feed_enabled=1,
    )
    admin_client = make_admin_client()

    queued_commands = []
    monkeypatch.setattr(
        server_module,
        "queue_command",
        lambda command, target_device=None: queued_commands.append((command, target_device)) or {"queued": True},
    )

    response = admin_client.post(
        f"/admin/customers/{device_id}/services",
        data={
            "csrf_token": TEST_CSRF_TOKEN,
            "q": device_id,
            "source_tank_monitoring_enabled": "1",
            "ai_analysis_enabled": "1",
            "cloud_feed_disabled": "1",
            "buzzer_enabled": "1",
            "led_display_enabled": "1",
        },
    )

    assert response.status_code == 200
    service_config = server_module.fetch_device_service_config(device_id)
    account = server_module.fetch_customer_account(device_id)

    assert service_config["cloud_feed_mode"] == server_module.DEVICE_SERVICE_CLOUD_FEED_OFF
    assert service_config["ai_analysis_enabled"] is True
    assert service_config["effective_ai_analysis_enabled"] is False
    assert account is not None
    assert int(account["cloud_feed_enabled"]) == 0
    assert queued_commands == [("SERVICECFG:1:1:1", device_id)]


def test_service_config_prioritizes_cloud_disabled_over_saved_ai_preference():
    if BASE_URL:
        pytest.skip("Service-priority test is skipped against shared BASE_URL deployments.")

    device_id = "swt-cloud-priority-001"
    upsert_customer_account(
        device_id,
        "PriorityCloudPass2026!",
        display_name="Priority Owner",
        cloud_feed_enabled=1,
    )

    server_module.upsert_device_service_config(
        device_id,
        source_tank_monitoring_enabled=True,
        ai_analysis_enabled=True,
        cloud_feed_mode=server_module.DEVICE_SERVICE_CLOUD_FEED_OFF,
        buzzer_enabled=True,
        led_display_enabled=True,
    )

    service_config = server_module.fetch_device_service_config(device_id)
    account = server_module.fetch_customer_account(device_id)

    assert service_config["cloud_feed_mode"] == server_module.DEVICE_SERVICE_CLOUD_FEED_OFF
    assert service_config["cloud_feed_enabled"] is False
    assert service_config["ai_analysis_enabled"] is True
    assert service_config["effective_ai_analysis_enabled"] is False
    assert account is not None
    assert int(account["cloud_feed_enabled"]) == 0


def test_mobile_bootstrap_returns_resolved_service_config():
    if BASE_URL:
        pytest.skip("Mobile bootstrap service-config test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    assert device_id
    original_config = server_module.fetch_device_service_config(device_id)
    response = client.post(
        "/status",
        json=build_status_payload(
            device_id=device_id,
            channel_mode="both",
            telemetry_service="ON",
            command_service="ON",
        ),
        headers=device_headers(device_id),
    )
    assert response.status_code == 200

    try:
        server_module.upsert_device_service_config(
            device_id,
            source_tank_monitoring_enabled=True,
            ai_analysis_enabled=False,
            cloud_feed_mode=server_module.DEVICE_SERVICE_CLOUD_FEED_BASIC,
            buzzer_enabled=False,
            led_display_enabled=True,
        )

        bootstrap = client.get(
            "/api/mobile/bootstrap",
            query_string={"device_id": device_id},
            headers=mobile_auth_headers(),
        )

        assert bootstrap.status_code == 200
        payload = bootstrap.get_json()
        assert payload["service_config"]["device_id"] == device_id
        assert payload["service_config"]["cloud_feed_enabled"] is True
        assert payload["service_config"]["cloud_feed_mode"] == server_module.DEVICE_SERVICE_CLOUD_FEED_BASIC
        assert payload["service_config"]["effective_ai_analysis_enabled"] is False
        assert payload["service_config"]["buzzer_enabled"] is False
    finally:
        server_module.upsert_device_service_config(
            device_id,
            source_tank_monitoring_enabled=original_config["source_tank_monitoring_enabled"],
            ai_analysis_enabled=original_config["ai_analysis_enabled"],
            cloud_feed_mode=original_config["cloud_feed_mode"],
            buzzer_enabled=original_config["buzzer_enabled"],
            led_display_enabled=original_config["led_display_enabled"],
        )


def test_customer_dashboard_shows_cloud_feed_disabled_state():
    if BASE_URL:
        pytest.skip("Customer cloud-feed UI test is skipped against shared BASE_URL deployments.")

    device_id = "swt-cloud-disabled-ui-001"
    upsert_customer_account(
        device_id,
        "CustomerCloudPass2026!",
        display_name="Cloud Disabled Owner",
        cloud_feed_enabled=0,
    )
    customer_client = make_customer_client(device_id)

    response = customer_client.get("/customer/dashboard")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "Cloud Feed Disabled" in body
    assert "Cloud feed is disabled for this customer account." in body
    assert '"Cloud Feed"' in body or "Cloud Feed" in body


def test_customer_last_blocks_when_cloud_feed_is_disabled():
    if BASE_URL:
        pytest.skip("Customer cloud-feed route test is skipped against shared BASE_URL deployments.")

    device_id = "swt-cloud-disabled-last-001"
    upsert_customer_account(
        device_id,
        "CustomerCloudBlockPass2026!",
        display_name="Blocked Owner",
        cloud_feed_enabled=0,
    )
    customer_client = make_customer_client(device_id)

    response = customer_client.get("/last")

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["cloud_feed_enabled"] is False
    assert "Cloud feed is disabled" in payload["error"]


def test_mobile_last_blocks_when_cloud_feed_is_disabled():
    if BASE_URL:
        pytest.skip("Mobile cloud-feed route test is skipped against shared BASE_URL deployments.")

    device_id = "swt-cloud-disabled-mobile-001"
    upsert_customer_account(
        device_id,
        "CustomerMobileCloudPass2026!",
        display_name="Mobile Blocked Owner",
        cloud_feed_enabled=0,
    )

    response = client.get(
        "/api/mobile/last",
        headers=mobile_auth_headers(role="customer", username=device_id, device_id=device_id),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["cloud_feed_enabled"] is False
    assert "Cloud feed is disabled" in payload["error"]


def test_mobile_analytics_blocks_when_ai_is_disabled():
    if BASE_URL:
        pytest.skip("Mobile AI-disabled route test is skipped against shared BASE_URL deployments.")

    device_id = "swt-ai-disabled-mobile-001"
    upsert_customer_account(
        device_id,
        "CustomerAIDisabledPass2026!",
        display_name="AI Disabled Owner",
        cloud_feed_enabled=1,
    )
    server_module.upsert_device_service_config(
        device_id,
        source_tank_monitoring_enabled=True,
        ai_analysis_enabled=False,
        cloud_feed_mode=server_module.DEVICE_SERVICE_CLOUD_FEED_BASIC,
        buzzer_enabled=True,
        led_display_enabled=True,
    )

    response = client.get(
        "/api/mobile/analytics",
        query_string={"device_id": device_id},
        headers=mobile_auth_headers(role="customer", username=device_id, device_id=device_id),
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["ai_analysis_enabled"] is False
    assert "AI analysis is disabled" in payload["error"]


def test_admin_customers_page_shows_server_registered_device_without_telemetry():
    if BASE_URL:
        pytest.skip("Server-registered device UI test is skipped against shared BASE_URL deployments.")

    original_map = dict(server_module.DEVICE_KEY_MAP)
    server_module.DEVICE_KEY_MAP["swt-node-from-server"] = "ServerSideKey2026!"
    try:
        admin_client = make_admin_client()
        response = admin_client.get("/admin/customers")
        assert response.status_code == 200

        body = response.get_data(as_text=True)
        assert "swt-node-from-server" in body
        assert "Server registered" not in body
        assert "Customer account" not in body
        assert "Device Details" in body
        assert "Reset Password" in body
    finally:
        server_module.DEVICE_KEY_MAP.clear()
        server_module.DEVICE_KEY_MAP.update(original_map)


def test_admin_customers_page_registers_virtual_device_env_entries(tmp_path, monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual-device env registration UI test is skipped against shared BASE_URL deployments.")

    tests_root = tmp_path / "tests"
    virtual_devices_dir = tests_root / "virtual_devices"
    virtual_devices_dir.mkdir(parents=True)
    (virtual_devices_dir / "device-009.env").write_text(
        "SWT_VIRTUAL_DEVICE_ID=swt-virtual-admin-009\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module, "PROJECT_ROOT", tmp_path)

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert "swt-virtual-admin-009" in body
    assert "Server registered" not in body
    assert "Device Details" in body
    assert "Reset Password" in body

    with get_db() as db:
        row = db.execute(
            "SELECT registration_source FROM registered_devices WHERE device_id = ?",
            ("swt-virtual-admin-009",),
        ).fetchone()
    assert row
    assert row["registration_source"] == "virtual_device_env"


def test_admin_customers_page_skips_virtual_device_env_entries_when_seeding_is_disabled(tmp_path, monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual-device env registration UI test is skipped against shared BASE_URL deployments.")

    device_id = "swt-virtual-admin-disabled-010"
    with get_db() as db:
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (device_id,))

    tests_root = tmp_path / "tests"
    virtual_devices_dir = tests_root / "virtual_devices"
    virtual_devices_dir.mkdir(parents=True)
    (virtual_devices_dir / "device-010.env").write_text(
        f"SWT_VIRTUAL_DEVICE_ID={device_id}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server_module, "SEED_VIRTUAL_DEVICE_ENVS", False)

    admin_client = make_admin_client()
    response = admin_client.get("/admin/customers")
    assert response.status_code == 200

    body = response.get_data(as_text=True)
    assert device_id not in body

    with get_db() as db:
        row = db.execute(
            "SELECT registration_source FROM registered_devices WHERE device_id = ?",
            (device_id,),
        ).fetchone()
    assert row is None


def test_admin_delete_known_device_removes_device_records():
    if BASE_URL:
        pytest.skip("Admin delete-known-device test is skipped against shared BASE_URL deployments.")

    device_id = "swt-delete-me-001"
    admin_client = make_admin_client()

    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
        db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM ops_audit_log WHERE device_id = ?", (device_id,))
        db.execute(
            """
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, updated_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (device_id, "Delete Demo", generate_password_hash("DeletePass2026!")),
        )
        db.execute(
            """
            INSERT INTO registered_devices(device_id, registration_source, key_rule, first_seen_at, last_seen_at, updated_at)
            VALUES (?, 'virtual_device_env', 'tests/virtual_devices/device-001.env', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute(
            """
            INSERT INTO tank_data(level, motor, mode, device_source, sensor, wifi, device_id, created_at)
            VALUES (55.0, 'OFF', 'AUTO', 'virtual', 'OK', 'ONLINE', ?, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute(
            """
            INSERT INTO device_command_queue(target_device, command, created_at)
            VALUES (?, 'ON', CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute(
            """
            INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
            VALUES (?, 'delete_demo', 'warning', 'delete me', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute(
            """
            INSERT INTO ops_audit_log(actor, action, target_type, target_id, device_id, details, created_at)
            VALUES ('admin', 'seed_delete_demo', 'device', ?, ?, '{}', CURRENT_TIMESTAMP)
            """,
            (device_id, device_id),
        )

    response = admin_client.post(
        f"/admin/customers/{device_id}/delete",
        data={"csrf_token": TEST_CSRF_TOKEN},
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"Deleted device {device_id} from admin records." in body

    with get_db() as db:
        assert db.execute("SELECT 1 FROM customer_accounts WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM registered_devices WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM tank_data WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM device_command_queue WHERE target_device = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM ops_alerts WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM ops_audit_log WHERE device_id = ?", (device_id,)).fetchone() is None
        ignored_row = db.execute("SELECT note FROM ignored_devices WHERE device_id = ?", (device_id,)).fetchone()
    assert ignored_row
    assert ignored_row["note"] == "admin_delete"


def test_admin_delete_known_device_hides_virtual_env_device_until_readded(tmp_path, monkeypatch):
    if BASE_URL:
        pytest.skip("Admin delete-known-device suppression test is skipped against shared BASE_URL deployments.")

    device_id = "swt-virtual-hidden-011"
    tests_root = tmp_path / "tests"
    virtual_devices_dir = tests_root / "virtual_devices"
    virtual_devices_dir.mkdir(parents=True)
    (virtual_devices_dir / "device-011.env").write_text(
        f"SWT_VIRTUAL_DEVICE_ID={device_id}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(server_module, "PROJECT_ROOT", tmp_path)

    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM registered_devices WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    admin_client = make_admin_client()
    first_response = admin_client.get("/admin/customers")
    assert first_response.status_code == 200
    assert device_id in first_response.get_data(as_text=True)

    delete_response = admin_client.post(
        f"/admin/customers/{device_id}/delete",
        data={"csrf_token": TEST_CSRF_TOKEN},
    )
    assert delete_response.status_code == 200
    delete_body = delete_response.get_data(as_text=True)
    assert f"Deleted device {device_id} from admin records." in delete_body

    second_response = admin_client.get("/admin/customers")
    assert second_response.status_code == 200
    second_body = second_response.get_data(as_text=True)
    assert device_id not in second_body

    with get_db() as db:
        assert db.execute("SELECT 1 FROM registered_devices WHERE device_id = ?", (device_id,)).fetchone() is None
        ignored_row = db.execute("SELECT note FROM ignored_devices WHERE device_id = ?", (device_id,)).fetchone()
    assert ignored_row
    assert ignored_row["note"] == "admin_delete"


def test_purge_configured_virtual_device_records_removes_virtual_device_rows(tmp_path, monkeypatch):
    if BASE_URL:
        pytest.skip("Virtual-device purge test is skipped against shared BASE_URL deployments.")

    device_id = "swt-virtual-purge-001"
    tests_root = tmp_path / "tests"
    virtual_devices_dir = tests_root / "virtual_devices"
    virtual_devices_dir.mkdir(parents=True)
    (virtual_devices_dir / "device-001.env").write_text(
        f"SWT_VIRTUAL_DEVICE_ID={device_id}\n",
        encoding="utf-8",
    )

    temp_db_path = tmp_path / "data" / "purge-virtual-devices.db"
    monkeypatch.setattr(server_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(server_module, "DB_PATH", temp_db_path)
    monkeypatch.setattr(server_module, "DB_FILE", str(temp_db_path))
    monkeypatch.setattr(server_module, "SEED_VIRTUAL_DEVICE_ENVS", False)
    monkeypatch.setattr(server_module, "PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT", False)
    server_module.init_db()

    with get_db() as db:
        db.execute(
            """
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, updated_at)
            VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP)
            """,
            (device_id, "Virtual Purge Device", generate_password_hash("VirtualPurge2026!")),
        )
        db.execute(
            """
            INSERT INTO registered_devices(device_id, registration_source, key_rule, first_seen_at, last_seen_at, updated_at)
            VALUES (?, 'virtual_device_env', 'tests/virtual_devices/device-001.env', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute("INSERT INTO tank_data(device_id, created_at) VALUES (?, CURRENT_TIMESTAMP)", (device_id,))
        db.execute("INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)", (device_id, "AUTO"))
        db.execute(
            """
            INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at)
            VALUES (?, 'virtual_cleanup', 'warning', 'cleanup me', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )
        db.execute(
            """
            INSERT INTO ops_audit_log(actor, action, target_type, target_id, device_id, details, created_at)
            VALUES ('admin', 'seed_virtual_device', 'device', ?, ?, '{}', CURRENT_TIMESTAMP)
            """,
            (device_id, device_id),
        )
        db.execute(
            """
            INSERT INTO ignored_devices(device_id, note, created_at, updated_at)
            VALUES (?, 'seed_virtual_device', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (device_id,),
        )

    monkeypatch.setattr(server_module, "PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT", True)
    deleted_counts = server_module.purge_configured_virtual_device_records()

    assert deleted_counts["device_ids"] == 1
    assert deleted_counts["customer_accounts"] == 1
    assert deleted_counts["registered_devices"] == 1
    assert deleted_counts["tank_data"] == 1
    assert deleted_counts["device_command_queue"] == 1
    assert deleted_counts["ops_alerts"] == 1
    assert deleted_counts["ops_audit_log"] == 1
    assert deleted_counts["ignored_devices"] == 1

    with get_db() as db:
        assert db.execute("SELECT 1 FROM customer_accounts WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM registered_devices WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM tank_data WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM device_command_queue WHERE target_device = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM ops_alerts WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM ops_audit_log WHERE device_id = ?", (device_id,)).fetchone() is None
        assert db.execute("SELECT 1 FROM ignored_devices WHERE device_id = ?", (device_id,)).fetchone() is None


def test_admin_device_dashboard_redirects_to_device_detail():
    if BASE_URL:
        pytest.skip("Admin device dashboard UI test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    admin_client = make_admin_client()

    response = admin_client.get(f"/admin/dashboard/{device_id}", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"].endswith(f"/devices/{device_id}")


def test_drain_relay_queue_drops_permanent_failures(monkeypatch):
    if BASE_URL:
        pytest.skip("Relay queue mutation test is skipped against shared BASE_URL deployments.")

    payload = {"device_id": "swt-node-01", "level": 55.0}
    with get_db() as db:
        db.execute("DELETE FROM relay_queue")
        db.execute(
            """
            INSERT INTO relay_queue (payload, next_attempt_at)
            VALUES (?, CURRENT_TIMESTAMP)
            """,
            (server_module.json.dumps(payload),),
        )
        row = db.execute("SELECT id FROM relay_queue ORDER BY id DESC LIMIT 1").fetchone()
        relay_id = row["id"]

    monkeypatch.setattr("server.relay_status_to_cloud", lambda item: "drop")
    drain_relay_queue(max_items=5)

    with get_db() as db:
        remaining = db.execute("SELECT id FROM relay_queue WHERE id = ?", (relay_id,)).fetchone()
    assert remaining is None


def test_firmware_update_redirect():
    ensure_device_snapshot()
    session_client = ensure_logged_in()
    if BASE_URL:
        response = session_client.get(f"{BASE_URL}/firmware/update", allow_redirects=False)
        assert response.status_code in (302, 303)
        location = response.headers["Location"]
    else:
        response = session_client.get("/firmware/update", follow_redirects=False)
        assert response.status_code in (302, 303)
        location = response.headers["Location"]

    assert location.endswith("/update")


def test_device_firmware_update_redirect():
    device_id = ensure_device_snapshot()
    session_client = ensure_logged_in()
    if BASE_URL:
        response = session_client.get(f"{BASE_URL}/devices/{device_id}/firmware/update", allow_redirects=False)
        assert response.status_code in (302, 303)
        location = response.headers["Location"]
    else:
        response = session_client.get(f"/devices/{device_id}/firmware/update", follow_redirects=False)
        assert response.status_code in (302, 303)
        location = response.headers["Location"]

    assert location.endswith("/update")


def test_firmware_update_redirect_ignores_loopback_snapshot(monkeypatch):
    device_id = ensure_device_snapshot()
    session_client = ensure_logged_in()

    original_device = DEVICE

    def fake_snapshot(_device_id):
        return {
            "device_id": device_id,
            "source_ip": "127.0.0.1",
        }

    monkeypatch.setattr("server.fetch_device_snapshot", fake_snapshot)
    monkeypatch.setattr("server.DEVICE", "http://192.168.1.50")

    response = session_client.get(f"/devices/{device_id}/firmware/update", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"] == "http://192.168.1.50/update"

    monkeypatch.setattr("server.DEVICE", original_device)


def test_firmware_update_redirect_prefers_device_local_url(monkeypatch):
    device_id = ensure_device_snapshot()
    session_client = ensure_logged_in()

    def fake_snapshot(_device_id):
        return {
            "device_id": device_id,
            "device_local_url": "http://192.168.1.50",
            "source_ip": "203.0.113.20",
        }

    monkeypatch.setattr("server.fetch_device_snapshot", fake_snapshot)

    response = session_client.get(f"/devices/{device_id}/firmware/update", follow_redirects=False)
    assert response.status_code in (302, 303)
    assert response.headers["Location"] == "http://192.168.1.50/update"


def test_history_endpoint():
    data = get_json("/history", params={"days": 1})
    assert isinstance(data, list)
    if data:
        assert "time" in data[0]
        assert "level" in data[0]
        assert "source_tank_level" in data[0]


def test_status_accepts_source_tank_aliases_and_history_returns_them():
    if BASE_URL:
        pytest.skip("Source-tank alias ingestion test is skipped against shared BASE_URL deployments.")

    device_id = "swt-source-alias-001"
    server_module.clear_runtime_caches(device_id)
    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    payload = {
        "device_id": device_id,
        "device_source": server_module.get_device_source_mode(),
        "level": 48.0,
        "motor": "OFF",
        "mode": "AUTO",
        "runtime": "0h 0m",
        "current_runtime": "0m 0s",
        "last_runtime": "0m 0s",
        "fill_time": "--",
        "leak": "NO",
        "pump_failure": "NO",
        "abnormal": "NO",
        "drip": "NO",
        "slow_leak": "NO",
        "pipe_leak": "NO",
        "ai_usage_rate": 0.5,
        "tomorrow_prediction": 10.0,
        "dry_run": "NO",
        "wifi": "ONLINE",
        "wifi_rssi": -52,
        "sensor": "OK",
        "sensor_info": "HC-SR04 CONNECTED",
        "sensor_distance_cm": 61.5,
        "tank_height_cm": 120.0,
        "tank_capacity_liters": 1000.0,
        "auto_status": "Auto waiting",
        "auto_status_tone": "info",
        "auto_timer": "Waiting for start",
        "tank_health": 93.0,
        "free_heap": 30000,
        "uptime_s": 321,
        "source_tank_level": 73.5,
        "source_tank_sensor": "OK",
        "source_tank_sensor_info": "Source tank healthy",
        "source_tank_sensor_distance_cm": 44.2,
        "source_tank_service": "ON",
        "device_local_url": "http://192.168.1.50",
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }

    server_module.process_telemetry_payload(payload, transport="test")

    snapshot = server_module.fetch_device_snapshot(device_id)
    assert snapshot["lower_tank_level"] == 73.5
    assert snapshot["source_tank_level"] == 73.5
    assert snapshot["lower_tank_service"] == "ON"
    assert snapshot["source_tank_service"] == "ON"

    data = get_json("/history", params={"days": 1, "device_id": device_id})
    assert data
    assert data[-1]["lower_tank_level"] == 73.5
    assert data[-1]["source_tank_level"] == 73.5


def test_analytics_endpoint():
    data = get_json("/analytics", params={"days": 1})
    assert "range" in data
    assert "insights" in data
    assert "health" in data
    assert "alerts" in data


def test_analytics_endpoint_compacts_large_series(monkeypatch):
    if BASE_URL:
        pytest.skip("Analytics compaction test is skipped against shared BASE_URL deployments.")

    device_id = "swt-analytics-compact-001"
    base_time = datetime.now(timezone.utc) - timedelta(hours=6)

    server_module.analytics_cache.clear()
    monkeypatch.setattr(server_module, "ANALYTICS_LEVEL_SERIES_MAX_POINTS", 40)
    monkeypatch.setattr(server_module, "ANALYTICS_MOTOR_SERIES_MAX_POINTS", 20)

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        for index in range(180):
            created_at = (base_time + timedelta(minutes=2 * index)).strftime(server_module.TIMESTAMP_FORMAT)
            level = max(12.0, 92.0 - (index * 0.32))
            motor = "ON" if index % 14 in {0, 1, 2} else "OFF"
            db.execute(
                """
                INSERT INTO tank_data(
                    level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
                    pump_failure, dry_run, wifi, wifi_rssi, sensor, lower_tank_level,
                    ai_usage_rate, tomorrow_prediction, device_source, device_id, created_at
                )
                VALUES (?, ?, 'AUTO', 'NO', 'NO', 'NO', 'NO',
                        'NO', 'NO', 'ONLINE', -54, 'OK', NULL,
                        0.55, 9.0, ?, ?, ?)
                """,
                (
                    level,
                    motor,
                    server_module.get_device_source_mode(),
                    device_id,
                    created_at,
                ),
            )

    response = make_admin_client().get(f"/analytics?days=1&device_id={device_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert len(payload["levels"]["time"]) == len(payload["levels"]["values"])
    assert len(payload["levels"]["time"]) <= 40
    assert len(payload["motor"]["time"]) == len(payload["motor"]["values"])
    assert len(payload["motor"]["time"]) <= 20

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
    server_module.analytics_cache.clear()


def test_analytics_csv_export_endpoint_contains_all_four_reports():
    if BASE_URL:
        pytest.skip("Analytics CSV export test is skipped against shared BASE_URL deployments.")

    device_id = "swt-analytics-export-001"
    base_time = datetime.now(timezone.utc) - timedelta(hours=3)
    rows = [
        (base_time, 82.0, "OFF"),
        (base_time + timedelta(hours=1), 74.5, "ON"),
        (base_time + timedelta(hours=2), 68.0, "OFF"),
    ]

    server_module.analytics_cache.clear()
    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        for created_at, level, motor in rows:
            db.execute(
                """
                INSERT INTO tank_data(
                    level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
                    pump_failure, dry_run, wifi, wifi_rssi, sensor, lower_tank_level,
                    ai_usage_rate, tomorrow_prediction, device_source, device_id, created_at
                )
                VALUES (?, ?, 'AUTO', 'NO', 'NO', 'NO', 'NO',
                        'NO', 'NO', 'ONLINE', -52, 'OK', NULL,
                        0.5, 10.0, ?, ?, ?)
                """,
                (
                    level,
                    motor,
                    server_module.get_device_source_mode(),
                    device_id,
                    created_at.strftime(server_module.TIMESTAMP_FORMAT),
                ),
            )

    admin_client = make_admin_client()
    response = admin_client.get(f"/analytics/export.csv?days=1&device_id={device_id}")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/csv")
    assert "attachment;" in response.headers["Content-Disposition"]
    body = response.get_data(as_text=True)
    assert "report_type,report_title,device_id,range_label" in body
    assert "tank_level_history" in body
    assert "daily_water_use" in body
    assert "hourly_water_pattern" in body
    assert "pump_activity" in body
    assert device_id in body

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
    server_module.analytics_cache.clear()


def test_dashboard_contains_analytics_csv_download_link():
    customer_client = make_customer_client(default_test_device_id())

    response = customer_client.get("/customer/dashboard")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'id="analyticsDownloadLink"' in body
    assert "/analytics/export.csv" in body


def test_status_keeps_only_latest_snapshot_when_history_is_disabled(monkeypatch):
    if BASE_URL:
        pytest.skip("Minimal-history storage test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    monkeypatch.setattr(server_module, "TELEMETRY_HISTORY_ENABLED", False)

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    payload = {
        "level": 48.0,
        "motor": "OFF",
        "mode": "AUTO",
        "simulator": "OFF",
        "source_tank_simulator": "OFF",
        "runtime": "0h 0m",
        "current_runtime": "0m 0s",
        "last_runtime": "0m 0s",
        "fill_time": "--",
        "leak": "NO",
        "pump_failure": "NO",
        "abnormal": "NO",
        "drip": "NO",
        "slow_leak": "NO",
        "pipe_leak": "NO",
        "ai_usage_rate": 0.5,
        "tomorrow_prediction": 10.0,
        "dry_run": "NO",
        "wifi": "ONLINE",
        "wifi_rssi": -52,
        "sensor": "OK",
        "sensor_info": "HC-SR04 CONNECTED",
        "sensor_distance_cm": 61.5,
        "tank_height_cm": 120.0,
        "tank_capacity_liters": 1000.0,
        "auto_status": "Auto waiting",
        "auto_status_tone": "info",
        "auto_timer": "Waiting for start",
        "tank_health": 93.0,
        "free_heap": 30000,
        "uptime_s": 321,
        "device_local_url": "http://192.168.1.50",
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }

    first = client.post("/status", json=payload, headers=device_headers(device_id))
    assert first.status_code == 200

    second_payload = dict(payload)
    second_payload["level"] = 61.0
    second_payload["wifi_rssi"] = -47
    second = client.post("/status", json=second_payload, headers=device_headers(device_id))
    assert second.status_code == 200

    with get_db() as db:
        rows = db.execute(
            "SELECT level, wifi_rssi FROM tank_data WHERE device_id = ? ORDER BY id ASC",
            (device_id,),
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["level"] == 61.0
    assert rows[0]["wifi_rssi"] == -47


def test_status_caps_rows_per_device_when_history_limit_is_enabled(monkeypatch):
    if BASE_URL:
        pytest.skip("Per-device history cap test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    monkeypatch.setattr(server_module, "TELEMETRY_HISTORY_ENABLED", True)
    monkeypatch.setattr(server_module, "MAX_TELEMETRY_ROWS_PER_DEVICE", 2)

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    payload = {
        "level": 48.0,
        "motor": "OFF",
        "mode": "AUTO",
        "simulator": "OFF",
        "source_tank_simulator": "OFF",
        "runtime": "0h 0m",
        "current_runtime": "0m 0s",
        "last_runtime": "0m 0s",
        "fill_time": "--",
        "leak": "NO",
        "pump_failure": "NO",
        "abnormal": "NO",
        "drip": "NO",
        "slow_leak": "NO",
        "pipe_leak": "NO",
        "ai_usage_rate": 0.5,
        "tomorrow_prediction": 10.0,
        "dry_run": "NO",
        "wifi": "ONLINE",
        "wifi_rssi": -52,
        "sensor": "OK",
        "sensor_info": "HC-SR04 CONNECTED",
        "sensor_distance_cm": 61.5,
        "tank_height_cm": 120.0,
        "tank_capacity_liters": 1000.0,
        "auto_status": "Auto waiting",
        "auto_status_tone": "info",
        "auto_timer": "Waiting for start",
        "tank_health": 93.0,
        "free_heap": 30000,
        "uptime_s": 321,
        "device_local_url": "http://192.168.1.50",
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }

    for level in (48.0, 61.0, 72.0):
        next_payload = dict(payload)
        next_payload["level"] = level
        response = client.post("/status", json=next_payload, headers=device_headers(device_id))
        assert response.status_code == 200

    with get_db() as db:
        rows = db.execute(
            "SELECT level FROM tank_data WHERE device_id = ? ORDER BY id ASC",
            (device_id,),
        ).fetchall()

    assert [row["level"] for row in rows] == [61.0, 72.0]


def test_status_prunes_old_auxiliary_rows_while_ingesting(monkeypatch):
    if BASE_URL:
        pytest.skip("Retention-pruning test is skipped against shared BASE_URL deployments.")

    device_id = default_test_device_id()
    monkeypatch.setattr(server_module, "DEVICE_COMMAND_RETENTION_DAYS", 1)
    monkeypatch.setattr(server_module, "OPS_ALERT_RETENTION_DAYS", 1)
    monkeypatch.setattr(server_module, "OPS_AUDIT_RETENTION_DAYS", 1)
    monkeypatch.setattr(server_module, "maybe_maintain_database", lambda *args, **kwargs: False)

    with get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
        db.execute("DELETE FROM ops_audit_log WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (device_id,))
        db.execute(
            """
            INSERT INTO device_command_queue(target_device, command, created_at, delivered_at)
            VALUES (?, ?, ?, ?), (?, ?, ?, ?)
            """,
            (
                device_id,
                "ON",
                "2026-04-01 09:00:00",
                "2026-04-01 09:05:00",
                device_id,
                "OFF",
                "2026-04-04 09:00:00",
                None,
            ),
        )
        db.execute(
            """
            INSERT INTO ops_audit_log(actor, action, target_type, target_id, device_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "admin",
                "old_action",
                "device",
                device_id,
                device_id,
                "{}",
                "2026-04-01 08:00:00",
                "admin",
                "new_action",
                "device",
                device_id,
                device_id,
                "{}",
                "2026-04-04 08:00:00",
            ),
        )
        db.execute(
            """
            INSERT INTO ops_alerts(device_id, kind, severity, message, active, created_at, updated_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                "old_alert",
                "warn",
                "old resolved alert",
                0,
                "2026-04-01 07:00:00",
                "2026-04-01 07:10:00",
                "2026-04-01 07:10:00",
                device_id,
                "active_alert",
                "warn",
                "active alert stays",
                1,
                "2026-04-01 07:00:00",
                "2026-04-04 08:30:00",
                None,
            ),
        )

    payload = {
        "level": 48.0,
        "motor": "OFF",
        "mode": "AUTO",
        "simulator": "OFF",
        "source_tank_simulator": "OFF",
        "runtime": "0h 0m",
        "current_runtime": "0m 0s",
        "last_runtime": "0m 0s",
        "fill_time": "--",
        "leak": "NO",
        "pump_failure": "NO",
        "abnormal": "NO",
        "drip": "NO",
        "slow_leak": "NO",
        "pipe_leak": "NO",
        "ai_usage_rate": 0.5,
        "tomorrow_prediction": 10.0,
        "dry_run": "NO",
        "wifi": "ONLINE",
        "wifi_rssi": -52,
        "sensor": "OK",
        "sensor_info": "HC-SR04 CONNECTED",
        "sensor_distance_cm": 61.5,
        "tank_height_cm": 120.0,
        "tank_capacity_liters": 1000.0,
        "auto_status": "Auto waiting",
        "auto_status_tone": "info",
        "auto_timer": "Waiting for start",
        "tank_health": 93.0,
        "free_heap": 30000,
        "uptime_s": 321,
        "device_local_url": "http://192.168.1.50",
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }

    response = client.post("/status", json=payload, headers=device_headers(device_id))
    assert response.status_code == 200

    with get_db() as db:
        command_rows = db.execute(
            "SELECT command, delivered_at FROM device_command_queue WHERE target_device = ? ORDER BY id ASC",
            (device_id,),
        ).fetchall()
        audit_rows = db.execute(
            "SELECT action FROM ops_audit_log WHERE device_id = ? ORDER BY id ASC",
            (device_id,),
        ).fetchall()
        alert_rows = db.execute(
            "SELECT kind, active FROM ops_alerts WHERE device_id = ? ORDER BY id ASC",
            (device_id,),
        ).fetchall()

    assert [(row["command"], row["delivered_at"]) for row in command_rows] == [("OFF", None)]
    assert [row["action"] for row in audit_rows] == ["new_action"]
    assert [(row["kind"], row["active"]) for row in alert_rows] == [("active_alert", 1)]


def test_history_endpoint_returns_empty_when_history_is_disabled(monkeypatch):
    if BASE_URL:
        pytest.skip("Minimal-history route test is skipped against shared BASE_URL deployments.")

    monkeypatch.setattr(server_module, "TELEMETRY_HISTORY_ENABLED", False)
    ensure_device_snapshot()
    admin_client = make_admin_client()

    response = admin_client.get("/history?days=1")

    assert response.status_code == 200
    assert response.get_json() == []


def test_analytics_endpoint_returns_empty_payload_when_history_is_disabled(monkeypatch):
    if BASE_URL:
        pytest.skip("Minimal-history analytics test is skipped against shared BASE_URL deployments.")

    monkeypatch.setattr(server_module, "TELEMETRY_HISTORY_ENABLED", False)
    server_module.analytics_cache.clear()
    ensure_device_snapshot()
    admin_client = make_admin_client()

    response = admin_client.get("/analytics?days=1")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["alerts"] == ["Analytics history is disabled on this deployment."]
    assert payload["daily"]["dates"] == []
    assert payload["levels"]["time"] == []


def test_validate_runtime_db_configuration_requires_render_persistent_db(monkeypatch):
    if BASE_URL:
        pytest.skip("Render persistent-db validation test is skipped against shared BASE_URL deployments.")

    monkeypatch.setattr(server_module, "IS_RENDER", True)
    monkeypatch.setattr(server_module, "REQUIRE_RENDER_PERSISTENT_DB", True)
    monkeypatch.setattr(server_module, "DB_PATH", Path("/tmp/smart-water-tank/tank.db"))
    monkeypatch.setattr(server_module, "DB_FILE", "/tmp/smart-water-tank/tank.db")
    monkeypatch.setattr(server_module, "DB_PATH_SOURCE", "render-fallback")
    monkeypatch.setattr(
        server_module,
        "DB_PATH_REJECTED",
        [{"path": "/var/data/tank.db", "source": "render-default"}],
    )

    with pytest.raises(RuntimeError) as exc_info:
        server_module.validate_runtime_db_configuration()

    message = str(exc_info.value)
    assert "/tmp/smart-water-tank/tank.db" in message
    assert "/var/data/tank.db" in message
    assert "supports disks" in message


def test_ml_predict_endpoint_returns_forecast(monkeypatch):
    if BASE_URL:
        pytest.skip("ML endpoint test is skipped against shared BASE_URL deployments.")

    device_id = "ml-node-forecast"
    seed_forecast_history(device_id)
    monkeypatch.setattr(server_module, "load_level_forecast_artifact", make_fake_forecast_loader(delta_percent=-4.5))

    response = make_admin_client().get(f"/ml/predict?device_id={device_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["device_id"] == device_id
    assert payload["model"]["family"] == "HistGradientBoostingRegressor"
    assert payload["model"]["artifact_path"].endswith("artifacts/level_forecast_model.pkl")
    assert payload["current_level_percent"] == pytest.approx(57.0, abs=0.001)
    assert payload["predicted_level_percent"] == pytest.approx(52.5, abs=0.001)
    assert payload["predicted_delta_percent"] == pytest.approx(-4.5, abs=0.001)
    assert payload["predicted_remaining_liters"] == pytest.approx(525.0, abs=0.01)
    assert payload["rows_considered"] >= 30


def test_ml_predict_endpoint_reports_missing_artifact(monkeypatch):
    if BASE_URL:
        pytest.skip("ML endpoint test is skipped against shared BASE_URL deployments.")

    device_id = "ml-node-missing-artifact"
    seed_forecast_history(device_id)

    def missing_loader(force_reload=False):
        raise FileNotFoundError("missing model")

    monkeypatch.setattr(server_module, "load_level_forecast_artifact", missing_loader)

    response = make_admin_client().get(f"/ml/predict?device_id={device_id}")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["device_id"] == device_id
    assert "Train it first" in payload["error"]


def test_ml_predict_endpoint_respects_customer_scope(monkeypatch):
    if BASE_URL:
        pytest.skip("ML endpoint test is skipped against shared BASE_URL deployments.")

    own_device_id = "ml-node-customer-own"
    other_device_id = "ml-node-customer-other"
    seed_forecast_history(own_device_id)
    seed_forecast_history(other_device_id)
    monkeypatch.setattr(server_module, "load_level_forecast_artifact", make_fake_forecast_loader(delta_percent=-2.0))

    customer_client = make_customer_client(own_device_id)

    own_response = customer_client.get("/ml/predict")
    assert own_response.status_code == 200
    assert own_response.get_json()["device_id"] == own_device_id

    forbidden = customer_client.get(f"/ml/predict?device_id={other_device_id}")
    assert forbidden.status_code == 403


def test_required_string_fields():
    data = get_response()

    for key in ("auto_status", "auto_status_tone", "runtime", "current_runtime", "last_runtime"):
        assert key in data
        assert isinstance(data[key], str)


def test_auto_status_tone_values():
    data = get_response()

    assert data["auto_status_tone"] in ["ok", "warn", "bad", "info"]


def test_simulator_flag():
    data = get_response()

    assert data["simulator"] in ["ON", "OFF"]


def test_source_tank_simulator_flag():
    data = get_response()

    assert data["source_tank_simulator"] in ["ON", "OFF"]


def test_fill_time_format():
    data = get_response()

    fill_time = data.get("fill_time", "")
    assert isinstance(fill_time, str)
    if fill_time not in {"", "--"}:
        assert "m" in fill_time or "s" in fill_time


def test_motor_status():
    data = get_response()

    assert data["motor"] in ["ON", "OFF"]


def test_mode():
    data = get_response()

    assert data["mode"] in ["AUTO", "MANUAL"]


def test_wifi_status():
    data = get_response()

    assert data["wifi"] in ["ONLINE", "OFFLINE", "CONNECTED", "OK"]


def test_sensor_status():
    data = get_response()

    assert data["sensor"] in ["OK", "ERROR", "DISCONNECTED"]


def test_abnormal_status():
    data = get_response()

    assert data["abnormal"] in ["YES", "NO"]


def test_leak_flags():
    data = get_response()

    for key in ("leak", "drip", "slow_leak", "pipe_leak", "pump_failure", "dry_run"):
        assert data[key] in ["YES", "NO"]


def test_capacity_validation():
    data = get_response()

    assert data["capacity_liters"] > 0
    assert data["tank_capacity_liters"] > 0


def test_remaining_liters_calculation():
    data = get_response()

    expected = data["capacity_liters"] * data["level"] / 100
    assert abs(data["remaining_liters"] - expected) < 5


def test_wifi_signal_range():
    data = get_response()

    assert -100 <= data["wifi_rssi"] <= 0


@pytest.mark.parametrize("key", ["level", "capacity_liters", "remaining_liters", "tank_health"])
def test_numeric_fields_finite(key):
    data = get_response()

    value = data.get(key)
    assert isinstance(value, (int, float))
    assert value == value


def test_tank_health_range():
    data = get_response()

    assert 0 <= data["tank_health"] <= 100


def test_sensor_distance_when_available():
    data = get_response()

    distance = data.get("sensor_distance_cm")
    if distance is None:
        return
    if isinstance(distance, str) and distance.lower() == "null":
        return
    assert float(distance) >= 0



def test_control_routes_require_csrf_token():
    if BASE_URL:
        pytest.skip("CSRF enforcement test is skipped against shared BASE_URL deployments.")

    device_id = ensure_device_snapshot()
    customer_client = make_customer_client(device_id)
    response = customer_client.post("/motor/on")
    assert response.status_code == 400
