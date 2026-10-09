from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import os
import threading
import time

from flask_app.runtime_utils import env_flag


@dataclass(frozen=True)
class CapacityFeatureDefinition:
    name: str
    default: bool = False
    requires: tuple[str, ...] = ()


CAPACITY_FEATURE_DEFINITIONS = (
    CapacityFeatureDefinition("capacity_metrics"),
    CapacityFeatureDefinition("request_timing"),
    CapacityFeatureDefinition("capacity_schema"),
    CapacityFeatureDefinition("latest_state_writes", requires=("capacity_schema",)),
    CapacityFeatureDefinition("narrow_history_writes", requires=("latest_state_writes",)),
    CapacityFeatureDefinition("adaptive_history", requires=("narrow_history_writes",)),
    CapacityFeatureDefinition("device_metadata_writes", requires=("latest_state_writes",)),
    CapacityFeatureDefinition("alert_occurrences"),
    CapacityFeatureDefinition("legacy_tank_data_writes", default=True),
    CapacityFeatureDefinition("dashboard_read_latest_state", requires=("latest_state_writes",)),
    CapacityFeatureDefinition("mobile_read_latest_state", requires=("latest_state_writes",)),
    CapacityFeatureDefinition("history_read_narrow_table", requires=("narrow_history_writes",)),
    CapacityFeatureDefinition("shared_ingestion_service"),
    CapacityFeatureDefinition("device_sync_api", requires=("shared_ingestion_service",)),
    CapacityFeatureDefinition("sync_command_delivery", requires=("device_sync_api",)),
    CapacityFeatureDefinition("sync_command_ack", requires=("device_sync_api",)),
    CapacityFeatureDefinition("sync_interval_hints", requires=("device_sync_api",)),
    CapacityFeatureDefinition("sequence_deduplication"),
    CapacityFeatureDefinition("replay_protection", requires=("sequence_deduplication",)),
    CapacityFeatureDefinition("device_rate_limiting"),
    CapacityFeatureDefinition("device_request_limits"),
    CapacityFeatureDefinition("database_job_queue"),
    CapacityFeatureDefinition("cron_job_processor", requires=("database_job_queue",)),
    CapacityFeatureDefinition("cron_retention"),
    CapacityFeatureDefinition("hourly_aggregation", requires=("narrow_history_writes",)),
    CapacityFeatureDefinition("daily_aggregation", requires=("hourly_aggregation",)),
    CapacityFeatureDefinition("analytics_read_aggregates", requires=("daily_aggregation",)),
    CapacityFeatureDefinition("archival", requires=("daily_aggregation",)),
    CapacityFeatureDefinition("legacy_telemetry_archive", requires=("archival",)),
    CapacityFeatureDefinition("db_connection_pool"),
    CapacityFeatureDefinition("database_size_alerts", requires=("capacity_metrics",)),
    CapacityFeatureDefinition("cron_health", requires=("capacity_metrics",)),
    CapacityFeatureDefinition("operational_alerts", requires=("capacity_metrics",)),
    CapacityFeatureDefinition("offserver_backup"),
    CapacityFeatureDefinition("staged_rollout", requires=("device_sync_api",)),
    CapacityFeatureDefinition("hosting_migration_assessment", requires=("capacity_metrics",)),
)


class CapacityFeatureRegistry:
    """Immutable startup feature configuration with safe dependency handling."""

    def __init__(self, environ=None, definitions=CAPACITY_FEATURE_DEFINITIONS):
        self._environ = os.environ if environ is None else environ
        self._definitions = {definition.name: definition for definition in definitions}
        self._configured = {
            name: env_flag(
                f"FEATURE_{name.upper()}",
                default=definition.default or (
                    name == 'db_connection_pool' and env_flag('SWT_CPANEL_RUNTIME', environ=self._environ)
                ),
                environ=self._environ,
            )
            for name, definition in self._definitions.items()
        }
        self._effective = dict(self._configured)
        self._warnings = self._resolve_dependencies()

    def _resolve_dependencies(self):
        warnings = []
        changed = True
        while changed:
            changed = False
            for name, definition in self._definitions.items():
                if not self._effective[name]:
                    continue
                missing = tuple(
                    requirement
                    for requirement in definition.requires
                    if not self._effective.get(requirement, False)
                )
                if not missing:
                    continue
                self._effective[name] = False
                changed = True
                warnings.append(
                    f"FEATURE_{name.upper()} disabled because required feature(s) are off: "
                    + ", ".join(f"FEATURE_{item.upper()}" for item in missing)
                )
        return tuple(warnings)

    def enabled(self, name):
        if name not in self._effective:
            raise KeyError(f"Unknown capacity feature: {name}")
        return self._effective[name]

    @property
    def warnings(self):
        return self._warnings

    def snapshot(self):
        return {
            name: {
                "configured": self._configured[name],
                "enabled": self._effective[name],
                "requires": list(definition.requires),
            }
            for name, definition in sorted(self._definitions.items())
        }


class BoundedRequestMetrics:
    """Small per-process request summaries suitable for shared cPanel hosting."""

    def __init__(self, max_buckets=60, clock=None):
        self.max_buckets = max(1, int(max_buckets))
        self._clock = clock or time.time
        self._started_at = self._clock()
        self._lock = threading.Lock()
        self._buckets = OrderedDict()

    def record(self, route, status_code, elapsed_ms):
        minute = int(self._clock() // 60) * 60
        normalized_route = str(route or "unmatched")[:160]
        key = f"{normalized_route}"
        with self._lock:
            bucket = self._buckets.setdefault(minute, {})
            metric = bucket.setdefault(
                key,
                {
                    "requests": 0,
                    "errors": 0,
                    "total_duration_ms": 0.0,
                    "max_duration_ms": 0.0,
                },
            )
            duration = max(0.0, float(elapsed_ms or 0.0))
            metric["requests"] += 1
            metric["errors"] += int(int(status_code or 0) >= 400)
            metric["total_duration_ms"] += duration
            metric["max_duration_ms"] = max(metric["max_duration_ms"], duration)
            while len(self._buckets) > self.max_buckets:
                self._buckets.popitem(last=False)

    def snapshot(self):
        with self._lock:
            buckets = []
            total_requests = 0
            total_errors = 0
            for minute, routes in self._buckets.items():
                route_payload = {}
                for route, metric in routes.items():
                    requests = int(metric["requests"])
                    total_requests += requests
                    total_errors += int(metric["errors"])
                    route_payload[route] = {
                        "requests": requests,
                        "errors": int(metric["errors"]),
                        "average_duration_ms": round(metric["total_duration_ms"] / requests, 2),
                        "max_duration_ms": round(metric["max_duration_ms"], 2),
                    }
                buckets.append({"minute_epoch": minute, "routes": route_payload})
            return {
                "scope": "passenger_process",
                "retained_minutes": self.max_buckets,
                "process_uptime_seconds": max(0, int(self._clock() - self._started_at)),
                "requests": total_requests,
                "errors": total_errors,
                "buckets": buckets,
            }
