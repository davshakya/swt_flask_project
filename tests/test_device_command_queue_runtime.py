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
