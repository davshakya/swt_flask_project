from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask_app import server


def test_leakage_ai_model_flags_unusual_off_pump_drop_pattern():
    model = server.build_leakage_ai_model(
        leak_events=0,
        consumption_rate=16.5,
        consumption_rate_segments=[2.0, 2.4, 2.1, 2.2, 14.0, 18.0, 21.0],
        usage_change_pct=72.0,
        motor_cycles=4,
        refill_events=0,
        valid_hours=3.5,
        valid_drop_count=5,
        quality={"score": 88},
        pump_activity_metrics={
            "runtime_hours": 0.8,
            "duty_cycle_pct": 18.0,
            "short_cycle_count": 1,
            "avg_run_seconds": 480,
        },
    )

    assert model["status"] in {"likely_leak", "possible_leak"}
    assert model["score"] >= 45
    assert model["leak_type"] in {"pipe_leak", "slow_leak", "usage_anomaly"}
    assert model["confidence"] >= 80


def test_leakage_ai_model_keeps_normal_usage_clear():
    model = server.build_leakage_ai_model(
        leak_events=0,
        consumption_rate=1.6,
        consumption_rate_segments=[1.2, 1.5, 1.4, 1.7, 1.6],
        usage_change_pct=5.0,
        motor_cycles=2,
        refill_events=1,
        valid_hours=2.0,
        valid_drop_count=1,
        quality={"score": 82},
        pump_activity_metrics={"duty_cycle_pct": 4.0, "short_cycle_count": 0},
    )

    assert model["status"] == "normal"
    assert model["score"] < 30
    assert model["leak_type"] == "none"
    assert model["features"]["pump_duty_cycle_pct"] == 4.0


def test_motor_activity_metrics_use_actual_time_gaps():
    metrics = server.build_motor_activity_metrics(
        [
            "2026-06-01 10:00:00",
            "2026-06-01 10:10:00",
            "2026-06-01 10:20:00",
            "2026-06-01 10:23:00",
            "2026-06-01 10:30:00",
        ],
        [1, 0, 1, 0, 0],
    )

    assert metrics["runtime_seconds"] == 13 * 60
    assert metrics["completed_runs"] == 2
    assert metrics["short_cycle_count"] == 1
    assert metrics["duty_cycle_pct"] == 43.33


def test_analysis_payload_includes_ai_leakage_anomaly_without_firmware_flag():
    leakage = {
        "status": "possible_leak",
        "severity": "warning",
        "score": 58,
        "model": "telemetry-leakage-ai-v1",
    }

    payload = server.build_analysis_payload(
        quality={"score": 82},
        current_level=52.0,
        consumption_rate=11.0,
        empty_prediction=8.0,
        usage_change_pct=30.0,
        leak_events=0,
        motor_cycles=3,
        refill_events=0,
        event_analysis={},
        leakage_model=leakage,
        pump_activity_metrics={"short_cycle_count": 4, "runtime_seconds": 900, "duty_cycle_pct": 25.0},
    )

    assert payload["leakage"] == leakage
    assert any(item["kind"] == "ai_leakage" for item in payload["anomalies"])
    assert any(item["kind"] == "short_cycling" for item in payload["anomalies"])
    assert payload["event_window"]["telemetry_short_cycle_count"] == 4
