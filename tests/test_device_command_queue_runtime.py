from flask_app import server


def test_turbidity_enablement_survives_old_snapshot_and_requeues_servicecfg8(monkeypatch):
    device_id = "swt-999-999-999-994"
    desired = server.default_device_service_config(device_id)
    desired.update(
        {
            "slave_device_enabled": True,
            "slave_upper_sensor_enabled": True,
            "master_upper_sensor_enabled": False,
            "source_tank_monitoring_enabled": True,
            "relay_enabled": True,
            "buzzer_enabled": True,
            "led_display_enabled": True,
            "local_firmware_upload_enabled": True,
            "municipal_sensor_enabled": True,
            "master_turbidity_enabled": True,
            "slave_turbidity_enabled": True,
        }
    )
    old_snapshot = {
        "device_id": device_id,
        "telemetry_status": "live",
        "seconds_since_sync": 0,
        "upper_sensor_source": "slave",
        "lower_tank_service": "ON",
        "relay_service": "ON",
        "buzzer_service": "ON",
        "led_display_service": "ON",
        "local_firmware_upload_service": "ON",
        "municipal_feature_enabled": True,
        "master_turbidity_enabled": False,
        "slave_turbidity_enabled": False,
    }

    rendered = server.snapshot_device_service_config(old_snapshot, device_id=device_id, existing=desired)
    assert rendered["master_turbidity_enabled"] is True
    assert rendered["slave_turbidity_enabled"] is True

    monkeypatch.setattr(
        server,
        "build_current_saved_config",
        lambda *_args, **_kwargs: {"service_config": desired, "automation_settings": {}},
    )
    sync = server.build_runtime_sync_command(device_id, snapshot=old_snapshot)
    assert sync == {"command": server.build_device_service_command(desired), "reason": "service_config"}
    assert sync["command"].startswith("SERVICECFG8:")
    assert sync["command"].endswith(":1:1:1")


def test_device_command_queue_serves_pending_config_commands_in_order_and_dedupes_family():
    device_id = "swt-999-999-999-996"

    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    try:
        first = server.queue_command("SERVICECFG5:1:0:1:1:0:0:0:1:1", target_device=device_id)
        second = server.queue_command("THRESHOLDS:35:95", target_device=device_id)
        third = server.queue_command("PEER_CHANNEL:6", target_device=device_id)
        replacement_threshold = server.queue_command("THRESHOLDS:40:90", target_device=device_id)

        assert first["command"] == "SERVICECFG5:1:0:1:1:0:0:0:1:1"
        assert second["command"] == "THRESHOLDS:35:95"
        assert third["command"] == "PEER_CHANNEL:6"
        assert replacement_threshold["command"] == "THRESHOLDS:40:90"

        with server.get_db() as db:
            pending_rows = db.execute(
                """
                SELECT id, command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id ASC
                """,
                (device_id,),
            ).fetchall()

        assert [row["command"] for row in pending_rows] == [
            "SERVICECFG5:1:0:1:1:0:0:0:1:1",
            "PEER_CHANNEL:6",
            "THRESHOLDS:40:90",
        ]

        queued = server.peek_queued_command(device_id)
        assert queued["command"] == "SERVICECFG5:1:0:1:1:0:0:0:1:1"
        assert server.acknowledge_queued_command_id(device_id, queued["id"]) is True
        queued = server.peek_queued_command(device_id)
        assert queued["command"] == "PEER_CHANNEL:6"
        assert server.acknowledge_queued_command_id(device_id, queued["id"]) is True
        queued = server.peek_queued_command(device_id)
        assert queued["command"] == "THRESHOLDS:40:90"

        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
            first_row = db.execute(
                "INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)",
                (device_id, "PEER_CHANNEL:1"),
            )
            second_row = db.execute(
                "INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)",
                (device_id, "CONFIG_UPPER:60.0:1000.0"),
            )

        queued = server.peek_queued_command(device_id)
        assert queued["command"] == "PEER_CHANNEL:1"
        assert queued["id"] == first_row.lastrowid

        assert server.acknowledge_queued_command_id(device_id, queued["id"]) is True
        queued = server.peek_queued_command(device_id)
        assert queued["command"] == "CONFIG_UPPER:60.0:1000.0"
        assert queued["id"] == second_row.lastrowid

        with server.get_db() as db:
            pending_rows = db.execute(
                """
                SELECT command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id ASC
                """,
                (device_id,),
            ).fetchall()

        assert [row["command"] for row in pending_rows] == ["CONFIG_UPPER:60.0:1000.0"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))


def test_runtime_sync_does_not_spam_or_use_stale_snapshots():
    device_id = "swt-999-999-999-995"

    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    try:
        stale_snapshot = {
            "device_id": device_id,
            "telemetry_status": "stale",
            "seconds_since_sync": server.STALE_AFTER_SECONDS + 1,
        }
        assert server.build_runtime_sync_command(device_id, snapshot=stale_snapshot) is None

        with server.get_db() as db:
            assert server.runtime_sync_command_allowed(db, device_id) is True
            db.execute(
                "INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)",
                (device_id, "THRESHOLDS:35:95"),
            )
            assert server.runtime_sync_command_allowed(db, device_id) is False

        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
            db.execute(
                """
                INSERT INTO device_command_queue(target_device, command, created_at, delivered_at)
                VALUES (?, ?, datetime('now', '-30 seconds'), datetime('now', '-25 seconds'))
                """,
                (device_id, "SERVICECFG5:1:1:0:1:0:0:0:1:1"),
            )
            assert server.runtime_sync_command_allowed(db, device_id) is False

        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
            db.execute(
                """
                INSERT INTO device_command_queue(target_device, command, created_at, delivered_at)
                VALUES (?, ?, datetime('now', '-700 seconds'), datetime('now', '-690 seconds'))
                """,
                (device_id, "SERVICECFG5:1:1:0:1:0:0:0:1:1"),
            )
            assert server.runtime_sync_command_allowed(db, device_id) is True
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
