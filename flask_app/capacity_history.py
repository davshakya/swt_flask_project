from __future__ import annotations

from datetime import datetime

from flask_app.capacity_state import build_alert_flags, normalize_sequence_number


TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _changed(previous, current):
    return str(previous or "").strip().upper() != str(current or "").strip().upper()


def choose_history_sample_reason(
    previous,
    payload,
    received_at,
    *,
    adaptive=True,
    idle_interval_seconds=300,
    active_interval_seconds=60,
    level_delta_pct=2.0,
):
    if not adaptive:
        return "every_report"
    if not previous:
        return "first_sample"
    comparisons = (
        ("motor_change", "last_motor", "motor"),
        ("mode_change", "last_mode", "mode"),
        ("sensor_change", "last_sensor", "sensor"),
    )
    for reason, previous_key, current_key in comparisons:
        if _changed(previous.get(previous_key), payload.get(current_key)):
            return reason
    if int(previous.get("last_alert_flags") or 0) != build_alert_flags(payload):
        return "alert_change"
    try:
        if abs(float(payload.get("level")) - float(previous.get("last_level"))) >= float(level_delta_pct):
            return "level_change"
    except (TypeError, ValueError):
        pass
    last_history_at = previous.get("last_history_at")
    if not last_history_at:
        return "first_sample"
    if isinstance(last_history_at, datetime):
        previous_time = last_history_at
    else:
        previous_time = datetime.strptime(str(last_history_at), TIMESTAMP_FORMAT)
    current_time = datetime.strptime(str(received_at), TIMESTAMP_FORMAT)
    active = str(payload.get("motor") or "").strip().upper() == "ON"
    interval = active_interval_seconds if active else idle_interval_seconds
    if (current_time - previous_time).total_seconds() >= max(1, int(interval)):
        return "active_interval" if active else "idle_interval"
    return None


def store_narrow_history_if_due(
    cursor,
    payload,
    received_at,
    *,
    adaptive=True,
    idle_interval_seconds=300,
    active_interval_seconds=60,
    level_delta_pct=2.0,
):
    device_id = str(payload.get("device_id") or "").strip()
    if not device_id:
        raise ValueError("device_id is required for narrow-history writes")
    device_source = str(payload.get("device_source") or "real").strip().lower() or "real"
    previous = cursor.execute(
        """
        SELECT last_history_at, last_level, last_lower_tank_level,
               last_motor, last_mode, last_sensor, last_alert_flags
        FROM telemetry_sampling_state
        WHERE device_id = ? AND device_source = ?
        """,
        (device_id, device_source),
    ).fetchone()
    reason = choose_history_sample_reason(
        previous,
        payload,
        received_at,
        adaptive=adaptive,
        idle_interval_seconds=idle_interval_seconds,
        active_interval_seconds=active_interval_seconds,
        level_delta_pct=level_delta_pct,
    )
    if reason is None:
        return "skipped"

    boot_id = str(payload.get("boot_id") or payload.get("pump_runtime_boot_id") or "").strip() or None
    sequence_number = normalize_sequence_number(payload)
    alert_flags = build_alert_flags(payload)
    cursor.execute(
        """
        INSERT INTO tank_telemetry_history(
            device_id, device_source, boot_id, sequence_number, level,
            lower_tank_level, motor, mode, sensor, alert_flags,
            sample_reason, recorded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id, device_source, boot_id, sequence_number) DO NOTHING
        """,
        (
            device_id, device_source, boot_id, sequence_number, payload.get("level"),
            payload.get("lower_tank_level"), payload.get("motor"), payload.get("mode"),
            payload.get("sensor"), alert_flags, reason, received_at,
        ),
    )
    cursor.execute(
        """
        INSERT INTO telemetry_sampling_state(
            device_id, device_source, last_history_at, last_level,
            last_lower_tank_level, last_motor, last_mode, last_sensor,
            last_alert_flags, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id, device_source) DO UPDATE SET
            last_history_at=excluded.last_history_at,
            last_level=excluded.last_level,
            last_lower_tank_level=excluded.last_lower_tank_level,
            last_motor=excluded.last_motor,
            last_mode=excluded.last_mode,
            last_sensor=excluded.last_sensor,
            last_alert_flags=excluded.last_alert_flags,
            updated_at=excluded.updated_at
        """,
        (
            device_id, device_source, received_at, payload.get("level"),
            payload.get("lower_tank_level"), payload.get("motor"), payload.get("mode"),
            payload.get("sensor"), alert_flags, received_at,
        ),
    )
    return reason
