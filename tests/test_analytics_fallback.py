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
        assert payload["levels"]["values"] == [83.7]
        assert payload["levels"]["time"]
        assert payload["motor"]["values"] == [0]
        assert payload["daily"]["dates"]
        assert payload["guidance"]["title"]
        assert "live snapshot" in payload["alerts"][0].lower()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        server.clear_runtime_caches(device_id)
        server.analytics_cache.clear()
