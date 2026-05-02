"""Response payload builders for optional tank-level forecasting."""

from pathlib import Path


def _rounded(value, digits):
    return None if value is None else round(float(value), digits)


def build_unavailable_level_forecast_payload(device_id, reason_code, message, artifact_path, remediation=None):
    return {
        "device_id": device_id,
        "available": False,
        "status": "unavailable",
        "reason_code": reason_code,
        "error": message,
        "remediation": remediation or "",
        "current_level_percent": None,
        "predicted_level_percent": None,
        "predicted_delta_percent": None,
        "predicted_remaining_liters": None,
        "raw_predicted_level_percent": None,
        "prediction_clamped": False,
        "rows_considered": 0,
        "model": {
            "artifact_path": str(artifact_path).replace("\\", "/"),
            "artifact_updated_at": None,
            "family": None,
            "target": None,
            "horizon_hours": None,
            "resample_minutes": None,
            "device_id_filter": None,
            "train_rows": None,
            "test_rows": None,
            "metrics": {},
        },
    }


def build_level_forecast_payload(
    normalized_device_id,
    prediction,
    artifact,
    artifact_path,
    artifact_updated_at,
    project_root,
    format_timestamp,
):
    metrics = {}
    for key, value in dict(artifact.get("metrics") or {}).items():
        try:
            metrics[key] = round(float(value), 4)
        except (TypeError, ValueError):
            metrics[key] = value

    metadata = dict(artifact.get("metadata") or {})
    try:
        artifact_label = str(Path(artifact_path).relative_to(project_root))
    except ValueError:
        artifact_label = str(artifact_path)
    artifact_label = artifact_label.replace("\\", "/")

    return {
        "device_id": prediction.get("device_id") or normalized_device_id,
        "available": True,
        "status": "ready",
        "observed_at": format_timestamp(prediction.get("observed_at")),
        "forecast_for": format_timestamp(prediction.get("forecast_for")),
        "current_level_percent": _rounded(prediction.get("current_level_percent"), 4),
        "predicted_level_percent": _rounded(prediction.get("predicted_level_percent"), 4),
        "predicted_delta_percent": _rounded(prediction.get("predicted_delta_percent"), 4),
        "predicted_remaining_liters": _rounded(prediction.get("predicted_remaining_liters"), 2),
        "raw_predicted_level_percent": _rounded(prediction.get("raw_predicted_level_percent"), 4),
        "prediction_clamped": bool(prediction.get("prediction_clamped")),
        "rows_considered": int(prediction.get("rows_considered") or 0),
        "model": {
            "artifact_path": artifact_label,
            "artifact_updated_at": format_timestamp(artifact_updated_at),
            "family": metadata.get("model_family"),
            "target": metadata.get("target"),
            "horizon_hours": metadata.get("horizon_hours"),
            "resample_minutes": metadata.get("resample_minutes"),
            "device_id_filter": metadata.get("device_id_filter"),
            "train_rows": artifact.get("train_rows"),
            "test_rows": artifact.get("test_rows"),
            "metrics": metrics,
        },
    }
