from __future__ import annotations

from datetime import date, datetime, timezone
import gzip
import json
from pathlib import Path
import tempfile


HOURLY_UPSERT = """
INSERT INTO tank_telemetry_hourly(
    device_id, device_source, bucket_at, sample_count,
    minimum_level, maximum_level, average_level,
    minimum_lower_level, maximum_lower_level, average_lower_level,
    alert_count, updated_at
)
SELECT device_id, device_source,
       DATE_FORMAT(recorded_at, '%%Y-%%m-%%d %%H:00:00') AS bucket_at,
       COUNT(*), MIN(level), MAX(level), AVG(level),
       MIN(lower_tank_level), MAX(lower_tank_level), AVG(lower_tank_level),
       SUM(CASE WHEN alert_flags <> 0 THEN 1 ELSE 0 END), CURRENT_TIMESTAMP
FROM tank_telemetry_history
WHERE recorded_at >= DATE_SUB(CURRENT_TIMESTAMP, INTERVAL ? HOUR)
  AND recorded_at < DATE_FORMAT(CURRENT_TIMESTAMP, '%%Y-%%m-%%d %%H:00:00')
GROUP BY device_id, device_source, DATE_FORMAT(recorded_at, '%%Y-%%m-%%d %%H:00:00')
ON DUPLICATE KEY UPDATE
 sample_count=VALUES(sample_count), minimum_level=VALUES(minimum_level),
 maximum_level=VALUES(maximum_level), average_level=VALUES(average_level),
 minimum_lower_level=VALUES(minimum_lower_level),
 maximum_lower_level=VALUES(maximum_lower_level),
 average_lower_level=VALUES(average_lower_level), alert_count=VALUES(alert_count),
 updated_at=CURRENT_TIMESTAMP
"""

DAILY_UPSERT = """
INSERT INTO tank_telemetry_daily(
    device_id, device_source, bucket_date, sample_count,
    minimum_level, maximum_level, average_level,
    minimum_lower_level, maximum_lower_level, average_lower_level,
    pump_runtime_seconds, pump_cycle_count, alert_count, missing_data_seconds, updated_at
)
SELECT device_id, device_source, DATE(bucket_at), SUM(sample_count),
       MIN(minimum_level), MAX(maximum_level),
       SUM(average_level * sample_count) / NULLIF(SUM(sample_count), 0),
       MIN(minimum_lower_level), MAX(maximum_lower_level),
       SUM(average_lower_level * sample_count) / NULLIF(SUM(sample_count), 0),
       SUM(pump_runtime_seconds), SUM(pump_cycle_count), SUM(alert_count),
       SUM(missing_data_seconds), CURRENT_TIMESTAMP
FROM tank_telemetry_hourly
WHERE bucket_at >= DATE_SUB(CURRENT_DATE, INTERVAL ? DAY)
  AND bucket_at < CURRENT_DATE
GROUP BY device_id, device_source, DATE(bucket_at)
ON DUPLICATE KEY UPDATE
 sample_count=VALUES(sample_count), minimum_level=VALUES(minimum_level),
 maximum_level=VALUES(maximum_level), average_level=VALUES(average_level),
 minimum_lower_level=VALUES(minimum_lower_level),
 maximum_lower_level=VALUES(maximum_lower_level),
 average_lower_level=VALUES(average_lower_level),
 pump_runtime_seconds=VALUES(pump_runtime_seconds), pump_cycle_count=VALUES(pump_cycle_count),
 alert_count=VALUES(alert_count), missing_data_seconds=VALUES(missing_data_seconds),
 updated_at=CURRENT_TIMESTAMP
"""


def aggregate_hourly(cursor, lookback_hours=48):
    cursor.execute(HOURLY_UPSERT, (max(1, min(int(lookback_hours), 168)),))
    return max(0, int(cursor.rowcount or 0))


