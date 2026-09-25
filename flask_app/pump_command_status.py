"""Correlate firmware acknowledgements and telemetry with one pump request."""

from datetime import datetime, timezone


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _state(value):
    text = str(value).strip().upper()
    if text in {"ON", "RUNNING", "TRUE", "1"}:
        return "ON"
    if text in {"OFF", "STOPPED", "FALSE", "0"}:
        return "OFF"
    return None


def pump_confirmation(command, snapshot):
    """Use server timestamps, never the phone clock, to reject pre-command data."""
    status = str(command.get("status") or "").lower()
    desired = _state(command.get("command"))
    snapshot = snapshot or {}
    observed = None
    observed_at = None
    ack = command.get("device_result") or {}
    if not isinstance(ack, dict):
        ack = {}
    if status in {"accepted", "running", "stopped"}:
        observed = _state(ack.get("motor_state"))
        observed_at = _time(command.get("completed_at") or command.get("accepted_at"))

    sample_at = _time(snapshot.get("last_sync_at"))
    queued_at = _time(command.get("queued_at"))
    if sample_at and queued_at and sample_at > queued_at and (not observed_at or sample_at >= observed_at):
        physical = str(snapshot.get("pump_state_confirmed")).lower() in {"true", "1", "on"}
        sample_state = _state(snapshot.get("physical_pump_running")) if physical else _state(snapshot.get("motor"))
        if sample_state is not None:
            observed, observed_at = sample_state, sample_at

    return {
        "pump_state": observed,
        "pump_observed_at": observed_at.isoformat() if observed_at else None,
        # Queueing/HTTP acceptance alone can never confirm a motor transition.
        "pump_confirmed": status in {"delivered", "accepted", "running", "stopped"}
        and desired is not None and observed == desired,
    }
