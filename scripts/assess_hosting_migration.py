#!/usr/bin/env python3
"""Read-only cPanel hosting migration assessment for cron or manual use."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def env_optional_float(name):
    value = str(os.environ.get(name) or "").strip()
    return float(value) if value else None


def env_int(name, default=0):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def env_bool(name, default=None):
    value = str(os.environ.get(name) or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu-percent", type=float, default=env_optional_float("HOST_CPU_PERCENT"))
    parser.add_argument("--ram-percent", type=float, default=env_optional_float("HOST_RAM_PERCENT"))
    parser.add_argument("--p95-sync-ms", type=float, default=env_optional_float("HOST_P95_SYNC_MS"))
    parser.add_argument("--api-error-percent", type=float, default=env_optional_float("HOST_API_ERROR_PERCENT"))
    parser.add_argument("--persistent-samples", type=int, default=env_int("HOST_THRESHOLD_PERSISTENT_SAMPLES", 3))
    parser.add_argument("--breach-samples", type=int, default=env_int("HOST_THRESHOLD_BREACH_SAMPLES", 0))
    return parser.parse_args(argv)


def run(argv=None):
    args = parse_args(argv)
    sys.path.insert(0, str(PROJECT_ROOT))
    from flask_app import server

    if not server.CAPACITY_FEATURES.enabled("hosting_migration_assessment"):
        print(json.dumps({"ok": True, "skipped": "feature_disabled"}))
        return 0

    from flask_app.capacity_migration import assess_hosting_migration
    from flask_app.capacity_operations import mysql_database_usage

    with server.get_db() as db:
        database = mysql_database_usage(db.cursor(), server.env_float("DATABASE_QUOTA_MB", 0))
    metrics = {
        "cpu_percent": args.cpu_percent,
        "ram_percent": args.ram_percent,
        "database_percent": database["usage_percent"],
        "p95_sync_ms": args.p95_sync_ms,
        "api_error_percent": args.api_error_percent,
        "mysql_connection_limit_errors": env_int("HOST_MYSQL_CONNECTION_LIMIT_ERRORS", 0),
        "passenger_worker_restarts_frequent": env_bool("HOST_PASSENGER_RESTARTS_FREQUENT", False),
        "cron_unreliable": env_bool("HOST_CRON_UNRELIABLE", False),
        "load_test_500_passed": env_bool("HOST_LOAD_TEST_500_PASSED", None),
        "host_throttling": env_bool("HOST_THROTTLING_DETECTED", False),
        "uptime_guarantee_required": env_bool("HOST_UPTIME_GUARANTEE_REQUIRED", False),
    }
    for metric_name in (
        "cpu_percent",
        "ram_percent",
        "database_percent",
        "p95_sync_ms",
        "api_error_percent",
    ):
        metrics[f"{metric_name}_breach_samples"] = args.breach_samples
    assessment = assess_hosting_migration(metrics, args.persistent_samples)
    print(json.dumps({"ok": True, "database": database, **assessment}, sort_keys=True))
    return 2 if assessment["migration_required"] else 0


if __name__ == "__main__":
    raise SystemExit(run())
