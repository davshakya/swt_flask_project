from flask_app import server


def test_build_events_falls_back_to_current_status_when_device_has_no_activity():
    events = server.build_events(limit=5, device_id="swt-999-999-999-999")

    assert events
    assert events[0]["kind"] == "device_config_current_status"
    assert "Live device config" in events[0]["message"]
    assert events[0]["details"]["device_id"] == "swt-999-999-999-999"
    assert all("Activity log is ready" not in event["message"] for event in events)


def test_build_events_generates_activity_from_telemetry_when_event_table_is_empty():
    device_id = "swt-999-999-999-998"
    created_at = server.now_utc().strftime(server.TIMESTAMP_FORMAT)

    with server.get_db() as db:
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute(
            """
            INSERT INTO tank_data(
                device_id, device_source, level, motor, mode, sensor, wifi, firmware_version,
                free_heap, lower_tank_level, telemetry_service, command_service,
                direct_peer, direct_peer_config_channel, direct_peer_wifi_channel,
                direct_peer_last_packet_age_s, direct_peer_remote_mac, node_role,
                device_type, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                server.DEVICE_SOURCE_REAL,
                72.5,
                "OFF",
                "AUTO",
                "OK",
                "ONLINE",
                "26.1.642",
                18856,
                55.0,
                "ON",
                "ON",
                "enabled",
                1,
                1,
                0,
                "C4:5B:BE:6C:AE:32",
                "master",
                "master",
                created_at,
            ),
        )

    try:
        events = server.build_events(limit=10, device_id=device_id, sync=False)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    assert events
    assert events[0]["kind"] != "device_config_current_status" or "Live device config" in events[0]["message"]
    assert any(event["kind"] == "telemetry_feed_active" for event in events)
    assert all("Activity log is ready" not in event["message"] for event in events)
