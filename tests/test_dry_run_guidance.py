from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from flask_app import server


def test_shared_guidance_prioritizes_dry_run_as_critical_pump_protection():
    guidance = server.build_shared_guidance_payload(
        {
            "level": 42,
            "motor": "OFF",
            "dry_run": "YES",
            "sensor": "OK",
            "telemetry_status": "online",
        },
        {
            "insights": {},
            "analysis": {"quality": {"score": 88}},
            "comparison": {},
            "events_analysis": {},
            "levels": {"values": [42]},
        },
    )

    assert guidance["severity"] == "critical"
    assert guidance["tone"] == "bad"
    assert guidance["title"] == "Pump stopped to prevent dry run"
    assert "Dry-run protection stopped the pump" in guidance["summary"]
    assert guidance["action_title"] == "Check the source of water before starting the pump"


def test_shared_guidance_names_source_tank_only_when_source_monitoring_is_active():
    guidance = server.build_shared_guidance_payload(
        {
            "level": 42,
            "motor": "OFF",
            "dry_run": "YES",
            "sensor": "OK",
            "lower_tank_service": "ON",
            "lower_sensor": "OK",
            "lower_tank_level": 2,
            "telemetry_status": "online",
        },
        {
            "insights": {},
            "analysis": {"quality": {"score": 88}},
            "comparison": {},
            "events_analysis": {},
            "levels": {"values": [42]},
        },
    )

    assert guidance["title"] == "Pump locked by source tank safety"
    assert "source water" in guidance["action_note"]


def test_shared_guidance_suppresses_short_empty_forecast_for_high_tank_possible_leak():
    guidance = server.build_shared_guidance_payload(
        {
            "level": 75.9,
            "motor": "OFF",
            "pipe_leak": "NO",
            "slow_leak": "NO",
            "drip": "NO",
            "sensor": "OK",
            "telemetry_status": "online",
        },
        {
            "insights": {
                "empty_prediction": 1.3,
                "consumption_rate": 58.0,
            },
            "analysis": {
                "quality": {"score": 78},
                "forecast_confidence": 78,
                "leakage": {
                    "status": "possible_leak",
                    "score": 54,
                    "reasons": ["Tank level dropped repeatedly while the pump was off."],
                },
            },
            "comparison": {},
            "events_analysis": {},
            "levels": {"values": [78.0, 75.9]},
        },
    )

    assert guidance["title"] == "AI found a possible leakage pattern"
    assert guidance["time_to_empty_hours"] is None
    assert "1.3" not in guidance["summary"]


def test_evaluate_snapshot_alerts_maps_dry_run_to_active_alert(monkeypatch):
    calls = []

    def fake_set_alert(kind, severity, message, **kwargs):
        calls.append((kind, severity, message, kwargs.get("active"), kwargs.get("device_id")))

    monkeypatch.setattr(server, "set_alert", fake_set_alert)

    server.evaluate_snapshot_alerts(
        {
            "device_id": "swt-test",
            "telemetry_status": "online",
            "pump_failure": "NO",
            "dry_run": "YES",
            "sensor": "OK",
        }
    )

    assert ("dry_run", "danger", "Dry-run protection triggered.", True, "swt-test") in calls
