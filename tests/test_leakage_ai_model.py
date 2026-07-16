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
    assert model["confidence"] > 90
    assert model["alert_eligible"] is (model["score"] > 90 and model["confidence"] > 90)


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

    assert model["status"] == "insufficient_data"
    assert model["score"] < 30
    assert model["leak_type"] == "none"
    assert model["features"]["pump_duty_cycle_pct"] == 4.0
    assert model["features"]["sufficient_evidence"] is False


def test_quality_score_reflects_window_coverage_and_evidence():
    quality = server.build_analytics_quality_payload(
        row_count=120,
        valid_hours=2,
        gap_count=8,
        valid_drop_count=1,
        latest_seconds_since_sync=30,
        window_hours=168,
    )

    assert quality["coverage_percent"] < 2
    assert quality["status"] == "limited"
    assert quality["sufficient_for_anomaly"] is False
    assert quality["sufficient_for_forecast"] is False
    assert quality["limitations"]


def test_daily_usage_plausibility_rejects_more_water_than_observed_refills_supply():
    plausible, limit = server.level_usage_matches_observed_refills(450.0, motor_cycles=3)
    assert plausible is True
    assert limit == 480.0

    plausible, limit = server.level_usage_matches_observed_refills(2281.6, motor_cycles=3)
    assert plausible is False
    assert limit == 480.0


def test_daily_forecast_requires_complete_history_and_reports_interval():
    insufficient = server.build_daily_usage_forecast([12.0], {"score": 90, "sufficient_for_forecast": True})
    assert insufficient["status"] == "insufficient_data"
    assert insufficient["value"] is None

    forecast = server.build_daily_usage_forecast(
        [10.0, 11.0, 10.5, 12.0, 11.5],
        {"score": 88, "sufficient_for_forecast": True},
    )
    assert forecast["status"] == "ready"
    assert forecast["lower"] < forecast["value"] < forecast["upper"]
    assert forecast["sample_days"] == 5


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


def test_motor_activity_ignores_duplicate_on_off_samples():
    times = [f"2026-06-01 10:0{minute}:00" for minute in range(6)]
    values = [0, 0, 1, 1, 0, 0]

    compact_times, compact_values = server.compact_motor_series(times, values)
    metrics = server.build_motor_activity_metrics(times, values)

    assert compact_times == [times[0], times[2], times[4]]
    assert compact_values == [0, 1, 0]
    assert metrics["completed_runs"] == 1
    assert metrics["runtime_seconds"] == 2 * 60


def test_chart_downsampling_preserves_short_minimum_and_maximum():
    times = list(range(100))
    values = [50.0] * 100
    values[24] = 5.0
    values[25] = 95.0

    sampled_times, sampled_values = server.downsample_series(times, values, 12)

    assert len(sampled_times) <= 12
    assert 5.0 in sampled_values
    assert 95.0 in sampled_values


def test_level_history_infers_only_confirmed_fill_cycles():
    times = [
        "2026-07-14 08:00:00",
        "2026-07-14 08:05:00",
        "2026-07-14 08:10:00",
        "2026-07-14 08:15:00",
        "2026-07-14 08:20:00",
    ]
    levels = [40.0, 45.0, 50.0, 50.0, 49.0]

    inferred_times, states, runs = server.infer_pump_activity_from_level_history(times, levels)
    metrics = server.build_motor_activity_metrics(inferred_times, states)

    assert states == [1, 1, 0, 0, 0]
    assert len(runs) == 1
    assert runs[0]["started_at"] == times[0]
    assert runs[0]["stopped_at"] == times[2]
    assert runs[0]["level_rise_pct"] == 10.0
    assert metrics["runtime_seconds"] == 600
    assert metrics["completed_runs"] == 1


def test_level_history_runtime_stops_at_first_observed_threshold_reading():
    times = [
        "2026-07-14 08:00:00",
        "2026-07-14 08:05:00",
        "2026-07-14 08:10:00",
        "2026-07-14 08:15:00",
        "2026-07-14 08:20:00",
        "2026-07-14 08:25:00",
    ]
    levels = [40.0, 60.0, 85.0, 92.0, 94.0, 94.0]

    inferred_times, states, runs = server.infer_pump_activity_from_level_history(
        times,
        levels,
        stop_threshold_pct=90.0,
    )
    metrics = server.build_motor_activity_metrics(inferred_times, states)

    assert states == [1, 1, 1, 0, 0, 0]
    assert metrics["runtime_seconds"] == 15 * 60
    assert metrics["completed_runs"] == 1
    assert runs[0]["started_at"] == times[0]
    assert runs[0]["stopped_at"] == times[3]
    assert runs[0]["stop_level_pct"] == 92.0
    assert runs[0]["stop_threshold_pct"] == 90.0


def test_level_history_runtime_sums_each_separate_rising_cycle():
    times = [
        f"2026-07-14 08:{minute:02d}:00"
        for minute in range(0, 55, 5)
    ]
    levels = [40.0, 45.0, 50.0, 50.0, 49.0, 40.0, 45.0, 50.0, 55.0, 55.0, 54.0]

    inferred_times, states, runs = server.infer_pump_activity_from_level_history(times, levels)
    metrics = server.build_motor_activity_metrics(inferred_times, states)

    assert [run["duration_seconds"] for run in runs] == [10 * 60, 15 * 60]
    assert metrics["completed_runs"] == 2
    assert metrics["runtime_seconds"] == (10 + 15) * 60
    assert metrics["avg_run_seconds"] == int(((10 + 15) * 60) / 2)


