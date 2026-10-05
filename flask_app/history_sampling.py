"""Thin routine history only when a separate live-state store is enabled."""
from datetime import datetime


HISTORY_TRANSITION_FIELDS = (
    "motor", "mode", "sensor", "lower_sensor", "wifi", "pipe_leak", "slow_leak",
    "drip", "leak", "dry_run", "pump_failure", "abnormal", "controller_state",
    "upper_high_float_active", "source_low_float_active", "physical_pump_running",
    "starter_contactor_active", "motor_current_detected", "water_flow_detected",
    "water_pressure_detected", "municipal_sensor_state", "firmware_version",
    "pump_runtime_boot_id", "pump_cycle_count", "reset_reason",
)


def legacy_history_sample_due(previous, payload, received_at, interval=30, level_delta=0.25):
    if not previous or interval <= 0:
        return True
    for field in HISTORY_TRANSITION_FIELDS:
        left, right = previous.get(field), payload.get(field)
        # MySQL stores boolean flags as integers; accept equivalent values.
        if isinstance(right, bool):
            if left is None or int(left) != int(right):
                return True
        elif str(left or "").strip().upper() != str(right or "").strip().upper():
            return True
    for field in ("level", "lower_tank_level"):
        left, right = previous.get(field), payload.get(field)
        if (left is None) != (right is None):
            return True
        try:
            if abs(float(left) - float(right)) >= level_delta:
                return True
        except (TypeError, ValueError):
            if left != right:
                return True
    try:
        previous_time = previous["created_at"]
        if not isinstance(previous_time, datetime):
            previous_time = datetime.strptime(str(previous_time), "%Y-%m-%d %H:%M:%S")
        now = datetime.strptime(received_at, "%Y-%m-%d %H:%M:%S")
        elapsed = (now - previous_time).total_seconds()
        return elapsed < 0 or elapsed >= interval
    except (KeyError, TypeError, ValueError):
        return True
