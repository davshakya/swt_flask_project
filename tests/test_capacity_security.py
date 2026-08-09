from __future__ import annotations

from types import SimpleNamespace

from flask_app import server
from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app.capacity_security import DeviceTokenBucketLimiter, sequence_status


def registry(*features):
    values = {
        "FEATURE_SHARED_INGESTION_SERVICE": "true",
        "FEATURE_DEVICE_SYNC_API": "true",
    }
    values.update({f"FEATURE_{name.upper()}": "true" for name in features})
    return CapacityFeatureRegistry(environ=values)


class DbContext:
    def __enter__(self):
        return SimpleNamespace(cursor=lambda: object())

    def __exit__(self, *_args):
        return False


def test_token_bucket_enforces_burst_and_recovers_tokens():
    limiter = DeviceTokenBucketLimiter(rate_per_second=2, burst=2)
    assert limiter.allow("swt-1", now=10)[0] is True
    assert limiter.allow("swt-1", now=10)[0] is True
    allowed, retry_after = limiter.allow("swt-1", now=10)
    assert allowed is False
    assert retry_after == 0.5
    assert limiter.allow("swt-1", now=10.5)[0] is True


def test_sequence_status_detects_duplicate_and_replay():
    class Cursor:
        def execute(self, *_args):
            return self

        def fetchone(self):
            return {"boot_id": "boot-1", "sequence_number": 10}

    assert sequence_status(Cursor(), "swt-1", "real", {"boot_id": "boot-1", "sequence_number": 10}) == "duplicate"
    assert sequence_status(Cursor(), "swt-1", "real", {"boot_id": "boot-1", "sequence_number": 9}) == "replay"
    assert sequence_status(Cursor(), "swt-1", "real", {"boot_id": "boot-2", "sequence_number": 1}) == "new"


def test_sync_rejects_oversized_payload_when_limit_enabled(monkeypatch):
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry("device_request_limits"))
    response = server.app.test_client().post(
        "/api/device/sync",
        json={"protocol_version": 1, "telemetry": {"padding": "x" * 9000}},
    )
    assert response.status_code == 413


def test_sync_rejects_replayed_sequence(monkeypatch):
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry("sequence_deduplication", "replay_protection"))
    monkeypatch.setattr(server, "authenticate_device_request", lambda _payload=None: (True, "swt-1", None))
    monkeypatch.setattr(server, "resolve_request_device_source", lambda _payload: "real")
    monkeypatch.setattr(server, "get_db", lambda: DbContext())
    monkeypatch.setattr(server, "sequence_status", lambda *_args: "replay")

    response = server.app.test_client().post(
        "/api/device/sync",
        json={
            "protocol_version": 1,
            "boot_id": "boot-1",
            "sequence_number": 9,
            "telemetry": {"level": 50},
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"] == "replayed sequence_number"


def test_sync_rate_limit_returns_retry_after(monkeypatch):
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry("device_rate_limiting"))
    monkeypatch.setattr(server, "authenticate_device_request", lambda _payload=None: (True, "swt-1", None))
    monkeypatch.setattr(server.DEVICE_SYNC_RATE_LIMITER, "allow", lambda _device: (False, 1.2))
    response = server.app.test_client().post(
        "/api/device/sync",
        json={"protocol_version": 1, "telemetry": {"level": 50}},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"
