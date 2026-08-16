from __future__ import annotations


MIGRATION_THRESHOLDS = {
    "cpu_percent": 75.0,
    "ram_percent": 75.0,
    "database_percent": 70.0,
    "p95_sync_ms": 1000.0,
    "api_error_percent": 0.5,
}


def assess_hosting_migration(metrics, persistent_samples=3):
    """Evaluate measured cPanel limits without performing a migration."""
    required_samples = max(1, int(persistent_samples))
    triggers = []
    unknown = []
    for metric_name, threshold in MIGRATION_THRESHOLDS.items():
        value = metrics.get(metric_name)
        if value is None:
            unknown.append(metric_name)
            continue
        samples = max(0, int(metrics.get(f"{metric_name}_breach_samples") or 0))
        breached = float(value) > threshold
        persistent = breached and samples >= required_samples
        triggers.append(
            {
                "name": metric_name,
                "value": float(value),
                "threshold": threshold,
                "breach_samples": samples,
                "persistent": persistent,
            }
        )

    event_triggers = {
        "mysql_connection_limit_errors": int(metrics.get("mysql_connection_limit_errors") or 0) > 0,
        "passenger_worker_restarts_frequent": bool(metrics.get("passenger_worker_restarts_frequent")),
        "cron_unreliable": bool(metrics.get("cron_unreliable")),
        "load_test_500_failed": metrics.get("load_test_500_passed") is False,
        "host_throttling": bool(metrics.get("host_throttling")),
        "uptime_guarantee_required": bool(metrics.get("uptime_guarantee_required")),
    }
    for name, active in event_triggers.items():
        triggers.append({"name": name, "persistent": active})

    active = [trigger["name"] for trigger in triggers if trigger["persistent"]]
    return {
        "migration_required": bool(active),
        "decision": "migrate" if active else ("measure" if unknown else "remain"),
        "active_triggers": active,
        "unknown_metrics": unknown,
        "persistent_samples_required": required_samples,
        "triggers": triggers,
    }
