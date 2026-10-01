from datetime import timedelta

from flask_app import server


def test_device_detail_template_renders_when_optional_json_context_is_missing():
    with server.app.test_request_context("/devices/swt-999-999-999-997"):
        html = server.render_template(
            "device_detail.html",
            device_id="swt-999-999-999-997",
            is_admin=True,
            snapshot={},
            customer_account=None,
            service_config={},
            automation_settings={},
            current_saved_config={},
            peer_channel_input_value=1,
            android_sso_active_session_count=0,
            firmware_install_profile={"label": "Master + Slave"},
            simulator_enabled=False,
            simulator_state="",
            initial_events=[],
            initial_info_cards=[],
            latest_firmware_artifacts={},
            config_message="",
            config_error="",
            csrf_token="test",
            master_upper_checked=False,
            slave_upper_checked=True,
            upper_setup_label="Slave Upper",
        )

    assert "const INITIAL_SYSTEM_STATUS={};" in html
    assert "Live device activity is loading from the current Flask snapshot." in html


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
    node_status_event = next(event for event in events if event["kind"] == "node_current_status")
    assert "Live node status: master reachable, slave reachable;" in node_status_event["message"]
    assert all("Activity log is ready" not in event["message"] for event in events)


def test_build_events_current_node_status_uses_live_snapshot_over_raw_telemetry(monkeypatch):
    device_id = "swt-999-999-999-993"
    created_at = server.format_timestamp(server.now_utc() - timedelta(hours=3))

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
                41.1,
                "OFF",
                "AUTO",
                "OK",
                "ONLINE",
                "26.1.698",
                224000,
                89.9,
                "ON",
                "ON",
                "enabled",
                11,
                11,
                0,
                "48:3F:DA:8A:FC:93",
                "master",
                "master",
                created_at,
            ),
        )

    monkeypatch.setattr(
        server,
        "fetch_device_snapshot",
        lambda requested_device_id: {
            "device_id": requested_device_id,
            "telemetry_status": "live",
            "node_role": "master",
            "device_type": "master",
            "direct_peer": "enabled",
            "direct_peer_config_channel": 11,
            "direct_peer_wifi_channel": 11,
            "direct_peer_last_packet_age_s": 0,
            "direct_peer_last_packet_bytes": 84,
            "direct_peer_remote_mac": "48:3F:DA:8A:FC:93",
            "direct_peer_sync_last_ok_age_s": 10,
            "telemetry_service": "ON",
            "command_service": "ON",
            "ota_service": "OFF",
            "lower_tank_service": "ON",
            "buzzer_service": "ON",
            "led_display_service": "ON",
            "local_firmware_upload_service": "ON",
        },
    )

    try:
        events = server.build_events(limit=10, device_id=device_id, sync=False)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    node_status_event = next(event for event in events if event["kind"] == "node_current_status")
    assert "Live node status: master reachable, slave reachable;" in node_status_event["message"]


