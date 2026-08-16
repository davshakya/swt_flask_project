from __future__ import annotations

from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app.capacity_history import store_narrow_history_if_due
from flask_app.capacity_reads import fetch_latest_state_payload
from flask_app.capacity_schema import ensure_capacity_schema
from flask_app.capacity_state import upsert_device_latest_state
from flask_app import server


def registry(**features):
    return CapacityFeatureRegistry(
        environ={f"FEATURE_{name.upper()}": "true" for name, enabled in features.items() if enabled}
    )


def test_latest_state_reader_decodes_complete_payload_and_timestamp():
    device_id = "swt-capacity-read-001"
    with server.get_db() as db:
        cursor = db.cursor()
        ensure_capacity_schema(cursor)
        cursor.execute("DELETE FROM device_latest_state WHERE device_id = ?", (device_id,))
        upsert_device_latest_state(
            cursor,
            {"device_id": device_id, "device_source": "virtual", "level": 77, "motor": "ON"},
            "2026-08-09 12:00:00",
        )
        payload = fetch_latest_state_payload(cursor, device_id, "virtual")
        cursor.execute("DELETE FROM device_latest_state WHERE device_id = ?", (device_id,))

    assert payload["device_id"] == device_id
    assert payload["level"] == 77
    assert payload["created_at"].strftime("%Y-%m-%d %H:%M:%S") == "2026-08-09 12:00:00"


def test_dashboard_capacity_overlay_preserves_materialized_events(monkeypatch):
    feature_registry = registry(capacity_schema=True, latest_state_writes=True, dashboard_read_latest_state=True)
    monkeypatch.setattr(server, "CAPACITY_FEATURES", feature_registry)
    monkeypatch.setattr(
        server,
        "load_dashboard_snapshot",
        lambda device_id, prefer_capacity=False: {
            "device_id": device_id,
            "device_source": "virtual",
            "level": 66,
            "created_at": "2026-08-09 12:00:00",
            "telemetry_status": "live",
            "sensor": "OK",
            "motor": "OFF",
        },
    )
    monkeypatch.setattr(server, "build_system_status_payload", lambda snapshot, device_id=None: {"level": snapshot["level"]})
    monkeypatch.setattr(server, "build_monitoring_summary_payload", lambda snapshot, device_id=None: {"device_id": device_id})

    result = server.overlay_capacity_snapshot(
        {"snapshot": {"level": 10}, "events": [{"id": 1}], "audit": [{"id": 2}]},
        "swt-test-001",
        "dashboard_read_latest_state",
    )

    assert result["snapshot"]["level"] == 66
    assert result["events"] == [{"id": 1}]
    assert result["audit"] == [{"id": 2}]
    assert result["system_status"] == {"level": 66}


def test_narrow_history_read_flag_preserves_history_contract(monkeypatch):
    device_id = "swt-capacity-history-read-001"
    feature_registry = registry(
        capacity_schema=True,
        latest_state_writes=True,
        narrow_history_writes=True,
        history_read_narrow_table=True,
    )
    monkeypatch.setattr(server, "CAPACITY_FEATURES", feature_registry)
    with server.get_db() as db:
        cursor = db.cursor()
        ensure_capacity_schema(cursor)
        cursor.execute("DELETE FROM tank_telemetry_history WHERE device_id = ?", (device_id,))
        cursor.execute("DELETE FROM telemetry_sampling_state WHERE device_id = ?", (device_id,))
        store_narrow_history_if_due(
            cursor,
            {
                "device_id": device_id,
                "device_source": server.get_device_source_mode(),
                "level": 61,
                "lower_tank_level": 42,
                "motor": "ON",
                "sensor": "OK",
            },
            "2026-08-09 12:00:00",
            adaptive=False,
        )

    try:
        history = server.fetch_device_history(device_id, limit=10)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_telemetry_history WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM telemetry_sampling_state WHERE device_id = ?", (device_id,))

    assert history == [{
        "time": "2026-08-09 12:00:00",
        "level": 61.0,
        "lower_tank_level": 42.0,
        "source_tank_level": 42.0,
        "motor": "ON",
        "sensor": "OK",
        "wifi_rssi": None,
        "free_heap": None,
        "cpu_utilization_pct": None,
        "slave_free_heap": None,
        "slave_cpu_utilization_pct": None,
        "node_role": None,
        "device_type": None,
    }]
