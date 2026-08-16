from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from flask_app import rag_routes, server


CRITICAL_ROUTES = {
    "/health": {"GET"},
    "/status": {"GET", "POST"},
    "/book-demo": {"GET", "POST"},
    "/chatbot/ask": {"POST"},
    "/api/mobile/auth/login": {"POST"},
    "/api/mobile/auth/logout": {"POST"},
    "/api/mobile/bootstrap": {"GET"},
    "/api/mobile/analytics": {"GET"},
    "/api/mobile/local-sync": {"POST"},
    "/api/mobile/last": {"GET"},
    "/api/mobile/motor/on": {"POST"},
    "/api/mobile/motor/off": {"POST"},
    "/api/mobile/sensor/calibrate": {"POST"},
    "/api/mobile/sensor/configure": {"POST"},
    "/api/mobile/device/status": {"GET"},
    "/api/mobile/device/services": {"GET", "POST"},
    "/api/mobile/device/thresholds": {"GET", "POST"},
    "/devices/<device_id>/configuration": {"POST"},
    "/devices/<device_id>/status": {"GET"},
    "/downloads/installation-guide": {"GET"},
}


def route_methods():
    return {
        rule.rule: set(rule.methods) - {"HEAD", "OPTIONS"}
        for rule in server.app.url_map.iter_rules()
    }


def test_all_critical_routes_are_registered_with_expected_methods():
    actual = route_methods()

    missing = sorted(set(CRITICAL_ROUTES) - set(actual))
    assert not missing, f"Critical Flask routes are missing: {missing}"
    for route, expected_methods in CRITICAL_ROUTES.items():
        assert expected_methods <= actual[route], (
            f"{route} must support {sorted(expected_methods)}; "
            f"registered methods are {sorted(actual[route])}"
        )


def test_health_and_status_discovery_contracts_return_json():
    client = server.app.test_client()

    health = client.get("/health")
    status = client.get("/status")

    assert health.status_code == 200
    assert health.is_json
    assert status.status_code == 200
    assert status.is_json
    assert status.get_json()["status"] == "running"
    assert status.get_json()["server"] == "SaleWell Smart Tank API"


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("get", "/api/mobile/bootstrap"),
        ("get", "/api/mobile/analytics"),
        ("get", "/api/mobile/last"),
        ("post", "/api/mobile/local-sync"),
        ("post", "/api/mobile/motor/on"),
        ("post", "/api/mobile/motor/off"),
        ("post", "/api/mobile/sensor/calibrate"),
        ("post", "/api/mobile/sensor/configure"),
    ),
)
def test_mobile_device_apis_reject_unauthenticated_requests(method, path):
    response = getattr(server.app.test_client(), method)(path, json={})

    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["error"] == "authentication required"


def test_admin_and_customer_resources_are_not_public():
    client = server.app.test_client()

    admin_response = client.get("/devices/swt-critical-001/status")
    guide_response = client.get("/downloads/installation-guide")

    assert admin_response.status_code in {302, 303}
    assert "/admin" in admin_response.headers["Location"]
    assert guide_response.status_code in {302, 303}
    assert "/customer" in guide_response.headers["Location"]


