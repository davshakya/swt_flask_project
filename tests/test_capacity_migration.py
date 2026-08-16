from __future__ import annotations

from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app.capacity_migration import assess_hosting_migration


def healthy_metrics():
    return {
        "cpu_percent": 50,
        "ram_percent": 60,
        "database_percent": 40,
        "p95_sync_ms": 400,
        "api_error_percent": 0.1,
        "load_test_500_passed": True,
    }


def test_healthy_complete_measurements_can_remain_on_current_host():
    assessment = assess_hosting_migration(healthy_metrics())
    assert assessment["decision"] == "remain"
    assert assessment["migration_required"] is False
    assert assessment["unknown_metrics"] == []


def test_numeric_threshold_requires_persistent_breach():
    metrics = {**healthy_metrics(), "cpu_percent": 80, "cpu_percent_breach_samples": 2}
    assert assess_hosting_migration(metrics)["migration_required"] is False

    metrics["cpu_percent_breach_samples"] = 3
    assessment = assess_hosting_migration(metrics)
    assert assessment["migration_required"] is True
    assert "cpu_percent" in assessment["active_triggers"]


def test_hard_operational_trigger_requires_immediate_migration():
    assessment = assess_hosting_migration(
        {**healthy_metrics(), "mysql_connection_limit_errors": 1}
    )
    assert assessment["decision"] == "migrate"
    assert "mysql_connection_limit_errors" in assessment["active_triggers"]


def test_missing_measurements_request_measurement_instead_of_false_assurance():
    assessment = assess_hosting_migration({})
    assert assessment["decision"] == "measure"
    assert "cpu_percent" in assessment["unknown_metrics"]


def test_migration_assessment_is_default_off_and_requires_metrics():
    disabled = CapacityFeatureRegistry(environ={"FEATURE_HOSTING_MIGRATION_ASSESSMENT": "true"})
    enabled = CapacityFeatureRegistry(
        environ={
            "FEATURE_CAPACITY_METRICS": "true",
            "FEATURE_HOSTING_MIGRATION_ASSESSMENT": "true",
        }
    )
    assert disabled.enabled("hosting_migration_assessment") is False
    assert enabled.enabled("hosting_migration_assessment") is True
