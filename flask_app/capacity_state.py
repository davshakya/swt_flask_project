from __future__ import annotations

from datetime import datetime, timezone
import json


ALERT_FLAG_FIELDS = (
    ("leak", 1 << 0),
    ("pump_failure", 1 << 1),
    ("dry_run", 1 << 2),
    ("abnormal", 1 << 3),
    ("upper_high_float_active", 1 << 4),
    ("source_low_float_active", 1 << 5),
)


def _enabled(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().upper() in {"1", "TRUE", "YES", "ON", "ACTIVE", "FAULT"}


def build_alert_flags(payload):
    explicit = payload.get("alert_flags")
    if explicit not in (None, ""):
        try:
            return max(0, int(explicit))
        except (TypeError, ValueError):
            pass
    flags = 0
    for field, mask in ALERT_FLAG_FIELDS:
        if _enabled(payload.get(field)):
            flags |= mask
    return flags


def normalize_sequence_number(payload):
    value = payload.get("sequence_number", payload.get("sequence"))
    if value in (None, ""):
        return None
    try:
        sequence = int(value)
    except (TypeError, ValueError):
        return None
    return sequence if sequence >= 0 else None


def normalize_device_reported_at(value):
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def upsert_device_latest_state(cursor, payload, received_at):
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        raise ValueError("device_id is required for latest-state writes")
    device_source = str(payload.get("device_source") or "real").strip().lower() or "real"
    boot_id = str(payload.get("boot_id") or payload.get("pump_runtime_boot_id") or "").strip() or None
    sequence_number = normalize_sequence_number(payload)

    existing = cursor.execute(
        """
        SELECT boot_id, sequence_number
        FROM device_latest_state
        WHERE device_id = ? AND device_source = ?
        """,
        (device_id, device_source),
    ).fetchone()
    if (
        existing
        and boot_id
        and existing.get("boot_id") == boot_id
        and sequence_number is not None
        and existing.get("sequence_number") is not None
        and sequence_number <= int(existing["sequence_number"])
    ):
        return "stale"

    state_json = json.dumps(
        {key: value for key, value in payload.items() if not str(key).startswith("_")},
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    cursor.execute(
        """
        INSERT INTO device_latest_state(
            device_id, device_source, boot_id, sequence_number,
            level, lower_tank_level, motor, mode, sensor, alert_flags,
            firmware_version, device_reported_at, received_at, state_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id, device_source) DO UPDATE SET
            boot_id=excluded.boot_id,
            sequence_number=excluded.sequence_number,
            level=excluded.level,
            lower_tank_level=excluded.lower_tank_level,
            motor=excluded.motor,
            mode=excluded.mode,
            sensor=excluded.sensor,
            alert_flags=excluded.alert_flags,
            firmware_version=excluded.firmware_version,
            device_reported_at=excluded.device_reported_at,
            received_at=excluded.received_at,
            state_json=excluded.state_json
        """,
        (
            device_id,
            device_source,
            boot_id,
            sequence_number,
            payload.get("level"),
            payload.get("lower_tank_level"),
            payload.get("motor"),
            payload.get("mode"),
            payload.get("sensor"),
            build_alert_flags(payload),
            payload.get("firmware_version"),
            normalize_device_reported_at(payload.get("device_reported_at")),
            received_at,
            state_json,
        ),
    )
    return "updated" if existing else "inserted"
