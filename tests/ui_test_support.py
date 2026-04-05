from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys_path_root = str(Path(__file__).resolve().parents[1])
if sys_path_root not in sys.path:
    sys.path.insert(0, sys_path_root)

import server as server_module
from server import DEVICE_KEY_MAP, LOGIN_USERNAME, app, get_dashboard_password, set_dashboard_password


BASE_URL = os.environ.get("TEST_BASE_URL")
client = app.test_client()
TEST_CSRF_TOKEN = "test-csrf-token"
TEST_ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD", "AdminFixturePass2026!")


def default_test_device_credentials():
    if DEVICE_KEY_MAP:
        device_id = next(iter(DEVICE_KEY_MAP))
        return device_id, DEVICE_KEY_MAP[device_id]
    if server_module.DEVICE_KEY_WILDCARD_RULES:
        rule = server_module.DEVICE_KEY_WILDCARD_RULES[0]
        return f"{rule['prefix']}001", rule["key"]
    return None, None


def default_test_device_id():
    device_id, _device_key = default_test_device_credentials()
    return device_id


def device_headers(device_id=None, device_key=None):
    fallback_device_id, fallback_device_key = default_test_device_credentials()
    if not fallback_device_id or not fallback_device_key:
        return {}
    resolved_device_id = device_id or fallback_device_id
    resolved_device_key = device_key or DEVICE_KEY_MAP.get(resolved_device_id) or fallback_device_key
    return {
        "X-Device-Id": resolved_device_id,
        "X-Device-Key": resolved_device_key,
        "X-Device-Source": server_module.get_device_source_mode(),
    }


def ensure_known_admin_password(password=TEST_ADMIN_PASSWORD):
    if BASE_URL:
        return os.environ.get("TEST_ADMIN_PASSWORD") or get_dashboard_password() or "Admin123"
    set_dashboard_password(password)
    return password


def csrf_headers(extra=None):
    headers = {"X-CSRF-Token": TEST_CSRF_TOKEN}
    if extra:
        headers.update(extra)
    return headers


def fetch_csrf_token(session_client, path="/admin/customers"):
    if not BASE_URL:
        return TEST_CSRF_TOKEN
    response = session_client.get(f"{BASE_URL}{path}")
    assert response.status_code == 200
    match = re.search(r'name="csrf-token" content="([^"]+)"', response.text)
    assert match
    return match.group(1)


def ensure_logged_in():
    if BASE_URL:
        import requests

        session = requests.Session()
        response = session.post(
            f"{BASE_URL}/login/admin",
            data={"username": LOGIN_USERNAME, "password": ensure_known_admin_password()},
            allow_redirects=False,
        )
        assert response.status_code in (302, 303)
        return session
    with client.session_transaction() as session_data:
        session_data["logged_in"] = True
        session_data["username"] = "admin"
        session_data["role"] = "admin"
        session_data["device_id"] = None
        session_data["csrf_token"] = TEST_CSRF_TOKEN
    return client


def make_admin_client():
    admin_client = app.test_client()
    with admin_client.session_transaction() as session_data:
        session_data["logged_in"] = True
        session_data["username"] = "admin"
        session_data["role"] = "admin"
        session_data["device_id"] = None
        session_data["csrf_token"] = TEST_CSRF_TOKEN
    return admin_client


def make_customer_client(device_id):
    customer_client = app.test_client()
    with customer_client.session_transaction() as session_data:
        session_data["logged_in"] = True
        session_data["username"] = device_id
        session_data["role"] = "customer"
        session_data["device_id"] = device_id
        session_data["csrf_token"] = TEST_CSRF_TOKEN
    return customer_client


def get_response():
    session_client = ensure_logged_in()
    if BASE_URL:
        response = session_client.get(f"{BASE_URL}/last")
        assert response.status_code == 200
        return response.json()
    response = session_client.get("/last")
    assert response.status_code == 200
    return response.get_json()


def get_json(path, params=None):
    session_client = ensure_logged_in()
    if BASE_URL:
        response = session_client.get(f"{BASE_URL}{path}", params=params)
        assert response.status_code == 200
        return response.json()
    response = session_client.get(path, query_string=params)
    assert response.status_code == 200
    return response.get_json()


def build_status_payload(**overrides):
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
        "lower_tank_level": None,
        "lower_sensor": "DISABLED",
        "lower_sensor_info": "Lower sensor disabled",
        "lower_sensor_distance_cm": None,
        "device_local_url": "http://192.168.1.50",
        "firmware_version": "1.1.0",
        "reset_reason": "Software/System restart",
    }
    payload.update(overrides)
    return payload


def ensure_device_snapshot():
    response = client.post("/status", json=build_status_payload(), headers=device_headers())
    assert response.status_code == 200
    return get_response()["device_id"]


def mobile_auth_headers(role="admin", username="admin", device_id=None):
    token = server_module.issue_mobile_token(
        {
            "role": role,
            "username": username,
            "device_id": device_id,
        }
    )
    return {"Authorization": f"Bearer {token}"}
