from __future__ import annotations

import pytest

from flask_app.capacity_ingestion import ingest_device_payload
from flask_app.capacity_features import CapacityFeatureRegistry


def test_shared_ingestion_preserves_authenticated_device_identity():
    captured = {}

    def processor(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {**payload, "_telemetry_sync_result": "saved"}

    result = ingest_device_payload(
        {"device_id": "body-device", "level": 55},
        processor=processor,
        authenticated_device_id="authenticated-device",
        source_ip="127.0.0.1",
        transport="unit-test",
        defer_postprocess=True,
    )

    assert result.saved is True
    assert result.telemetry["device_id"] == "authenticated-device"
    assert captured["payload"]["device_id"] == "authenticated-device"
    assert captured["kwargs"] == {
        "source_ip": "127.0.0.1",
        "transport": "unit-test",
        "defer_postprocess": True,
    }


@pytest.mark.parametrize("outcome", ("saved", "duplicate", "ignored"))
def test_shared_ingestion_reports_idempotency_outcome(outcome):
    result = ingest_device_payload(
        {"device_id": "swt-test"},
        processor=lambda payload, **_kwargs: {**payload, "_telemetry_sync_result": outcome},
    )

    assert result.outcome == outcome
    assert result.saved is (outcome == "saved")
    assert result.duplicate is (outcome == "duplicate")
    assert result.ignored is (outcome == "ignored")


def test_shared_ingestion_rejects_invalid_contracts():
    with pytest.raises(TypeError, match="payload must be a mapping"):
        ingest_device_payload([], processor=lambda payload, **kwargs: payload)

    with pytest.raises(TypeError, match="processor must return a dictionary"):
        ingest_device_payload({}, processor=lambda payload, **kwargs: None)


def test_device_sync_api_requires_shared_ingestion_service():
    registry = CapacityFeatureRegistry(environ={"FEATURE_DEVICE_SYNC_API": "true"})

    assert registry.enabled("device_sync_api") is False
    assert "FEATURE_SHARED_INGESTION_SERVICE" in registry.warnings[0]


@pytest.mark.parametrize("shared_enabled", (False, True))
def test_legacy_status_route_can_switch_ingestion_implementation_without_contract_change(monkeypatch, shared_enabled):
    from flask_app import server

    environment = {"FEATURE_SHARED_INGESTION_SERVICE": "true"} if shared_enabled else {}
    monkeypatch.setattr(server, "CAPACITY_FEATURES", CapacityFeatureRegistry(environ=environment))
    monkeypatch.setattr(server, "authenticate_device_request", lambda _payload=None: (True, "swt-authenticated", 200))
    calls = []

    def shared_ingestion(payload, **kwargs):
        calls.append(("shared", dict(payload), kwargs))
        return object()

    def legacy_ingestion(payload, **kwargs):
        calls.append(("legacy", dict(payload), kwargs))
        return dict(payload)

    monkeypatch.setattr(server, "ingest_device_sync", shared_ingestion)
    monkeypatch.setattr(server, "process_telemetry_payload", legacy_ingestion)

    response = server.app.test_client().post(
        "/status",
        json={"device_id": "swt-authenticated", "level": 50},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == "saved"
    assert len(calls) == 1
    assert calls[0][0] == ("shared" if shared_enabled else "legacy")
    assert calls[0][1]["device_id"] == "swt-authenticated"
    assert calls[0][2]["transport"] == "http"
    assert calls[0][2]["defer_postprocess"] is True
