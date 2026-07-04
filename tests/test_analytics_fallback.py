from datetime import timedelta

from flask_app import server


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
    server.analytics_cache.clear()
    server.clear_runtime_caches(device_id)
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
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