def test_stale_master_snapshot_never_reports_slave_peer_reachable(monkeypatch):
    device_id = "swt-999-999-999-992"
    created_at = server.format_timestamp(server.now_utc() - timedelta(hours=2))
    stale_snapshot = {
        "device_id": device_id,
        "telemetry_status": "stale",
        "seconds_since_sync": 7200,
        "node_role": "master",
        "device_type": "master",
        "direct_peer": "enabled",
        "direct_peer_config_channel": 1,
        "direct_peer_wifi_channel": 1,
        "direct_peer_last_packet_age_s": 0,
        "direct_peer_remote_ip": "192.168.1.9",
        "created_at": created_at,
    }
    monkeypatch.setattr(server, "fetch_device_snapshot", lambda _device_id: dict(stale_snapshot))

    with server.get_db() as db:
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute(
            """
            INSERT INTO tank_data(
                device_id, device_source, level, motor, mode, sensor, wifi,
                direct_peer, direct_peer_config_channel, direct_peer_wifi_channel,
                direct_peer_last_packet_age_s, node_role, device_type, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (device_id, server.DEVICE_SOURCE_REAL, 50, "OFF", "AUTO", "OK", "OFFLINE",
             "enabled", 1, 1, 0, "master", "master", created_at),
        )

    try:
        events = server.build_events(limit=20, device_id=device_id, sync=False)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    messages = [event.get("message", "") for event in events]
    assert any("master telemetry is stale" in message for message in messages)
    assert not any("Slave peer reachable" in message for message in messages)


def test_local_firmware_logs_become_activity_events(monkeypatch):
    device_id = "swt-999-999-999-995"

    def fake_fetch_local_device_logs(base_url, device_id=None):
        assert base_url == "http://192.168.1.2"
        return {
            "device_id": device_id,
            "firmware_role": "master",
            "device_local_url": base_url,
            "logs": [
                {
                    "line": (
                        "[2026-06-27T11:21:04+05:30] [INFO] "
                        "flask_command Command executed: THRESHOLDS:35:95"
                    )
                },
                {
                    "line": (
                        "[2026-06-27T11:21:05+05:30] [WARNING] "
                        "Peer channel sync is waiting for accepted slave packet"
                    )
                },
            ],
        }

    monkeypatch.setattr(server, "fetch_local_device_logs", fake_fetch_local_device_logs)

    events = server.build_local_firmware_log_events(
        limit=5,
        device_id=device_id,
        snapshot={"device_local_url": "http://192.168.1.2"},
    )

    assert [event["kind"] for event in events] == [
        "firmware_command_applied",
        "firmware_peer_channel_log",
    ]
    assert events[0]["severity"] == "success"
    assert events[0]["details"]["source_table"] == "firmware_local_log"
    assert "THRESHOLDS:35:95" in events[0]["message"]


def test_telemetry_pushed_firmware_logs_are_persisted_as_activity_events():
    device_id = "swt-999-999-999-994"

    with server.get_db() as db:
        db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    try:
        server.process_telemetry_payload(
            {
                "device_id": device_id,
                "device_source": server.DEVICE_SOURCE_REAL,
                "level": 64.0,
                "motor": "OFF",
                "mode": "AUTO",
                "sensor": "OK",
                "firmware_role": "master",
                "firmware_logs": [
                    {
                        "line": (
                            "[2026-06-27T11:21:06+05:30] [INFO] "
                            "flask_command Command executed: SERVICECFG5:1:1:0:1:0:0:1:1:1"
                        )
                    }
                ],
            },
            source_ip="unit-test",
            transport="unit-test",
        )
        events = server.fetch_device_events(limit=20, device_id=device_id)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    assert any(event["kind"] == "firmware_command_applied" for event in events)
    assert any("SERVICECFG5" in event["message"] for event in events)


def test_safety_state_and_peer_sequence_diagnostics_are_persisted(monkeypatch):
    # This contract tests the legacy history row, independently of the local
    # device.env rollout, which may intentionally disable legacy writes.
    from flask_app.capacity_features import CapacityFeatureRegistry
    monkeypatch.setattr(server, "CAPACITY_FEATURES", CapacityFeatureRegistry(environ={}))
    device_id = "swt-999-999-999-993"
    payload = {
        "device_id": device_id,
        "device_source": server.DEVICE_SOURCE_REAL,
        "level": 64.0,
        "motor": "OFF",
        "mode": "AUTO",
        "sensor": "OK",
        "controller_state": "WAITING_FOR_SOURCE",
        "upper_high_float_enabled": True,
        "upper_high_float_active": False,
        "source_low_float_enabled": True,
        "source_low_float_active": True,
        "direct_peer_last_sequence": 91,
        "direct_peer_duplicate_packets": 2,
        "direct_peer_out_of_order_packets": 3,
        "direct_peer_estimated_lost_packets": 4,
    }

    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    try:
        server.process_telemetry_payload(payload, source_ip="unit-test", transport="unit-test")
        with server.get_db() as db:
            row = db.execute(
                """
                SELECT controller_state, upper_high_float_enabled, upper_high_float_active,
                       source_low_float_enabled, source_low_float_active,
                       direct_peer_last_sequence, direct_peer_duplicate_packets,
                       direct_peer_out_of_order_packets, direct_peer_estimated_lost_packets
                FROM tank_data WHERE device_id = ? ORDER BY id DESC LIMIT 1
                """,
                (device_id,),
            ).fetchone()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    columns = (
        "controller_state",
        "upper_high_float_enabled",
        "upper_high_float_active",
        "source_low_float_enabled",
        "source_low_float_active",
        "direct_peer_last_sequence",
        "direct_peer_duplicate_packets",
        "direct_peer_out_of_order_packets",
        "direct_peer_estimated_lost_packets",
    )
    assert tuple(row[column] for column in columns) == ("WAITING_FOR_SOURCE", 1, 0, 1, 1, 91, 2, 3, 4)
