import pytest

from flask_app import server
from flask_app.capacity_features import CapacityFeatureRegistry


@pytest.mark.parametrize("previous,current", [("OFF", "ON"), ("ON", "OFF")])
def test_bootstrap_pump_status_uses_same_sample_as_tank_level(monkeypatch, previous, current):
    device_id = "swt-test-001"
    old = {"device_id": device_id, "motor": previous, "level": 53.8}
    latest = {"device_id": device_id, "motor": current, "level": 62.96}

    def system_status(snapshot, **kwargs):
        return {"synchronized_status": {"pump": {"state": snapshot["motor"]}}}

    cached = {"snapshot": old, "system_status": system_status(old)}
    monkeypatch.setattr(server, "CAPACITY_FEATURES", CapacityFeatureRegistry(environ={}))
    monkeypatch.setattr(server, "mobile_customer_cloud_feed_block_response", lambda: None)
    monkeypatch.setattr(server, "current_mobile_scope_device_id", lambda _: device_id)
    monkeypatch.setattr(server, "resolve_mobile_user", lambda: {"role": "customer"})
    monkeypatch.setattr(server, "load_persisted_dashboard_summary", lambda _: cached)
    monkeypatch.setattr(server, "load_dashboard_snapshot", lambda _: latest)
    monkeypatch.setattr(server, "snapshot_has_live_device_data", lambda _: True)
    monkeypatch.setattr(server, "resolve_device_service_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(server, "fetch_device_automation_settings", lambda *args, **kwargs: {})
    monkeypatch.setattr(server, "build_current_saved_config", lambda _: {})
    monkeypatch.setattr(server, "pop_device_mobile_action", lambda _: None)
    monkeypatch.setattr(server, "build_system_status_payload", system_status)

    with server.app.test_request_context("/api/mobile/bootstrap"):
        payload = server.mobile_bootstrap.__wrapped__().get_json()

    assert payload["snapshot"]["level"] == 62.96
    assert payload["snapshot"]["motor"] == current
    assert payload["system_status"]["synchronized_status"]["pump"]["state"] == current
    assert cached["system_status"]["synchronized_status"]["pump"]["state"] == previous
