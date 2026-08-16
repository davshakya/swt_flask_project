from __future__ import annotations

import hashlib


VALID_ROLLOUT_STAGES = {"disabled", "internal", "pilot", "percentage", "general"}


def _csv_values(value):
    return {item.strip() for item in str(value or "").split(",") if item.strip()}


def _bounded_percentage(value):
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def stable_rollout_bucket(device_id):
    digest = hashlib.sha256(str(device_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % 100


def evaluate_device_rollout(device_id, environ):
    """Return a deterministic, advisory rollout decision for one device."""
    normalized_id = str(device_id or "").strip()
    configured_stage = str(environ.get("CAPACITY_ROLLOUT_STAGE") or "disabled").strip().lower()
    stage = configured_stage if configured_stage in VALID_ROLLOUT_STAGES else "disabled"
    percentage = _bounded_percentage(environ.get("CAPACITY_ROLLOUT_PERCENTAGE", 0))
    allowlist = _csv_values(environ.get("CAPACITY_ROLLOUT_DEVICE_ALLOWLIST"))
    denylist = _csv_values(environ.get("CAPACITY_ROLLOUT_DEVICE_DENYLIST"))
    bucket = stable_rollout_bucket(normalized_id) if normalized_id else None

    eligible = False
    reason = "disabled"
    if normalized_id in denylist:
        reason = "denylist"
    elif normalized_id in allowlist:
        eligible = True
        reason = "allowlist"
    elif stage == "general":
        eligible = True
        reason = "general"
    elif stage == "percentage" and bucket is not None and bucket < percentage:
        eligible = True
        reason = "percentage"
    elif stage in {"internal", "pilot", "percentage"}:
        reason = "outside_cohort"

    return {
        "eligible": eligible,
        "stage": stage,
        "reason": reason,
        "cohort_percentage": percentage,
        "cohort_bucket": bucket,
        "target_firmware_version": str(
            environ.get("CAPACITY_ROLLOUT_TARGET_FIRMWARE_VERSION") or ""
        ).strip()
        or None,
        "configuration_version": str(
            environ.get("CAPACITY_ROLLOUT_CONFIGURATION_VERSION") or ""
        ).strip()
        or None,
    }
