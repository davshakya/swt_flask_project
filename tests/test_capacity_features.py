from __future__ import annotations

import pytest

from flask_app.capacity_features import BoundedRequestMetrics, CapacityFeatureRegistry


def test_capacity_features_default_to_safe_legacy_behavior():
    registry = CapacityFeatureRegistry(environ={})

    assert registry.enabled("legacy_tank_data_writes") is True
    assert registry.enabled("latest_state_writes") is False
    assert registry.enabled("narrow_history_writes") is False
    assert registry.enabled("device_sync_api") is False
    assert registry.warnings == ()


def test_capacity_feature_dependency_is_disabled_without_prerequisite():
    registry = CapacityFeatureRegistry(environ={"FEATURE_ADAPTIVE_HISTORY": "true"})

    assert registry.snapshot()["adaptive_history"]["configured"] is True
    assert registry.enabled("adaptive_history") is False
    assert registry.warnings == (
        "FEATURE_ADAPTIVE_HISTORY disabled because required feature(s) are off: "
        "FEATURE_NARROW_HISTORY_WRITES",
    )


def test_capacity_feature_dependency_chain_can_be_enabled():
    registry = CapacityFeatureRegistry(
        environ={
            "FEATURE_CAPACITY_SCHEMA": "true",
            "FEATURE_LATEST_STATE_WRITES": "true",
            "FEATURE_NARROW_HISTORY_WRITES": "true",
            "FEATURE_ADAPTIVE_HISTORY": "true",
        }
    )

    assert registry.enabled("latest_state_writes") is True
    assert registry.enabled("narrow_history_writes") is True
    assert registry.enabled("adaptive_history") is True
    assert registry.warnings == ()


def test_unknown_capacity_feature_is_rejected():
    registry = CapacityFeatureRegistry(environ={})

    with pytest.raises(KeyError, match="Unknown capacity feature"):
        registry.enabled("not_registered")


def test_request_metrics_are_bounded_and_aggregated_by_route():
    current_time = [60.0]
    metrics = BoundedRequestMetrics(max_buckets=2, clock=lambda: current_time[0])

    metrics.record("/status", 200, 10)
    metrics.record("/status", 500, 30)
    current_time[0] = 120.0
    metrics.record("/health", 200, 5)
    current_time[0] = 180.0
    metrics.record("/system/status", 200, 15)

    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 2
    assert snapshot["errors"] == 0
    assert [bucket["minute_epoch"] for bucket in snapshot["buckets"]] == [120, 180]
    assert snapshot["buckets"][0]["routes"]["/health"] == {
        "requests": 1,
        "errors": 0,
        "average_duration_ms": 5.0,
        "max_duration_ms": 5.0,
    }


def test_flask_request_hook_records_route_metrics(monkeypatch):
    from flask_app import server

    registry = CapacityFeatureRegistry(
        environ={"FEATURE_CAPACITY_METRICS": "true", "FEATURE_REQUEST_TIMING": "true"}
    )
    metrics = BoundedRequestMetrics()
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry)
    monkeypatch.setattr(server, "CAPACITY_REQUEST_METRICS", metrics)

    response = server.app.test_client().get("/health")

    assert response.status_code == 200
    snapshot = metrics.snapshot()
    assert snapshot["requests"] == 1
    assert snapshot["errors"] == 0
    assert snapshot["buckets"][-1]["routes"]["/health"]["requests"] == 1


def test_system_status_exposes_effective_flags_only_when_metrics_enabled(monkeypatch):
    from flask_app import server

    registry = CapacityFeatureRegistry(environ={"FEATURE_CAPACITY_METRICS": "true"})
    metrics = BoundedRequestMetrics()
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry)
    monkeypatch.setattr(server, "CAPACITY_REQUEST_METRICS", metrics)
    monkeypatch.setattr(server, "current_scope_device_id", lambda _device_id: "swt-test-001")
    monkeypatch.setattr(server, "load_persisted_dashboard_summary", lambda _device_id: {"system_status": {"status": "online"}})

    with server.app.test_request_context("/system/status"):
        response = server.system_status.__wrapped__()

    payload = response.get_json()
    assert payload["status"] == "online"
    assert payload["capacity"]["features"]["legacy_tank_data_writes"]["enabled"] is True
    assert payload["capacity"]["features"]["device_sync_api"]["enabled"] is False
    assert payload["capacity"]["request_metrics"]["scope"] == "passenger_process"
