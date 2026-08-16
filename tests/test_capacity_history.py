from __future__ import annotations

from flask_app.capacity_history import choose_history_sample_reason, store_narrow_history_if_due
from flask_app.capacity_schema import ensure_capacity_schema
from flask_app import server


BASE = {
    "device_id": "swt-capacity-history-001",
    "device_source": "virtual",
    "boot_id": "history-boot",
    "sequence_number": 1,
    "level": 50.0,
    "lower_tank_level": 40.0,
    "motor": "OFF",
    "mode": "AUTO",
    "sensor": "OK",
}


def previous(**overrides):
    values = {
        "last_history_at": "2026-08-09 10:00:00",
        "last_level": 50.0,
        "last_lower_tank_level": 40.0,
        "last_motor": "OFF",
        "last_mode": "AUTO",
        "last_sensor": "OK",
        "last_alert_flags": 0,
    }
    values.update(overrides)
    return values


def test_adaptive_history_reasons_cover_state_level_and_intervals():
    assert choose_history_sample_reason(None, BASE, "2026-08-09 10:00:01") == "first_sample"
    assert choose_history_sample_reason(previous(), {**BASE, "motor": "ON"}, "2026-08-09 10:00:01") == "motor_change"
    assert choose_history_sample_reason(previous(), {**BASE, "level": 52}, "2026-08-09 10:00:01") == "level_change"
    assert choose_history_sample_reason(previous(), BASE, "2026-08-09 10:05:00") == "idle_interval"
    active_previous = previous(last_motor="ON")
    assert choose_history_sample_reason(active_previous, {**BASE, "motor": "ON"}, "2026-08-09 10:01:00") == "active_interval"
    assert choose_history_sample_reason(previous(), BASE, "2026-08-09 10:00:30") is None
    assert choose_history_sample_reason(previous(), BASE, "2026-08-09 10:00:01", adaptive=False) == "every_report"


def test_narrow_history_persists_only_due_samples_in_mysql():
    device_id = BASE["device_id"]
    with server.get_db() as db:
        cursor = db.cursor()
        ensure_capacity_schema(cursor)
        cursor.execute("DELETE FROM tank_telemetry_history WHERE device_id = ?", (device_id,))
        cursor.execute("DELETE FROM telemetry_sampling_state WHERE device_id = ?", (device_id,))
        first = store_narrow_history_if_due(cursor, BASE, "2026-08-09 10:00:00")
        skipped = store_narrow_history_if_due(
            cursor,
            {**BASE, "sequence_number": 2, "level": 50.5},
            "2026-08-09 10:00:30",
        )
        changed = store_narrow_history_if_due(
            cursor,
            {**BASE, "sequence_number": 3, "level": 52.0},
            "2026-08-09 10:00:40",
        )
        rows = cursor.execute(
            "SELECT sample_reason, level FROM tank_telemetry_history WHERE device_id = ? ORDER BY id",
            (device_id,),
        ).fetchall()
        cursor.execute("DELETE FROM tank_telemetry_history WHERE device_id = ?", (device_id,))
        cursor.execute("DELETE FROM telemetry_sampling_state WHERE device_id = ?", (device_id,))

    assert first == "first_sample"
    assert skipped == "skipped"
    assert changed == "level_change"
    assert [(row["sample_reason"], row["level"]) for row in rows] == [
        ("first_sample", 50.0),
        ("level_change", 52.0),
    ]


def test_sampling_state_probe_does_not_gap_lock_new_device_ids():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "flask_app"
        / "capacity_history.py"
    ).read_text(encoding="utf-8")
    start = source.index("def store_narrow_history_if_due")
    assert "FOR UPDATE" not in source[start:]