def test_level_history_does_not_guess_runtime_for_sub_threshold_rise():
    times = [
        "2026-07-14 09:00:00",
        "2026-07-14 09:05:00",
        "2026-07-14 09:10:00",
        "2026-07-14 09:15:00",
    ]
    levels = [40.0, 50.0, 60.0, 60.0]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times,
        levels,
        stop_threshold_pct=90.0,
    )

    assert states == [0, 0, 0, 0]
    assert runs == []


def test_level_history_stops_after_two_flat_intervals_below_upper_threshold():
    times = [f"2026-07-14 10:{minute:02d}:00" for minute in (0, 5, 10, 15, 20)]
    levels = [40.0, 43.0, 47.0, 47.1, 47.1]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )

    assert states == [1, 1, 0, 0, 0]
    assert len(runs) == 1
    assert runs[0]["duration_seconds"] == 10 * 60


def test_level_history_uses_upper_threshold_tolerance():
    times = [f"2026-07-14 11:{minute:02d}:00" for minute in (0, 5, 10, 15)]
    levels = [80.0, 84.0, 88.2, 88.3]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )

    assert states == [1, 1, 0, 0]
    assert len(runs) == 1
    assert runs[0]["stop_level_pct"] == 88.2


def test_level_history_merges_one_flat_interval_inside_confirmed_fill():
    times = [f"2026-07-14 12:{minute:02d}:00" for minute in (0, 5, 10, 15, 20, 25, 30)]
    levels = [40.0, 44.0, 48.0, 48.0, 52.0, 52.0, 52.0]

    _, states, runs = server.infer_pump_activity_from_level_history(times, levels)

    assert states == [1, 1, 1, 1, 0, 0, 0]
    assert len(runs) == 1
    assert runs[0]["level_rise_pct"] == 12.0


def test_level_history_rejects_physically_implausible_jump():
    times = [f"2026-07-14 13:{minute:02d}:00" for minute in (0, 5, 10, 15, 20)]
    levels = [40.0, 70.0, 75.0, 75.0, 75.0]

    _, states, runs = server.infer_pump_activity_from_level_history(times, levels)

    assert states[0] is None
    assert runs == []


def test_level_history_rejects_symmetric_sensor_bounce_as_pump_activity():
    times = [
        "2026-07-14 09:00:00",
        "2026-07-14 09:02:00",
        "2026-07-14 09:04:00",
        "2026-07-14 09:06:00",
        "2026-07-14 09:08:00",
    ]
    levels = [40.0, 60.0, 40.0, 60.0, 40.0]

    _, states, runs = server.infer_pump_activity_from_level_history(times, levels)

    assert states == [0, 0, 0, 0, 0]
    assert runs == []


def test_level_usage_excludes_confirmed_fill_and_counts_new_drawdown_only():
    times = [
        "2026-07-14 10:00:00",
        "2026-07-14 10:05:00",
        "2026-07-14 10:10:00",
        "2026-07-14 10:15:00",
        "2026-07-14 10:20:00",
        "2026-07-14 10:25:00",
    ]
    levels = [50.0, 45.0, 55.0, 60.0, 60.0, 58.0]
    _, states, runs = server.infer_pump_activity_from_level_history(times, levels)

    usage = server.estimate_level_history_usage(times, levels, states)

    assert len(runs) == 1
    assert states == [0, 1, 1, 0, 0, 0]
    assert usage["total_usage"] == 7.0
    assert usage["daily_usage"] == {"2026-07-14": 7.0}


def test_analysis_payload_hides_ai_leakage_anomaly_until_score_and_confidence_exceed_90():
    leakage = {
        "status": "possible_leak",
        "severity": "warning",
        "score": 58,
        "confidence": 91,
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
    assert not any(item["kind"] == "ai_leakage" for item in payload["anomalies"])
    assert any(item["kind"] == "short_cycling" for item in payload["anomalies"])
    assert payload["event_window"]["telemetry_short_cycle_count"] == 4


def test_ai_leakage_alert_requires_score_and_confidence_strictly_above_90():
    base = {
        "status": "possible_leak",
        "severity": "warning",
        "score": 91,
    }
    assert server.ai_leakage_alert_eligible({**base, "confidence": 90}) is False
    assert server.ai_leakage_alert_eligible({**base, "confidence": 90.0}) is False
    assert server.ai_leakage_alert_eligible({**base, "confidence": 91}) is True
    assert server.ai_leakage_alert_eligible({**base, "score": 90, "confidence": 91}) is False
    assert server.ai_leakage_alert_eligible({**base, "score": 89, "confidence": 99}) is False

    low_confidence_payload = server.build_analysis_payload(
        quality={"score": 95, "sufficient_for_anomaly": True},
        current_level=50,
        consumption_rate=8,
        empty_prediction=None,
        usage_change_pct=None,
        leak_events=0,
        motor_cycles=2,
        refill_events=0,
        leakage_model={**base, "confidence": 90},
    )
    assert not any(item["kind"] == "ai_leakage" for item in low_confidence_payload["anomalies"])
