#!/usr/bin/env python3
"""Run bounded capacity maintenance from cPanel Cron Jobs."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK_PATH = PROJECT_ROOT / "instance" / "capacity-retention.lock"
DEFAULT_ARCHIVE_DIR = PROJECT_ROOT / "instance" / "capacity-archive"


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-seconds", type=int, default=240)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument(
        "--task",
        choices=("retention", "hourly", "daily", "archive", "legacy-archive", "jobs", "all"),
        default="retention",
    )
    parser.add_argument("--lookback-hours", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=2)
    parser.add_argument("--archive-before-days", type=int, default=365)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument(
        "--legacy-archive-before-days",
        type=int,
        default=env_int("LEGACY_TELEMETRY_ARCHIVE_BEFORE_DAYS", 30),
    )
    parser.add_argument(
        "--legacy-archive-batch-rows",
        type=int,
        default=env_int("LEGACY_TELEMETRY_ARCHIVE_BATCH_ROWS", 1000),
    )
    parser.add_argument("--job-batch-size", type=int, default=25)
    parser.add_argument("--job-max-attempts", type=int, default=5)
    return parser.parse_args(argv)


def run(argv=None):
    args = parse_args(argv)
    started = time.monotonic()
    if args.max_seconds < 1:
        raise ValueError("--max-seconds must be positive")
    args.lock_file.parent.mkdir(parents=True, exist_ok=True)
    with args.lock_file.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"ok": True, "skipped": "already_running"}))
            return 0

        sys.path.insert(0, str(PROJECT_ROOT))
        from flask_app import server

        required_features = {
            "retention": ("cron_retention",),
            "hourly": ("hourly_aggregation",),
            "daily": ("daily_aggregation",),
            "archive": ("archival",),
            "legacy-archive": ("legacy_telemetry_archive",),
            "jobs": ("cron_job_processor",),
            "all": (
                "cron_retention",
                "hourly_aggregation",
                "daily_aggregation",
                "archival",
                "legacy_telemetry_archive",
                "cron_job_processor",
            ),
        }[args.task]
        disabled = [name for name in required_features if not server.CAPACITY_FEATURES.enabled(name)]
        if disabled:
            print(json.dumps({"ok": True, "skipped": "feature_disabled", "features": disabled}))
            return 0
        legacy_archive_allowed = not server.CAPACITY_FEATURES.enabled("legacy_tank_data_writes")
        if args.task == "legacy-archive" and not legacy_archive_allowed:
            print(json.dumps({"ok": True, "skipped": "legacy_writes_enabled"}))
            return 0

        from flask_app.capacity_aggregation import (
            aggregate_daily,
            aggregate_hourly,
            archive_daily_rows,
            archive_legacy_tank_rows,
        )

        task_results = {}
        if args.task in {"retention", "all"}:
            pruned = server.maybe_prune_retained_rows(force=True)
            task_results["retention"] = {
                "tables": pruned,
                "rows": sum(int(value or 0) for value in pruned.values()),
            }
        if args.task in {"jobs", "all"}:
            from flask_app.capacity_jobs import process_job_batch

            task_results["jobs"] = process_job_batch(
                server.get_db,
                {"alert_webhook": lambda payload: server.deliver_alert_webhook(payload, raise_on_failure=True)},
                batch_size=args.job_batch_size,
                max_attempts=args.job_max_attempts,
            )
        if args.task in {"legacy-archive", "all"} and legacy_archive_allowed:
            with server.get_db() as db:
                task_results["legacy_archive"] = archive_legacy_tank_rows(
                    db.cursor(),
                    args.archive_dir,
                    args.legacy_archive_before_days,
                    args.legacy_archive_batch_rows,
                )
        elif args.task == "all":
            task_results["legacy_archive"] = {"skipped": "legacy_writes_enabled"}
        if args.task in {"hourly", "daily", "archive", "all"}:
            with server.get_db() as db:
                cursor = db.cursor()
                if args.task in {"hourly", "all"}:
                    task_results["hourly"] = {"rows": aggregate_hourly(cursor, args.lookback_hours)}
                if args.task in {"daily", "all"}:
                    task_results["daily"] = {"rows": aggregate_daily(cursor, args.lookback_days)}
                if args.task in {"archive", "all"}:
                    task_results["archive"] = archive_daily_rows(
                        cursor, args.archive_dir, args.archive_before_days
                    )
        elapsed = round(time.monotonic() - started, 3)
        result = {
            "ok": server.db_prune_state.get("last_error") is None,
            "task": args.task,
            "elapsed_seconds": elapsed,
            "max_seconds": args.max_seconds,
            "results": task_results,
            "error": server.db_prune_state.get("last_error"),
        }
        if server.CAPACITY_FEATURES.enabled("cron_health") or server.CAPACITY_FEATURES.enabled("database_size_alerts"):
            from flask_app.capacity_operations import mysql_database_usage, record_runtime_status

            with server.get_db() as db:
                cursor = db.cursor()
                if server.CAPACITY_FEATURES.enabled("database_size_alerts"):
                    usage = mysql_database_usage(cursor, server.env_float("DATABASE_QUOTA_MB", 0))
                    task_results["database_usage"] = usage
                    record_runtime_status(
                        cursor,
                        "database_quota",
                        "warning" if usage["warning"] else "ok",
                        usage,
                    )
                if server.CAPACITY_FEATURES.enabled("cron_health"):
                    record_runtime_status(
                        cursor,
                        f"cron:{args.task}",
                        "ok" if result["ok"] and elapsed <= args.max_seconds else "failed",
                        {"elapsed_seconds": elapsed, "results": task_results, "error": result["error"]},
                    )
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] and elapsed <= args.max_seconds else 1


if __name__ == "__main__":
    raise SystemExit(run())
