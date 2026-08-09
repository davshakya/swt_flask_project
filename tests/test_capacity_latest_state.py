from __future__ import annotations

import json

from flask_app.capacity_state import build_alert_flags, normalize_device_reported_at, upsert_device_latest_state
from flask_app.capacity_schema import ensure_capacity_schema
from flask_app.capacity_features import CapacityFeatureRegistry
from flask_app import server


def test_alert_flags_support_explicit_and_derived_values():
    assert build_alert_flags({"alert_flags": "9", "leak": "OFF"}) == 9
    assert build_alert_flags({"leak": "ON", "dry_run": True, "pump_failure": "NO"}) == 5


def test_device_reported_timestamp_is_normalized_to_utc_mysql_format():
    assert normalize_device_reported_at("2026-08-09T12:30:00+05:30") == "2026-08-09 07:00:00"
    assert normalize_device_reported_at("invalid") is None


def test_latest_state_upsert_is_monotonic_within_boot_and_accepts_new_boot():
    device_id = "swt-capacity-latest-state-001"
    source = "virtual"
    with server.get_db() as db:
        cursor = db.cursor()
        ensure_capacity_schema(cursor)
        cursor.execute(
            "DELETE FROM device_latest_state WHERE device_id = ? AND device_source = ?",
            (device_id, source),
        )
        first = upsert_device_latest_state(
            cursor,
            {
                "device_id": device_id,
                "device_source": source,
                "boot_id": "boot-a",
                "sequence_number": 10,
                "level": 70,
                "motor": "ON",
                "sensor": "OK",
                "device_reported_at": "2026-08-09T10:00:00Z",
            },
            "2026-08-09 10:00:01",
        )
        stale = upsert_device_latest_state(
            cursor,
            {
                "device_id": device_id,
                "device_source": source,
                "boot_id": "boot-a",
                "sequence_number": 9,
                "level": 20,
            },
            "2026-08-09 10:00:02",
        )
        after_stale = cursor.execute(
            "SELECT boot_id, sequence_number, level FROM device_latest_state WHERE device_id = ? AND device_source = ?",
            (device_id, source),
        ).fetchone()
        new_boot = upsert_device_latest_state(
            cursor,
            {
                "device_id": device_id,
                "device_source": source,
                "boot_id": "boot-b",
                "sequence_number": 1,
                "level": 71,
                "leak": "ON",
            },
            "2026-08-09 10:00:03",
        )
        final = cursor.execute(
            "SELECT boot_id, sequence_number, level, alert_flags, state_json FROM device_latest_state WHERE device_id = ? AND device_source = ?",
            (device_id, source),
        ).fetchone()
        cursor.execute(
            "DELETE FROM device_latest_state WHERE device_id = ? AND device_source = ?",
            (device_id, source),
        )

    assert first == "inserted"
    assert stale == "stale"
    assert after_stale["level"] == 70
    assert new_boot == "updated"
    assert final["boot_id"] == "boot-b"
    assert final["sequence_number"] == 1
    assert final["level"] == 71
    assert final["alert_flags"] == 1
    assert json.loads(final["state_json"])["device_id"] == device_id


def test_telemetry_dual_write_is_feature_controlled_and_atomic(monkeypatch):
    device_id = "swt-capacity-dual-write-001"
    registry = CapacityFeatureRegistry(
        environ={"FEATURE_CAPACITY_SCHEMA": "true", "FEATURE_LATEST_STATE_WRITES": "true"}
    )
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry)
    monkeypatch.setattr(server, "postprocess_telemetry_payload", lambda *_args, **_kwargs: None)
    with server.get_db() as db:
        ensure_capacity_schema(db.cursor())
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_latest_state WHERE device_id = ?", (device_id,))

    try:
        result = server.process_telemetry_payload(
            {
                "device_id": device_id,
                "device_source": "virtual",
                "boot_id": "dual-write-boot",
                "sequence_number": 1,
                "level": 44,
                "motor": "OFF",
                "mode": "AUTO",
                "sensor": "OK",
            },
            source_ip="unit-test",
            transport="unit-test-dual-write",
        )
        with server.get_db() as db:
            legacy_count = db.execute(
                "SELECT COUNT(*) AS count FROM tank_data WHERE device_id = ?",
                (device_id,),
            ).fetchone()["count"]
            latest = db.execute(
                "SELECT level, sequence_number FROM device_latest_state WHERE device_id = ?",
                (device_id,),
            ).fetchone()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_latest_state WHERE device_id = ?", (device_id,))

    assert result["_latest_state_write_result"] == "inserted"
    assert legacy_count == 1
    assert latest["level"] == 44
    assert latest["sequence_number"] == 1


def test_latest_state_failure_rolls_back_legacy_insert(monkeypatch):
    device_id = "swt-capacity-dual-write-rollback-001"
    registry = CapacityFeatureRegistry(
        environ={"FEATURE_CAPACITY_SCHEMA": "true", "FEATURE_LATEST_STATE_WRITES": "true"}
    )
    monkeypatch.setattr(server, "CAPACITY_FEATURES", registry)
    monkeypatch.setattr(
        server,
        "upsert_device_latest_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("latest-state failure")),
    )
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    try:
        try:
            server.process_telemetry_payload(
                {
                    "device_id": device_id,
                    "device_source": "virtual",
                    "level": 40,
                    "motor": "OFF",
                    "mode": "AUTO",
                    "sensor": "OK",
                },
                source_ip="unit-test",
                transport="unit-test-rollback",
            )
        except RuntimeError as exc:
            assert str(exc) == "latest-state failure"
        else:
            raise AssertionError("latest-state failure did not propagate")
        with server.get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) AS count FROM tank_data WHERE device_id = ?",
                (device_id,),
            ).fetchone()["count"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    assert count == 0

def test_latest_state_probe_does_not_gap_lock_new_device_ids():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "flask_app" / "capacity_state.py").read_text(encoding="utf-8")
    start = source.index("def upsert_device_latest_state")
    assert "FOR UPDATE" not in source[start:]


def test_multi_worker_schema_initialization_uses_mysql_advisory_lock():
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")
    assert "def init_db_serialized():" in source
    assert 'SELECT GET_LOCK(?, ?) AS acquired' in source
    assert 'SELECT RELEASE_LOCK(?) AS released' in source
    assert "init_db_serialized()" in source
