"""Shared helpers for tank level forecasting training and inference."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

SUPPORTED_RESAMPLE_MINUTES = {5, 10, 15, 30, 60}
SELECT_COLUMNS = [
    "created_at",
    "device_id",
    "level",
    "motor",
    "mode",
    "tank_health",
    "ai_usage_rate",
    "lower_tank_level",
    "wifi_rssi",
    "tank_capacity_liters",
    "pipe_leak",
    "slow_leak",
    "drip",
    "abnormal",
    "dry_run",
]
FEATURE_COLUMNS = [
    "level",
    "tank_health",
    "ai_usage_rate",
    "lower_tank_level",
    "wifi_rssi",
    "tank_capacity_liters",
    "motor_on",
    "mode_auto",
    "alert_flag",
    "level_delta_1",
    "level_delta_4",
    "usage_roll_mean_4",
    "usage_roll_mean_24",
    "level_roll_mean_4",
    "level_roll_mean_24",
    "pump_on_roll_mean_4",
    "pump_on_roll_mean_24",
    "level_lag_1",
    "level_lag_2",
    "level_lag_4",
    "level_lag_8",
    "level_lag_24",
    "tank_health_lag_1",
    "tank_health_lag_4",
    "tank_health_lag_24",
    "usage_lag_1",
    "usage_lag_4",
    "usage_lag_24",
    "motor_on_lag_1",
    "motor_on_lag_4",
    "motor_on_lag_24",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]


def validate_forecast_args(resample_minutes: int, horizon_hours: int, min_samples: int | None = None) -> None:
    if resample_minutes not in SUPPORTED_RESAMPLE_MINUTES:
        raise ValueError(
            f"--resample-minutes must be one of {sorted(SUPPORTED_RESAMPLE_MINUTES)}, got {resample_minutes}",
        )
    if horizon_hours <= 0:
        raise ValueError("--horizon-hours must be greater than zero")
    if min_samples is not None and min_samples < 50:
        raise ValueError("--min-samples must be at least 50")


def _query_forecast_rows(connection, device_id: str = "", device_source: str | None = None) -> pd.DataFrame:
    clauses: list[str] = []
    params: list[str] = []
    if device_id.strip():
        clauses.append("COALESCE(device_id, '') = ?")
        params.append(device_id.strip())
    if device_source:
        clauses.append("COALESCE(device_source, 'real') = ?")
        params.append(str(device_source).strip().lower())

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    query = f"""
        SELECT {", ".join(SELECT_COLUMNS)}
        FROM tank_data
        {where}
        ORDER BY created_at ASC
    """
    if hasattr(connection, "execute"):
        rows = connection.execute(query, tuple(params)).fetchall()
    else:
        with connection.cursor() as cursor:
            cursor.execute(query.replace("?", "%s"), tuple(params))
            rows = cursor.fetchall()
    return pd.DataFrame([dict(row) for row in rows], columns=SELECT_COLUMNS)


def query_training_rows(connection, device_id: str, device_source: str | None = None) -> pd.DataFrame:
    return _query_forecast_rows(connection, device_id=device_id, device_source=device_source)


def query_device_forecast_rows(
    connection,
    device_id: str,
    device_source: str | None = None,
) -> pd.DataFrame:
    normalized_device_id = str(device_id or "").strip()
    if not normalized_device_id:
        raise ValueError("device_id is required for forecast inference")

    return _query_forecast_rows(connection, device_id=normalized_device_id, device_source=device_source)


def normalize_boolean_flag(series: pd.Series, truthy: Iterable[str]) -> pd.Series:
    normalized = series.fillna("").astype(str).str.upper().str.strip()
    return normalized.isin({item.upper() for item in truthy}).astype(float)


def prepare_feature_frame(
    raw: pd.DataFrame,
    resample_minutes: int,
    horizon_hours: int | None = None,
    include_target: bool = True,
) -> tuple[pd.DataFrame, int | None]:
    if raw.empty:
        raise ValueError("No telemetry rows were found in tank_data for the selected filter.")

    if include_target:
        if horizon_hours is None:
            raise ValueError("horizon_hours is required when include_target=True")
        validate_forecast_args(resample_minutes, horizon_hours)
        horizon_steps: int | None = max(1, int(round((horizon_hours * 60) / resample_minutes)))
    else:
        if resample_minutes not in SUPPORTED_RESAMPLE_MINUTES:
            raise ValueError(
                f"--resample-minutes must be one of {sorted(SUPPORTED_RESAMPLE_MINUTES)}, got {resample_minutes}",
            )
        horizon_steps = None

    frame = raw.copy()
    frame["device_id"] = frame["device_id"].fillna("").replace("", "default")
    frame["created_at"] = pd.to_datetime(frame["created_at"], errors="coerce", utc=False)
    frame = frame.dropna(subset=["created_at"]).sort_values(["device_id", "created_at"])
    if frame.empty:
        raise ValueError("Telemetry rows exist, but none had a valid created_at timestamp.")

    numeric_columns = [
        "level",
        "tank_health",
        "ai_usage_rate",
        "lower_tank_level",
        "wifi_rssi",
        "tank_capacity_liters",
    ]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["motor_on"] = normalize_boolean_flag(frame["motor"], {"ON", "RUNNING"})
    frame["mode_auto"] = normalize_boolean_flag(frame["mode"], {"AUTO"})
    frame["pipe_leak_flag"] = normalize_boolean_flag(frame["pipe_leak"], {"YES", "ON", "TRUE"})
    frame["slow_leak_flag"] = normalize_boolean_flag(frame["slow_leak"], {"YES", "ON", "TRUE"})
    frame["drip_flag"] = normalize_boolean_flag(frame["drip"], {"YES", "ON", "TRUE"})
    frame["abnormal_flag"] = normalize_boolean_flag(frame["abnormal"], {"YES", "ON", "TRUE"})
    frame["dry_run_flag"] = normalize_boolean_flag(frame["dry_run"], {"YES", "ON", "TRUE"})
    frame["alert_flag"] = frame[
        ["pipe_leak_flag", "slow_leak_flag", "drip_flag", "abnormal_flag", "dry_run_flag"]
    ].max(axis=1)

    resample_rule = f"{resample_minutes}min"
    feature_frames: list[pd.DataFrame] = []

    for device_key, device_frame in frame.groupby("device_id", sort=False):
        device_frame = device_frame.set_index("created_at")
        aggregated = pd.DataFrame(index=device_frame.resample(resample_rule).size().index)
        aggregated["device_id"] = device_key
        aggregated["level"] = device_frame["level"].resample(resample_rule).mean()
        aggregated["tank_health"] = device_frame["tank_health"].resample(resample_rule).mean()
        aggregated["ai_usage_rate"] = device_frame["ai_usage_rate"].resample(resample_rule).mean()
        aggregated["lower_tank_level"] = device_frame["lower_tank_level"].resample(resample_rule).mean()
        aggregated["wifi_rssi"] = device_frame["wifi_rssi"].resample(resample_rule).mean()
        aggregated["tank_capacity_liters"] = device_frame["tank_capacity_liters"].resample(resample_rule).last()
        aggregated["motor_on"] = device_frame["motor_on"].resample(resample_rule).max()
        aggregated["mode_auto"] = device_frame["mode_auto"].resample(resample_rule).max()
        aggregated["alert_flag"] = device_frame["alert_flag"].resample(resample_rule).max()

        aggregated = aggregated.ffill(limit=max(1, int(180 / resample_minutes)))
        aggregated = aggregated.dropna(subset=["level"])
        if aggregated.empty:
            continue

        aggregated["level_delta_1"] = aggregated["level"].diff(1)
        aggregated["level_delta_4"] = aggregated["level"].diff(4)
        aggregated["usage_roll_mean_4"] = aggregated["ai_usage_rate"].rolling(4, min_periods=1).mean()
        aggregated["usage_roll_mean_24"] = aggregated["ai_usage_rate"].rolling(24, min_periods=1).mean()
        aggregated["level_roll_mean_4"] = aggregated["level"].rolling(4, min_periods=1).mean()
        aggregated["level_roll_mean_24"] = aggregated["level"].rolling(24, min_periods=1).mean()
        aggregated["pump_on_roll_mean_4"] = aggregated["motor_on"].rolling(4, min_periods=1).mean()
        aggregated["pump_on_roll_mean_24"] = aggregated["motor_on"].rolling(24, min_periods=1).mean()

        for lag in (1, 2, 4, 8, 24):
            aggregated[f"level_lag_{lag}"] = aggregated["level"].shift(lag)
            aggregated[f"tank_health_lag_{lag}"] = aggregated["tank_health"].shift(lag)
            aggregated[f"usage_lag_{lag}"] = aggregated["ai_usage_rate"].shift(lag)
            aggregated[f"motor_on_lag_{lag}"] = aggregated["motor_on"].shift(lag)

        hour = aggregated.index.hour + (aggregated.index.minute / 60.0)
        day_of_week = aggregated.index.dayofweek.astype(float)
        aggregated["hour_sin"] = np.sin((2 * np.pi * hour) / 24.0)
        aggregated["hour_cos"] = np.cos((2 * np.pi * hour) / 24.0)
        aggregated["dow_sin"] = np.sin((2 * np.pi * day_of_week) / 7.0)
        aggregated["dow_cos"] = np.cos((2 * np.pi * day_of_week) / 7.0)

        if include_target and horizon_steps is not None:
            aggregated["target_level"] = aggregated["level"].shift(-horizon_steps)

        feature_frames.append(aggregated.reset_index(names="created_at"))

    if not feature_frames:
        raise ValueError("No resampled telemetry was available after preprocessing.")

    feature_frame = pd.concat(feature_frames, ignore_index=True)
    if include_target:
        feature_frame = feature_frame.dropna(subset=["target_level"])
    return feature_frame, horizon_steps


def build_feature_matrix(feature_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series | None]:
    matrix = feature_frame[FEATURE_COLUMNS].copy()
    matrix = matrix.dropna(axis=1, how="all")
    target = feature_frame["target_level"].copy() if "target_level" in feature_frame.columns else None
    return matrix, target


def align_feature_matrix(matrix: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    if not feature_columns:
        raise RuntimeError("Model artifact is missing feature_columns metadata.")

    aligned = matrix.copy()
    for column in feature_columns:
        if column not in aligned.columns:
            aligned[column] = np.nan
    return aligned[feature_columns]


def load_forecast_artifact(path: Path) -> dict[str, object]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(f"Model artifact not found: {artifact_path}")

    with artifact_path.open("rb") as artifact_file:
        payload = pickle.load(artifact_file)

    if not isinstance(payload, dict):
        raise RuntimeError("Forecast artifact is invalid: expected a dictionary payload.")

    model = payload.get("model")
    if model is None or not hasattr(model, "predict"):
        raise RuntimeError("Forecast artifact is invalid: missing a predict-capable model.")

    feature_columns = payload.get("feature_columns") or []
    if not isinstance(feature_columns, list) or not all(isinstance(item, str) and item for item in feature_columns):
        raise RuntimeError("Forecast artifact is invalid: feature_columns metadata is missing or malformed.")

    metrics = payload.get("metrics") or {}
    metadata = payload.get("metadata") or {}
    return {
        "model": model,
        "feature_columns": feature_columns,
        "metrics": dict(metrics) if isinstance(metrics, dict) else {},
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        "train_rows": payload.get("train_rows"),
        "test_rows": payload.get("test_rows"),
    }


def predict_latest_level(raw: pd.DataFrame, artifact: dict[str, object]) -> dict[str, object]:
    metadata = dict(artifact.get("metadata") or {})
    resample_minutes = int(metadata.get("resample_minutes") or 15)
    horizon_hours = int(metadata.get("horizon_hours") or 1)
    validate_forecast_args(resample_minutes, horizon_hours)

    feature_frame, _ = prepare_feature_frame(
        raw,
        resample_minutes=resample_minutes,
        horizon_hours=horizon_hours,
        include_target=False,
    )
    matrix, _ = build_feature_matrix(feature_frame)
    aligned_matrix = align_feature_matrix(matrix, list(artifact.get("feature_columns") or []))
    if aligned_matrix.empty:
        raise ValueError("Not enough telemetry rows are available to build a forecast window yet.")

    latest_index = aligned_matrix.index[-1]
    latest_features = aligned_matrix.iloc[[-1]]
    latest_row = feature_frame.loc[latest_index]
    raw_prediction = float(artifact["model"].predict(latest_features)[0])
    bounded_prediction = max(0.0, min(100.0, raw_prediction))

    observed_at = latest_row.get("created_at")
    if hasattr(observed_at, "to_pydatetime"):
        observed_at = observed_at.to_pydatetime()
    forecast_for = observed_at + pd.Timedelta(hours=horizon_hours) if observed_at is not None else None
    if hasattr(forecast_for, "to_pydatetime"):
        forecast_for = forecast_for.to_pydatetime()

    current_level = latest_row.get("level")
    tank_capacity = latest_row.get("tank_capacity_liters")
    current_level_percent = None if pd.isna(current_level) else float(current_level)
    predicted_remaining_liters = None
    if not pd.isna(tank_capacity):
        predicted_remaining_liters = float(float(tank_capacity) * bounded_prediction / 100.0)

    return {
        "device_id": str(latest_row.get("device_id") or "").strip() or None,
        "observed_at": observed_at,
        "forecast_for": forecast_for,
        "current_level_percent": current_level_percent,
        "predicted_level_percent": float(bounded_prediction),
        "predicted_delta_percent": None
        if current_level_percent is None
        else float(bounded_prediction - current_level_percent),
        "predicted_remaining_liters": predicted_remaining_liters,
        "prediction_clamped": abs(raw_prediction - bounded_prediction) > 1e-9,
        "raw_predicted_level_percent": float(raw_prediction),
        "rows_considered": int(len(feature_frame)),
    }
