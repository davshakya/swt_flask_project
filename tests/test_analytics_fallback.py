from datetime import timedelta
import json

from flask_app import server


def test_explicit_seven_day_payload_uses_the_selected_range_for_all_charts(monkeypatch):
    today_payload = {
        "range": {"label": "Today"},
        "levels": {"time": ["today"], "values": [75]},
        "motor": {"time": ["today"], "values": [0]},
        "pump_activity": {"completed_runs": 0},
    }
    monkeypatch.setattr(server, "build_analytics", lambda *_args, **_kwargs: today_payload)

    with server.app.test_request_context("/analytics?days=7"):
        payload = server.attach_default_chart_windows(
            {"range": {"label": "Last 7 days"}, "daily": {"dates": ["day-1"]}},
            device_id="swt-test",
        )

    assert payload == {"range": {"label": "Last 7 days"}, "daily": {"dates": ["day-1"]}}


def test_service_config_upsert_persists_ai_and_cloud_mode():
    device_id = "swt-analytics-config-001"
    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    with server.get_db() as db:
        db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))

    try:
        config = server.upsert_device_service_config(
            device_id,
            ai_analysis_enabled=True,
            cloud_feed_mode=server.DEVICE_SERVICE_CLOUD_FEED_FULL,
            slave_device_enabled=True,
            slave_upper_sensor_enabled=True,
            source_tank_monitoring_enabled=False,
            relay_enabled=True,
            direct_peer_wifi_channel=6,
        )

        assert config["ai_analysis_enabled"] is True
        assert config["effective_ai_analysis_enabled"] is True
        assert config["cloud_feed_mode"] == server.DEVICE_SERVICE_CLOUD_FEED_FULL
        assert config["direct_peer_wifi_channel"] == 6
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()


