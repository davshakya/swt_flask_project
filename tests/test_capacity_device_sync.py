from __future__ import annotations

from types import SimpleNamespace

import pytest

from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app import server


def enabled_registry(**extra):
    values = {"FEATURE_SHARED_INGESTION_SERVICE": "true", "FEATURE_DEVICE_SYNC_API": "true"}
    values.update({f"FEATURE_{name.upper()}": "true" for name, enabled in extra.items() if enabled})
    return CapacityFeatureRegistry(environ=values)


def test_device_sync_is_disabled_with_legacy_fallback(monkeypatch):
    monkeypatch.setattr(server, "CAPACITY_FEATURES", CapacityFeatureRegistry(environ={}))

    response = server.app.test_client().post("/api/device/sync", json={})

    assert response.status_code == 404
    assert response.get_json() == {"error": "device sync protocol is not enabled", "fallback": "/status"}


@pytest.mark.parametrize("payload", ({}, {"protocol_version": 2, "telemetry": {}}, {"protocol_version": "bad", "telemetry": {}}))
def test_device_sync_validates_protocol_contract(monkeypatch, payload):
    monkeypatch.setattr(server, "CAPACITY_FEATURES", enabled_registry())

    response = server.app.test_client().post("/api/device/sync", json=payload)

    assert response.status_code == 400


def test_device_sync_combines_ingestion_ack_command_and_interval(monkeypatch):
    monkeypatch.setattr(
        server,
        "CAPACITY_FEATURES",
        enabled_registry(sync_command_ack=True, sync_command_delivery=True, sync_interval_hints=True),
    )
    monkeypatch.setattr(server, "authenticate_device_request", lambda payload=None: (True, "swt-authenticated", None))
    captured = {}

    def ingest(telemetry, **kwargs):
        captured["telemetry"] = telemetry
        captured["kwargs"] = kwargs
        return SimpleNamespace(duplicate=False, outcome="saved")

    monkeypatch.setattr(server, "ingest_device_sync", ingest)
    monkeypatch.setattr(server, "acknowledge_queued_command_id", lambda device_id, command_id, result=None: True)
    monkeypatch.setattr(server, "clear_mqtt_command", lambda device_id: None)
    monkeypatch.setattr(
        server,
        "peek_queued_command",
        lambda device_id: {"id": 9, "command": "OFF", "request_id": "request-9", "desired_state": "OFF"},
    )

    response = server.app.test_client().post(
        "/api/device/sync",
        json={
            "protocol_version": 1,
            "device_id": "swt-authenticated",
            "boot_id": "boot-1",
            "sequence_number": 7,
            "telemetry": {"level": 80, "motor": "ON", "sensor": "OK"},
            "command_ack": {"command_id": 8, "status": "accepted"},
        },
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["accepted"] is True
    assert payload["result"] == "saved"
    assert payload["next_sync_seconds"] == 30
    assert payload["command"]["command_id"] == 9
    assert payload["command_ack"]["acknowledged"] is True
    assert captured["telemetry"]["device_id"] == "swt-authenticated"
    assert captured["telemetry"]["boot_id"] == "boot-1"
    assert captured["telemetry"]["sequence_number"] == 7
    assert captured["kwargs"]["transport"] == "device_sync"
