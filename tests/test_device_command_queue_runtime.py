from flask_app import server


def test_device_command_queue_serves_latest_pending_command_and_prunes_stale_rows():
    device_id = "swt-999-999-999-996"

    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    try:
        first = server.queue_command("SERVICECFG5:1:0:1:1:0:0:0:1:1", target_device=device_id)
        second = server.queue_command("THRESHOLDS:35:95", target_device=device_id)

        assert first["command"] == "SERVICECFG5:1:0:1:1:0:0:0:1:1"
        assert second["command"] == "THRESHOLDS:35:95"

        with server.get_db() as db:
            pending_rows = db.execute(
                """
                SELECT command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id DESC
                """,
                (device_id,),
            ).fetchall()

        assert [row["command"] for row in pending_rows] == ["THRESHOLDS:35:95"]

        with server.get_db() as db:
            db.execute(
                "INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)",
                (device_id, "PEER_CHANNEL:1"),
            )
            db.execute(
                "INSERT INTO device_command_queue(target_device, command) VALUES (?, ?)",
                (device_id, "CONFIG_UPPER:60.0:1000.0"),
            )

        queued = server.peek_queued_command(device_id)

        assert queued["command"] == "CONFIG_UPPER:60.0:1000.0"

        with server.get_db() as db:
            pending_rows = db.execute(
                """
                SELECT command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id DESC
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