def test_low_history_analytics_returns_live_snapshot_fallback():
    device_id = "swt-analytics-live-001"
    now = server.now_utc()
    cache_key = server.build_analytics_cache_key(now - timedelta(days=1), now + timedelta(days=1), device_id)
    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        db.execute(
            """
            INSERT INTO tank_data(
                device_id, device_source, level, motor, mode, sensor, wifi,
                tank_capacity_liters, tank_health, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                server.DEVICE_SOURCE_REAL,
                83.7,
                "OFF",
                "AUTO",
                "OK",
                "Excellent",
                2000,
                100,
                now.strftime(server.TIMESTAMP_FORMAT),
            ),
        )

    try:
        payload = server.build_analytics(
            now - timedelta(days=1),
            now + timedelta(days=1),
            "Today",
            device_id=device_id,
        )

        assert payload["analysis"]["live_snapshot_fallback"] is True
        assert payload["levels"]["values"] == [83.7, 83.7]
        assert len(payload["levels"]["time"]) == 2
        assert payload["motor"]["values"] == [0, 0]
        assert len(payload["motor"]["time"]) == 2
        assert payload["daily"]["dates"]
        assert len(payload["daily"]["dates"]) == len(payload["daily"]["values"])
        assert payload["prediction"]["tomorrow_usage"] is None
        assert payload["insights"]["avg_daily_usage"] is None
        assert payload["guidance"]["title"]
        assert "live snapshot" in payload["alerts"][0].lower()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()


def test_last_valid_analytics_survives_empty_recalculation():
    device_id = "swt-analytics-cache-001"
    now = server.now_utc()
    start_dt = now - timedelta(days=1)
    end_exclusive = now + timedelta(days=1)
    cache_key = server.build_analytics_cache_key(start_dt, end_exclusive, device_id)
    setting_key = server.analytics_last_valid_setting_key(cache_key)
    valid_payload = {
        "range": {
            "label": "Today",
            "start_date": start_dt.strftime(server.DATE_ONLY_FORMAT),
            "end_date": now.strftime(server.DATE_ONLY_FORMAT),
        },
        "insights": {
            "consumption_rate": 13.66,
            "avg_daily_usage": 1833.39,
            "usage_change_pct": -81.9,
        },
        "daily": {"dates": ["2026-07-03", "2026-07-04"], "values": [1731.85, 313.71]},
        "pattern": {"hours": list(range(24)), "values": [0.0] * 24},
        "levels": {
            "time": ["2026-07-04 11:10:00", "2026-07-04 11:13:00", "2026-07-04 11:14:00"],
            "values": [83.5, 87.0, 86.8],
        },
        "motor": {
            "time": ["2026-07-04 11:10:00", "2026-07-04 11:13:00", "2026-07-04 11:14:00"],
            "values": [0, 1, 0],
        },
        "prediction": {"tomorrow_usage": 1332.1},
        "alerts": ["AI found a possible leakage pattern."],
        "analysis": {
            "quality": {"score": 80, "row_count": 3, "status": "good"},
            "forecast_confidence": 80,
            "live_snapshot_fallback": False,
            "leakage": {"status": "possible_leak", "score": 59},
        },
    }

    try:
        server.analytics_cache.clear()
        server.delete_app_setting(setting_key)
        server.store_cached_analytics(cache_key, valid_payload, now_ts=1)
        server.analytics_cache.clear()

        empty_payload = server.build_empty_analytics(start_dt, end_exclusive, "Today", device_id=device_id)
        fallback = server.fallback_analytics_payload(
            cache_key,
            empty_payload,
            reason="Fresh analytics needs more history.",
            now_ts=2,
        )

        assert fallback["analytics_cached"] is True
        assert fallback["analytics_source"] == "last_valid"
        assert fallback["insights"]["consumption_rate"] == 13.66
        assert fallback["prediction"]["tomorrow_usage"] == 1332.1
        assert fallback["analysis"]["forecast_confidence"] == 80
        assert fallback["levels"]["values"] == [83.5, 87.0, 86.8]
        assert fallback["daily"]["values"] == [1731.85, 313.71]
    finally:
        server.delete_app_setting(setting_key)
        server.analytics_cache.clear()


def test_analytics_recovers_history_from_device_events_when_tank_rows_are_thin():
    device_id = "swt-analytics-events-001"
    now = server.now_utc()
    cache_key = server.build_analytics_cache_key(now - timedelta(days=1), now + timedelta(days=1), device_id)
    event_times = [now - timedelta(minutes=30), now - timedelta(minutes=15), now - timedelta(minutes=1)]
    levels = [80.0, 72.0, 63.5]
    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        for index, (event_time, level) in enumerate(zip(event_times, levels), start=1):
            db.execute(
                """
                INSERT INTO device_events(
                    event_key, device_id, event_kind, severity, message, details_json,
                    source_table, source_row_id, event_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{device_id}:telemetry:{index}",
                    device_id,
                    "telemetry_feed_active",
                    "success",
                    "Device telemetry feed is active.",
                    json.dumps(
                        {
                            "device_id": device_id,
                            "level": level,
                            "motor": "OFF",
                            "mode": "AUTO",
                            "sensor": "OK",
                            "source_table": "tank_data",
                            "source_row_id": index,
                        }
                    ),
                    "tank_data",
                    str(index),
                    event_time.strftime(server.TIMESTAMP_FORMAT),
                ),
            )

    try:
        payload = server.build_analytics(
            now - timedelta(days=1),
            now + timedelta(days=1),
            "Today",
            device_id=device_id,
        )

        assert payload["analysis"]["live_snapshot_fallback"] is False
        assert payload["analysis"]["history_source"] == "device_events"
        assert payload["analysis"]["tank_data_row_count"] == 0
        assert payload["analysis"]["event_history_row_count"] == 3
        assert payload["analysis"]["quality"]["row_count"] == 3
        assert payload["levels"]["values"] == levels
        assert payload["insights"]["consumption_rate"] > 0
        assert payload["prediction"]["tomorrow_usage"] is None
        assert payload["prediction"]["status"] == "insufficient_data"
        assert payload["analysis"]["quality"]["sufficient_for_forecast"] is False
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()


def test_analytics_skips_invalid_levels_and_recovers_all_source_rows():
    device_id = "swt-analytics-source-recovery-001"
    original_mode = server.get_device_source_mode()
    now = server.now_utc()
    start_dt = now - timedelta(days=1)
    end_exclusive = now + timedelta(days=1)
    event_times = [now - timedelta(minutes=30), now - timedelta(minutes=15), now - timedelta(minutes=1)]
    valid_levels = [76.0, 70.5, 62.0]

    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    server.set_device_source_mode(server.DEVICE_SOURCE_VIRTUAL)
    cache_key = server.build_analytics_cache_key(start_dt, end_exclusive, device_id)
    server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        for index, event_time in enumerate(event_times, start=1):
            db.execute(
                """
                INSERT INTO tank_data(
                    device_id, device_source, level, motor, mode, sensor, wifi,
                    tank_capacity_liters, tank_health, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    server.DEVICE_SOURCE_VIRTUAL,
                    -1,
                    "OFF",
                    "AUTO",
                    "ERROR",
                    "connected",
                    1000,
                    100,
                    event_time.strftime(server.TIMESTAMP_FORMAT),
                ),
            )
            db.execute(
                """
                INSERT INTO tank_data(
                    device_id, device_source, level, motor, mode, sensor, wifi,
                    tank_capacity_liters, tank_health, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    server.DEVICE_SOURCE_REAL,
                    valid_levels[index - 1],
                    "OFF",
                    "AUTO",
                    "OK",
                    "connected",
                    1000,
                    100,
                    event_time.strftime(server.TIMESTAMP_FORMAT),
                ),
            )

    try:
        payload = server.build_analytics(start_dt, end_exclusive, "Today", device_id=device_id)

        assert payload["analysis"]["live_snapshot_fallback"] is False
        assert payload["analysis"]["history_source"] == "tank_data_all_sources"
        assert payload["analysis"]["tank_data_raw_row_count"] == 3
        assert payload["analysis"]["tank_data_row_count"] == 3
        assert payload["analysis"]["quality"]["row_count"] == 3
        assert payload["levels"]["values"] == valid_levels
        assert payload["insights"]["consumption_rate"] > 0
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
        server.set_device_source_mode(original_mode)
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()


