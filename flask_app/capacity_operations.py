from __future__ import annotations

import json


def record_runtime_status(cursor, status_key, status_value, details=None):
    cursor.execute(
        """
        INSERT INTO capacity_runtime_status(status_key, status_value, details_json, checked_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(status_key) DO UPDATE SET
          status_value=excluded.status_value,
          details_json=excluded.details_json,
          checked_at=CURRENT_TIMESTAMP
        """,
        (status_key, status_value, json.dumps(details or {}, separators=(",", ":"), sort_keys=True, default=str)),
    )


def fetch_runtime_status(cursor):
    rows = cursor.execute(
        "SELECT status_key, status_value, details_json, checked_at FROM capacity_runtime_status ORDER BY status_key"
    ).fetchall()
    return {
        row["status_key"]: {
            "status": row["status_value"],
            "details": json.loads(row.get("details_json") or "{}"),
            "checked_at": row.get("checked_at"),
        }
        for row in rows
    }


def mysql_database_usage(cursor, quota_mb=0):
    row = cursor.execute(
        """
        SELECT COALESCE(SUM(data_length + index_length), 0) AS total_bytes
        FROM information_schema.TABLES
        WHERE table_schema = DATABASE()
        """
    ).fetchone()
    total_bytes = int((row or {}).get("total_bytes") or 0)
    quota_bytes = max(0, int(float(quota_mb) * 1024 * 1024))
    usage_pct = round((total_bytes / quota_bytes) * 100, 2) if quota_bytes else None
    return {
        "total_bytes": total_bytes,
        "quota_bytes": quota_bytes,
        "usage_percent": usage_pct,
        "warning": usage_pct is not None and usage_pct >= 70,
    }
