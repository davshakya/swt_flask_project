from __future__ import annotations

from types import SimpleNamespace

from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app.capacity_rollout import evaluate_device_rollout, stable_rollout_bucket
from flask_app import server


def test_rollout_cohorts_are_stable_bounded_and_denylist_wins():
    device_id = "swt-rollout-001"
    bucket = stable_rollout_bucket(device_id)
    assert 0 <= bucket < 100
    assert stable_rollout_bucket(device_id) == bucket

    allowed = evaluate_device_rollout(
        device_id,
        {
            "CAPACITY_ROLLOUT_STAGE": "pilot",
            "CAPACITY_ROLLOUT_DEVICE_ALLOWLIST": device_id,
            "CAPACITY_ROLLOUT_TARGET_FIRMWARE_VERSION": "26.8.1",
        },
    )
    denied = evaluate_device_rollout(
        device_id,
        {
            "CAPACITY_ROLLOUT_STAGE": "general",
            "CAPACITY_ROLLOUT_DEVICE_ALLOWLIST": device_id,
            "CAPACITY_ROLLOUT_DEVICE_DENYLIST": device_id,
        },
    )

    assert allowed["eligible"] is True
    assert allowed["reason"] == "allowlist"
    assert allowed["target_firmware_version"] == "26.8.1"
    assert denied["eligible"] is False
    assert denied["reason"] == "denylist"


def test_percentage_rollout_uses_exclusive_upper_bucket_boundary():
    device_id = "swt-rollout-boundary"
    percentage = stable_rollout_bucket(device_id)

    excluded = evaluate_device_rollout(
        device_id,
        {"CAPACITY_ROLLOUT_STAGE": "percentage", "CAPACITY_ROLLOUT_PERCENTAGE": percentage},
    )
    included = evaluate_device_rollout(
        device_id,
        {"CAPACITY_ROLLOUT_STAGE": "percentage", "CAPACITY_ROLLOUT_PERCENTAGE": percentage + 1},
    )

    assert excluded["eligible"] is False
    assert included["eligible"] is True


def test_sync_rollout_advice_is_feature_flagged(monkeypatch):
    monkeypatch.setattr(
        server,
        "CAPACITY_FEATURES",
        CapacityFeatureRegistry(
            environ={
                "FEATURE_SHARED_INGESTION_SERVICE": "true",
                "FEATURE_DEVICE_SYNC_API": "true",
                "FEATURE_STAGED_ROLLOUT": "true",
            }
        ),
    )
    monkeypatch.setattr(server, "authenticate_device_request", lambda payload=None: (True, "swt-rollout-api", None))
    monkeypatch.setattr(
        server,
        "ingest_device_sync",
        lambda *_args, **_kwargs: SimpleNamespace(duplicate=False, outcome="saved"),
    )
    monkeypatch.setenv("CAPACITY_ROLLOUT_STAGE", "pilot")
    monkeypatch.setenv("CAPACITY_ROLLOUT_DEVICE_ALLOWLIST", "swt-rollout-api")

    response = server.app.test_client().post(
        "/api/device/sync",
        json={"protocol_version": 1, "telemetry": {"device_id": "swt-rollout-api", "level": 50}},
    )

    assert response.status_code == 200
    assert response.get_json()["rollout"]["eligible"] is True


def test_rollout_feature_requires_combined_sync_api():
    registry = CapacityFeatureRegistry(environ={"FEATURE_STAGED_ROLLOUT": "true"})
    assert registry.enabled("staged_rollout") is False