def test_usage_estimate_does_not_recount_off_pump_sensor_oscillation():
    device_id = "swt-analytics-oscillation-001"
    now = server.now_utc()
    start_dt = now - timedelta(hours=1)
    end_exclusive = now + timedelta(hours=1)
    cache_key = server.build_analytics_cache_key(start_dt, end_exclusive, device_id)
    levels = [80.0, 70.0, 80.0, 70.0, 80.0, 70.0, 80.0, 70.0]

    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        for index, level in enumerate(levels):
            created_at = now - timedelta(minutes=(len(levels) - index) * 2)
            db.execute(
                """
                INSERT INTO tank_data(
                    device_id, device_source, level, motor, mode, sensor, wifi,
                    tank_capacity_liters, tank_health, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    server.DEVICE_SOURCE_REAL,
                    level,
                    "OFF",
                    "AUTO",
                    "OK",
                    "connected",
                    1000,
                    100,
                    created_at.strftime(server.TIMESTAMP_FORMAT),
                ),
            )

    try:
        payload = server.build_analytics(start_dt, end_exclusive, "Today", device_id=device_id)

        assert payload["daily"]["values"] == [10.0]
        assert payload["daily"]["complete"] == [False]
        assert payload["insights"]["latest_day_usage_liters"] == 100.0
        assert payload["analysis"]["quality"]["usage_physically_plausible"] is True
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()


def test_pump_activity_is_inferred_from_level_history_when_relay_stays_off(monkeypatch):
    device_id = "swt-level-fill-inference-001"
    now = server.now_utc().replace(second=0, microsecond=0)
    start_dt = now - timedelta(hours=1)
    end_exclusive = now + timedelta(minutes=1)
    cache_key = server.build_analytics_cache_key(start_dt, end_exclusive, device_id)
    levels = [50.0, 45.0, 55.0, 60.0, 60.0, 58.0]
    monkeypatch.setattr(
        server,
        "fetch_device_automation_settings",
        lambda *_args, **_kwargs: {"auto_start_pct": 40.0, "auto_stop_pct": 60.0},
    )

    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        for index, level in enumerate(levels):
            created_at = now - timedelta(minutes=(len(levels) - 1 - index) * 5)
            db.execute(
                """
                INSERT INTO tank_data(
                    device_id, device_source, level, motor, mode, sensor, wifi,
                    tank_capacity_liters, tank_health, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    server.DEVICE_SOURCE_REAL,
                    level,
                    "OFF",
                    "AUTO",
                    "OK",
                    "connected",
                    1000,
                    100,
                    created_at.strftime(server.TIMESTAMP_FORMAT),
                ),
            )

    try:
        payload = server.build_analytics(start_dt, end_exclusive, "Today", device_id=device_id)

        assert payload["motor"]["source"] == "tank_level_history"
        assert payload["motor"]["relay_state_used"] is False
        assert payload["motor"]["values"] == [0, 1, 0, 0]
        assert payload["pump_activity"]["source"] == "tank_level_history"
        assert payload["pump_activity"]["relay_state_used"] is False
        assert payload["pump_activity"]["completed_runs"] == 1
        assert payload["pump_activity"]["runtime_seconds"] == 600
        assert payload["pump_activity"]["runtime_basis"] == "tank_level_rise_to_configured_stop_threshold"
        assert payload["pump_activity"]["stop_threshold_pct"] == 60.0
        assert payload["pump_activity"]["last_started_at"] is not None
        assert payload["pump_activity"]["last_stopped_at"] is not None
        assert payload["insights"]["latest_day_usage"] == 7.0
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        server.delete_app_setting(server.analytics_last_valid_setting_key(cache_key))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()
