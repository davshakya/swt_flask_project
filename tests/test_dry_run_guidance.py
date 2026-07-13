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


def test_recovered_dry_run_flag_no_longer_drives_guidance_or_alerts():
    snapshot = {
        "device_id": "swt-test",
        "level": 86.8,
        "motor": "OFF",
        "dry_run": "YES",
        "pump_failure": "YES",
        "sensor": "OK",
        "telemetry_status": "live",
        "auto_start_pct": 40,
        "lower_tank_service": "OFF",
    }

    guidance = server.build_shared_guidance_payload(snapshot, {"analysis": {"quality": {"score": 70}}})

    assert server.effective_dry_run_active(snapshot) is False
    assert server.effective_pump_failure_active(snapshot) is False
    assert guidance["title"] == "Water system is stable"
    assert guidance["confidence_percent"] == 60
    assert guidance["confidence_basis"] == "telemetry_quality"


def test_shared_guidance_suppresses_low_confidence_ai_leak_for_high_tank():
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

    assert guidance["title"] == "Water system is stable"
    assert guidance["time_to_empty_hours"] is None
    assert "1.3" not in guidance["summary"]


def test_shared_guidance_explains_high_confidence_leak_in_customer_language():
    guidance = server.build_shared_guidance_payload(
        {
            "level": 72,
            "motor": "OFF",
            "sensor": "OK",
            "telemetry_status": "live",
            "last_sync_at": "2026-07-13 08:10:00",
        },
        {
            "analysis": {
                "quality": {"score": 92},
                "forecast_confidence": 92,
                "leakage": {
                    "status": "possible_leak",
                    "score": 68,
                    "confidence": 94,
                    "reasons": [
                        "Tank level dropped repeatedly while the pump was off.",
                        "Peak off-pump loss rate is very high.",
                    ],
                },
            },
            "levels": {"values": [78, 75, 72]},
        },
    )

    assert guidance["title"] == "Possible Water Leak Detected"
    assert guidance["risk_label"] == "Medium"
    assert guidance["reliability_label"] == "High"
    assert guidance["confidence_percent"] == 94
    assert guidance["motor_safety"] == "No immediate motor-safety alert was detected."
    assert "Water was being used even while the pump was off." in guidance["observations"]
    assert guidance["possible_causes"]
    assert guidance["estimated_impact"]["water_loss_liters"] is None
    assert "Not enough evidence" in guidance["estimated_impact"]["message"]


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


def test_set_alert_keeps_one_active_row_per_device_and_kind(monkeypatch):
    device_id = "swt-alert-dedupe-test"
    kind = "pump_failure"
    monkeypatch.setattr(server, "send_alert_webhook", lambda _payload: None)

    with server.get_db() as db:
        db.execute("DELETE FROM ops_alerts WHERE device_id = ? AND kind = ?", (device_id, kind))

    server.forget_alert_touch(kind, device_id)
    server.set_alert(kind, "danger", "Pump failure reported by firmware.", device_id=device_id, active=True)
    first_row = server.fetch_active_alerts(device_id=device_id)[0]

    server.forget_alert_touch(kind, device_id)
    server.set_alert(kind, "danger", "Pump failure reported by firmware.", device_id=device_id, active=False)
    server.forget_alert_touch(kind, device_id)
    server.set_alert(kind, "danger", "Pump failure reported by firmware.", device_id=device_id, active=True)

    active_rows = [
        row
        for row in server.fetch_active_alerts(limit=10, device_id=device_id)
        if row["kind"] == kind
    ]
    assert len(active_rows) == 1
    assert active_rows[0]["id"] == first_row["id"]

    with server.get_db() as db:
        db.execute(
            """
            INSERT INTO ops_alerts(device_id, kind, severity, message, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            (device_id, kind, "danger", "Duplicate pump failure row.",),
        )

    server.forget_alert_touch(kind, device_id)
    server.set_alert(kind, "danger", "Pump failure reported by firmware.", device_id=device_id, active=True)

    active_rows = [
        row
        for row in server.fetch_active_alerts(limit=10, device_id=device_id)
        if row["kind"] == kind
    ]
    assert len(active_rows) == 1