def test_telemetry_post_rejects_invalid_json():
    response = server.app.test_client().post(
        "/status",
        data="not-json",
        content_type="text/plain",
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid json"}


def test_authenticated_telemetry_is_sent_to_shared_ingestion(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "authenticate_device_request", lambda payload: (True, "swt-critical-001", 200))
    monkeypatch.setattr(server, "resolve_request_device_source", lambda payload: server.DEVICE_SOURCE_REAL)
    monkeypatch.setattr(server.CAPACITY_FEATURES, "enabled", lambda feature: True)

    def fake_ingest(payload, **kwargs):
        captured["payload"] = dict(payload)
        captured["kwargs"] = kwargs

    monkeypatch.setattr(server, "ingest_device_sync", fake_ingest)

    response = server.app.test_client().post(
        "/status",
        json={"device_id": "untrusted-client-value", "level": 61.5, "motor": "OFF", "mode": "AUTO"},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == "saved"
    assert captured["payload"]["device_id"] == "swt-critical-001"
    assert captured["payload"]["device_source"] == server.DEVICE_SOURCE_REAL
    assert captured["kwargs"]["authenticated_device_id"] == "swt-critical-001"
    assert captured["kwargs"]["transport"] == "http"
    assert captured["kwargs"]["defer_postprocess"] is True


def test_public_chatbot_returns_stable_disabled_contract(monkeypatch):
    monkeypatch.setenv("RAG_ENABLED", "false")
    rag_routes._rate_windows.clear()

    response = server.app.test_client().post("/chatbot/ask", json={"question": "How do I install it?"})

    assert response.status_code == 503
    assert response.is_json
    assert "unavailable" in response.get_json()["error"].lower()


def test_booking_api_validates_required_customer_details():
    client = server.app.test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "critical-api-token"

    response = client.post(
        "/book-demo",
        data={"csrf_token": "critical-api-token", "return_to": "pricing"},
    )

    assert response.status_code == 400
    assert b"required" in response.data.lower()


def test_analytics_payload_is_bound_to_firmware_device(monkeypatch):
    start = datetime(2026, 8, 7, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    base_payload = {
        "range": {"label": "Last 7 days"},
        "analysis": {"quality": {"status": "ready", "daily_usage_reliable": True}},
        "insights": {"avg_level": 60.0},
        "daily": {"dates": [], "values": []},
    }
    monkeypatch.setattr(server, "build_analytics", lambda *args, **kwargs: base_payload)
    monkeypatch.setattr(server, "fixed_ai_analysis_window", lambda: (start, end, "Last 7 days"))
    monkeypatch.setattr(server, "analytics_payload_version", lambda payload: "critical-test-v1")

    payload = server.build_dashboard_analytics(start, end, "Last 7 days", device_id="swt-critical-001")

    assert payload["device_id"] == "swt-critical-001"
    assert payload["sync_contract"] == {
        "version": 1,
        "device_id": "swt-critical-001",
        "telemetry_authority": "firmware",
        "analytics_authority": "flask",
        "control_authority": "firmware",
        "analytics_generated_at": payload.get("analytics_generated_at"),
    }


def test_runtime_configuration_save_persists_queues_and_returns_json(monkeypatch):
    device_id = "swt-critical-001"
    saved_config = {
        "device_id": device_id,
        "auto_mode_enabled": True,
        "municipal_sensor_enabled": False,
        "master_turbidity_enabled": False,
        "slave_turbidity_enabled": False,
    }
    queue_result = {"id": 42, "command": "SERVICECFG10:test"}
    monkeypatch.setattr(server, "current_scope_device_id", lambda value: value)
    monkeypatch.setattr(server, "fetch_device_snapshot", lambda value: {})
    monkeypatch.setattr(server, "upsert_device_service_config", lambda *args, **kwargs: saved_config)
    monkeypatch.setattr(server, "build_device_service_command", lambda config: "SERVICECFG10:test")
    monkeypatch.setattr(server, "safe_queue_device_detail_command", lambda *args, **kwargs: (queue_result, ""))
    monkeypatch.setattr(server, "disable_orphaned_device_simulators", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(server, "log_audit_event", lambda **kwargs: None)
    monkeypatch.setattr(server, "current_actor_username", lambda: "critical-test-admin")
    view = inspect.unwrap(server.admin_device_detail_configuration)

    with server.app.test_request_context(
        f"/devices/{device_id}/configuration",
        method="POST",
        data={"device_setup_type": "custom", "auto_mode_enabled": "1"},
        headers={"X-Requested-With": "XMLHttpRequest"},
    ):
        response, status_code = view(device_id)

    payload = response.get_json()
    assert status_code == 200
    assert payload["ok"] is True
    assert payload["service_config"] == saved_config
    assert payload["queued_command"] == "SERVICECFG10:test"
    assert payload["queue_result"] == queue_result
    assert "Configuration saved" in payload["message"]
