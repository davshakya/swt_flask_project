#!/usr/bin/env python
"""Train a first-pass tank level forecasting model from SQLite telemetry."""

from __future__ import annotations

import argparse
import os
import pickle
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask_app.ml_forecasting import build_feature_matrix, prepare_feature_frame, query_training_rows, validate_forecast_args

# Keep training deterministic and avoid flaky multiprocessing/thread-pool issues
# on small Windows environments.
os.environ.setdefault("LOKY_MAX_CPU_COUNT", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

try:
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
except ImportError as exc:  # pragma: no cover - handled at runtime for optional dependency
    raise SystemExit(
        "scikit-learn is required for ML training. Install it with: pip install -r requirements-ml.txt",
    ) from exc


DEFAULT_DB_PATH = Path("data/tank.db")
DEFAULT_OUTPUT_PATH = Path("artifacts/level_forecast_model.pkl")


@dataclass
class TrainingResult:
    model: HistGradientBoostingRegressor
    feature_columns: list[str]
    train_rows: int
    test_rows: int
    metrics: dict[str, float]
    metadata: dict[str, object]


@dataclass(frozen=True)
class ModelTrainingConfig:
    profile: str
    params: dict[str, object]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a HistGradientBoostingRegressor to forecast future tank level.",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("DB_FILE") or str(DEFAULT_DB_PATH),
        help="Path to the SQLite database containing tank_data.",
    )
    parser.add_argument(
        "--device-id",
        default="",
        help="Optional device_id filter. Leave empty to train across all devices.",
    )
    parser.add_argument(
        "--resample-minutes",
        type=int,
        default=15,
        help="Resample cadence in minutes before feature generation. Recommended: 15 or 30.",
    )
    parser.add_argument(
        "--horizon-hours",
        type=int,
        default=1,
        help="Forecast horizon in hours for the target tank level.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=50,
        help="Minimum engineered rows required before training starts. Use 50 for short pilot windows.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Where to save the trained model artifact.",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    try:
        validate_forecast_args(
            resample_minutes=args.resample_minutes,
            horizon_hours=args.horizon_hours,
            min_samples=args.min_samples,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def chronological_split(
    matrix: pd.DataFrame,
    target: pd.Series,
    timestamps: pd.Series,
    train_ratio: float = 0.8,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    order = np.argsort(pd.to_datetime(timestamps, errors="coerce").values)
    matrix_sorted = matrix.iloc[order].reset_index(drop=True)
    target_sorted = target.iloc[order].reset_index(drop=True)
    split_index = max(1, int(len(matrix_sorted) * train_ratio))
    split_index = min(split_index, len(matrix_sorted) - 1)
    return (
        matrix_sorted.iloc[:split_index],
        matrix_sorted.iloc[split_index:],
        target_sorted.iloc[:split_index],
        target_sorted.iloc[split_index:],
    )


def build_training_config(train_rows: int) -> ModelTrainingConfig:
    if train_rows <= 0:
        raise ValueError("train_rows must be greater than zero")

    # Small single-device pilots only yield a few dozen chronological samples
    # after lag features are engineered. Use a gentler profile in that regime.
    if train_rows < 80:
        return ModelTrainingConfig(
            profile="small_window",
            params={
                "loss": "squared_error",
                "learning_rate": 0.05,
                "max_depth": 3,
                "max_iter": 500,
                "min_samples_leaf": 5,
                "l2_regularization": 0.01,
                "early_stopping": False,
                "random_state": 42,
            },
        )

    if train_rows < 120:
        return ModelTrainingConfig(
            profile="medium_window",
            params={
                "loss": "squared_error",
                "learning_rate": 0.05,
                "max_depth": 4,
                "max_iter": 400,
                "min_samples_leaf": 10,
                "l2_regularization": 0.05,
                "early_stopping": False,
                "random_state": 42,
            },
        )

    return ModelTrainingConfig(
        profile="standard",
        params={
            "loss": "squared_error",
            "learning_rate": 0.05,
            "max_depth": 6,
            "max_iter": 300,
            "min_samples_leaf": 20,
            "l2_regularization": 0.1,
            "early_stopping": True,
            "random_state": 42,
        },
    )


def train_model(
    train_x: pd.DataFrame,
    train_y: pd.Series,
) -> tuple[HistGradientBoostingRegressor, ModelTrainingConfig]:
    training_config = build_training_config(len(train_x))
    model = HistGradientBoostingRegressor(**training_config.params)
    model.fit(train_x, train_y)
    return model, training_config


def save_artifact(result: TrainingResult, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": result.model,
        "feature_columns": result.feature_columns,
        "metrics": result.metrics,
        "metadata": result.metadata,
        "train_rows": result.train_rows,
        "test_rows": result.test_rows,
    }
    with output_path.open("wb") as artifact_file:
        pickle.dump(payload, artifact_file)


def main() -> None:
    args = parse_args()
    validate_args(args)

    db_path = Path(args.db_path)
    output_path = Path(args.output)
    try:
        raw = query_training_rows(db_path, args.device_id)
        training_frame, horizon_steps = prepare_feature_frame(
            raw,
            resample_minutes=args.resample_minutes,
            horizon_hours=args.horizon_hours,
            include_target=True,
        )
        matrix, target = build_feature_matrix(training_frame)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    usable = matrix.assign(target_level=target).dropna(subset=["target_level"])

    if len(usable) < args.min_samples:
        raise SystemExit(
            "Only "
            f"{len(usable)} engineered rows were available, which is below --min-samples={args.min_samples}. "
            "Collect more telemetry or lower --resample-minutes to retain more samples.",
        )

    usable_x = usable.drop(columns=["target_level"])
    usable_y = usable["target_level"]
    train_x, test_x, train_y, test_y = chronological_split(
        usable_x,
        usable_y,
        training_frame.loc[usable.index, "created_at"],
    )

    if train_x.empty or test_x.empty:
        raise SystemExit("Training split produced an empty train or test set. Add more telemetry first.")

    model, training_config = train_model(train_x, train_y)
    predictions = model.predict(test_x)
    rmse = float(np.sqrt(mean_squared_error(test_y, predictions)))
    metrics = {
        "mae_level_percent": round(float(mean_absolute_error(test_y, predictions)), 4),
        "rmse_level_percent": round(rmse, 4),
        "r2": round(float(r2_score(test_y, predictions)), 4),
    }

    result = TrainingResult(
        model=model,
        feature_columns=list(train_x.columns),
        train_rows=len(train_x),
        test_rows=len(test_x),
        metrics=metrics,
        metadata={
            "db_path": str(db_path),
            "device_id_filter": args.device_id or None,
            "resample_minutes": args.resample_minutes,
            "horizon_hours": args.horizon_hours,
            "horizon_steps": horizon_steps,
            "target": "future tank level percent",
            "model_family": "HistGradientBoostingRegressor",
            "training_profile": training_config.profile,
            "training_params": dict(training_config.params),
        },
    )
    save_artifact(result, output_path)

    print("Model training complete.")
    print(f"  artifact: {output_path}")
    print(f"  train rows: {result.train_rows}")
    print(f"  test rows: {result.test_rows}")
    print(f"  target: forecast level percent {args.horizon_hours} hour(s) ahead")
    for key, value in metrics.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
