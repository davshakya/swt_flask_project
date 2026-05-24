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
    assert guidance["title"] == "Pump locked by source tank safety"
    assert "Dry-run protection stopped the pump" in guidance["summary"]
    assert "Check source water" in guidance["action_title"]


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
