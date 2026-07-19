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


def test_daily_usage_plausibility_rejects_excessive_tank_turnovers():
    plausible, limit, peak = server.daily_usage_matches_tank_turnover_limit(
        {"2026-07-17": 240.0, "2026-07-18": 850.0},
        max_turnovers=8,
    )

    assert plausible is False
    assert limit == 800.0
    assert peak == 850.0


def test_daily_usage_plausibility_accepts_normal_refill_demand():
    plausible, limit, peak = server.daily_usage_matches_tank_turnover_limit(
        {"2026-07-17": 240.0, "2026-07-18": 315.0},
        max_turnovers=8,
    )

    assert plausible is True
    assert limit == 800.0
    assert peak == 315.0

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
    assert metrics["started_runs"] == 2
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


def test_observed_motor_series_counts_relay_cycles_even_when_sensor_level_is_invalid():
    rows = [
        {"created_at": "2026-07-19 08:00:00", "motor": "OFF", "level": -1},
        {"created_at": "2026-07-19 08:00:05", "motor": "ON", "level": -1},
        {"created_at": "2026-07-19 08:00:10", "motor": "OFF", "level": 31},
        {"created_at": "2026-07-19 08:00:15", "motor": "ON", "level": 32},
        {"created_at": "2026-07-19 08:00:20", "motor": "OFF", "level": 33},
    ]

    times, values = server.build_observed_motor_activity_series(rows)
    metrics = server.build_motor_activity_metrics(times, values)

    assert values == [0, 1, 0, 1, 0]
    assert metrics["completed_runs"] == 2
    assert metrics["runtime_seconds"] == 10
    assert metrics["short_cycle_count"] == 2


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
    levels = [20.0, 60.0, 85.0, 92.0, 94.0, 94.0]

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


def test_threshold_runtime_accepts_configured_30_to_90_fill_range():
    times = [
        "2026-07-17 04:13:01",
        "2026-07-17 04:14:00",
        "2026-07-17 04:15:00",
        "2026-07-17 04:16:24",
    ]
    levels = [30.0, 55.0, 78.0, 90.5]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times,
        levels,
        stop_threshold_pct=90.0,
        start_threshold_pct=30.0,
    )

    assert states == [1, 1, 1, 0]
    assert len(runs) == 1
    assert runs[0]["start_level_pct"] == 30.0
    assert runs[0]["stop_level_pct"] == 90.5


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


def test_threshold_runtime_uses_local_minimum_to_90_and_keeps_fluctuations_in_cycle():
    times = [
        "2026-07-11 00:31:46",
        "2026-07-11 00:38:28",
        "2026-07-11 01:22:15",
        "2026-07-11 01:27:43",
        "2026-07-11 01:29:04",
        "2026-07-11 01:32:34",
        "2026-07-11 02:00:19",
        "2026-07-11 04:41:41",
        "2026-07-11 04:48:00",
        "2026-07-11 04:53:14",
    ]
    levels = [6.79, 42.47, 35.75, 70.74, 58.27, 92.20, 80.0, 0.0, 50.0, 91.97]

    inferred_times, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )
    metrics = server.build_motor_activity_metrics(inferred_times, states)

    expected_durations = [
        int((server.parse_timestamp(times[5]) - server.parse_timestamp(times[0])).total_seconds()),
        int((server.parse_timestamp(times[9]) - server.parse_timestamp(times[7])).total_seconds()),
    ]
    assert [run["duration_seconds"] for run in runs] == expected_durations
    assert [run["start_level_pct"] for run in runs] == [6.79, 0.0]
    assert metrics["completed_runs"] == 2
    assert metrics["runtime_seconds"] == sum(expected_durations)


def test_exported_minimum_to_90_cycles_total_nine_hours_twenty_minutes():
    cycles = [
        ("2026-07-11 00:31:46", 6.79, "2026-07-11 01:32:34", 92.20),
        ("2026-07-11 04:41:41", 0.0, "2026-07-11 04:53:14", 91.97),
        # This 67.96-point rebound is not a clear full filling cycle.
        ("2026-07-11 06:18:50", 25.13, "2026-07-11 06:30:33", 93.09),
        ("2026-07-11 09:18:43", 19.80, "2026-07-11 09:28:55", 97.98),
        ("2026-07-11 16:04:13", 16.0, "2026-07-11 16:18:27", 97.03),
        ("2026-07-12 03:36:06", 13.63, "2026-07-12 03:47:08", 99.12),
        ("2026-07-12 05:36:27", 0.0, "2026-07-12 07:45:06", 91.86),
        ("2026-07-12 11:53:19", 23.63, "2026-07-12 12:03:43", 97.09),
        ("2026-07-13 00:46:07", 20.66, "2026-07-13 00:58:23", 92.23),
        ("2026-07-13 04:19:22", 7.65, "2026-07-13 04:31:20", 96.17),
        ("2026-07-13 05:56:46", 7.47, "2026-07-13 06:11:00", 92.12),
        ("2026-07-13 15:01:01", 11.14, "2026-07-13 15:40:53", 99.78),
        ("2026-07-14 06:51:09", 5.82, "2026-07-14 07:16:36", 92.12),
        ("2026-07-14 14:00:29", 7.71, "2026-07-14 14:20:53", 95.55),
        ("2026-07-15 00:55:22", 17.86, "2026-07-15 01:12:36", 92.69),
        ("2026-07-15 04:42:17", 19.09, "2026-07-15 04:56:51", 91.09),
        ("2026-07-15 06:22:05", 0.0, "2026-07-15 06:36:34", 93.80),
        ("2026-07-15 14:25:48", 0.0, "2026-07-15 16:11:02", 92.20),
        ("2026-07-16 04:41:32", 17.34, "2026-07-16 04:57:28", 91.77),
        ("2026-07-16 06:33:46", 8.88, "2026-07-16 06:46:31", 95.32),
        ("2026-07-16 13:19:19", 29.23, "2026-07-16 13:29:00", 99.18),
    ]
    times = [timestamp for cycle in cycles for timestamp in (cycle[0], cycle[2])]
    levels = [level for cycle in cycles for level in (cycle[1], cycle[3])]

    inferred_times, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )
    metrics = server.build_motor_activity_metrics(inferred_times, states)

    assert len(runs) == 20
    assert metrics["completed_runs"] == 20
    assert metrics["runtime_seconds"] == 9 * 3600 + 20 * 60 + 56


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


def test_level_history_does_not_count_incomplete_fill_below_90_percent():
    times = [f"2026-07-14 10:{minute:02d}:00" for minute in (0, 5, 10, 15, 20)]
    levels = [40.0, 43.0, 47.0, 47.1, 47.1]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )

    assert states == [0, 0, 0, 0, 0]
    assert runs == []


def test_level_history_requires_observed_90_percent_reading():
    times = [f"2026-07-14 11:{minute:02d}:00" for minute in (0, 5, 10, 15)]
    levels = [80.0, 84.0, 88.2, 88.3]

    _, states, runs = server.infer_pump_activity_from_level_history(
        times, levels, stop_threshold_pct=90.0
    )

    assert states == [0, 0, 0, 0]
    assert runs == []


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
    assert usage["hourly_timeline"] == {"2026-07-14 10:00:00": 7.0}


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