def aggregate_daily(cursor, lookback_days=14):
    cursor.execute(DAILY_UPSERT, (max(1, min(int(lookback_days), 90)),))
    return max(0, int(cursor.rowcount or 0))


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def archive_daily_rows(cursor, archive_dir, before_days=365, batch_rows=5000):
    """Copy a bounded batch of old daily aggregates to a compressed JSONL file."""
    archive_path = Path(archive_dir)
    archive_path.mkdir(parents=True, exist_ok=True)
    checkpoint_path = archive_path / ".daily-checkpoint.json"
    checkpoint = {"bucket_date": "1900-01-01", "device_id": "", "device_source": ""}
    if checkpoint_path.exists():
        checkpoint.update(json.loads(checkpoint_path.read_text(encoding="utf-8")))
    cursor.execute(
        """
        SELECT * FROM tank_telemetry_daily
        WHERE bucket_date < DATE_SUB(CURRENT_DATE, INTERVAL ? DAY)
          AND (
            bucket_date > ?
            OR (bucket_date = ? AND device_id > ?)
            OR (bucket_date = ? AND device_id = ? AND device_source > ?)
          )
        ORDER BY bucket_date, device_id, device_source
        LIMIT ?
        """,
        (
            max(30, min(int(before_days), 3650)),
            checkpoint["bucket_date"],
            checkpoint["bucket_date"],
            checkpoint["device_id"],
            checkpoint["bucket_date"],
            checkpoint["device_id"],
            checkpoint["device_source"],
            max(1, min(int(batch_rows), 5000)),
        ),
    )
    rows = list(cursor.fetchall())
    if not rows:
        return {"rows": 0, "path": None}

    first_bucket = str(_json_value(rows[0]["bucket_date"])).replace(":", "-")
    last_bucket = str(_json_value(rows[-1]["bucket_date"])).replace(":", "-")
    final_path = archive_path / f"daily-{first_bucket}-{last_bucket}-{int(datetime.now(timezone.utc).timestamp())}.jsonl.gz"
    with tempfile.NamedTemporaryFile(dir=archive_path, suffix=".tmp", delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        with gzip.open(temporary_path, "wt", encoding="utf-8") as archive:
            for row in rows:
                archive.write(json.dumps({key: _json_value(value) for key, value in row.items()}, sort_keys=True))
                archive.write("\n")
        temporary_path.replace(final_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    last_row = rows[-1]
    checkpoint_payload = {
        "bucket_date": str(_json_value(last_row["bucket_date"])),
        "device_id": str(last_row["device_id"]),
        "device_source": str(last_row["device_source"]),
    }
    with tempfile.NamedTemporaryFile(
        dir=archive_path, mode="w", encoding="utf-8", suffix=".checkpoint.tmp", delete=False
    ) as handle:
        json.dump(checkpoint_payload, handle, sort_keys=True)
        checkpoint_temp = Path(handle.name)
    checkpoint_temp.replace(checkpoint_path)
    return {"rows": len(rows), "path": str(final_path)}


def archive_legacy_tank_rows(cursor, archive_dir, before_days=30, batch_rows=1000):
    """Archive then delete one bounded batch from the legacy wide telemetry table."""
    archive_path = Path(archive_dir)
    archive_path.mkdir(parents=True, exist_ok=True)
    cursor.execute(
        """
        SELECT * FROM tank_data
        WHERE created_at < DATE_SUB(CURRENT_TIMESTAMP, INTERVAL ? DAY)
        ORDER BY id
        LIMIT ?
        """,
        (
            max(1, min(int(before_days), 3650)),
            max(1, min(int(batch_rows), 1000)),
        ),
    )
    rows = list(cursor.fetchall())
    if not rows:
        return {"rows": 0, "deleted_rows": 0, "path": None}

    first_id = int(rows[0]["id"])
    last_id = int(rows[-1]["id"])
    final_path = archive_path / (
        f"legacy-tank-data-{first_id}-{last_id}-{int(datetime.now(timezone.utc).timestamp())}.jsonl.gz"
    )
    with tempfile.NamedTemporaryFile(dir=archive_path, suffix=".tmp", delete=False) as handle:
        temporary_path = Path(handle.name)
    try:
        with gzip.open(temporary_path, "wt", encoding="utf-8") as archive:
            for row in rows:
                archive.write(
                    json.dumps({key: _json_value(value) for key, value in row.items()}, sort_keys=True)
                )
                archive.write("\n")
        temporary_path.replace(final_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _item in ids)
    cursor.execute(f"DELETE FROM tank_data WHERE id IN ({placeholders})", tuple(ids))
    deleted_rows = max(0, int(cursor.rowcount or 0))
    if deleted_rows != len(ids):
        raise RuntimeError(
            f"Legacy archive deletion mismatch: archived={len(ids)} deleted={deleted_rows}"
        )
    return {"rows": len(rows), "deleted_rows": deleted_rows, "path": str(final_path)}
