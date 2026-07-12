from flask_app import server


def count_device_rows(db, table_name, column_name, device_id):
    row = db.execute(
        f"SELECT COUNT(*) AS count FROM {server.quote_mysql_identifier(table_name)} "
        f"WHERE {server.quote_mysql_identifier(column_name)} = ?",
        (device_id,),
    ).fetchone()
    return int(row["count"] or 0)


def test_purge_device_app_settings_uses_quoted_key_identifier(monkeypatch):
    class FakeCursor:
        def __init__(self):
            self.executed_sql = []

        def execute(self, sql, params=None):
            self.executed_sql.append((sql, params))
            return type("Result", (), {"rowcount": 1})()

    cursor = FakeCursor()
    monkeypatch.setattr(
        server,
        "device_scoped_app_setting_keys",
        lambda device_id: ["device-setting", f"{server.ANALYTICS_LAST_VALID_SETTING_PREFIX}{device_id}:%"],
    )

    server.purge_device_app_settings(cursor, "swt-purge-app-settings-001")

    assert any(sql == "DELETE FROM app_settings WHERE `key` = ?" for sql, _ in cursor.executed_sql)
    assert any(sql == "DELETE FROM app_settings WHERE `key` LIKE ?" for sql, _ in cursor.executed_sql)


def test_purge_device_app_settings_retries_transient_deadlock(monkeypatch):
    class FlakyCursor:
        def __init__(self):
            self.calls = 0

        def execute(self, sql, params=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")
            return type("Result", (), {"rowcount": 1})()

    cursor = FlakyCursor()
    monkeypatch.setattr(
        server,
        "device_scoped_app_setting_keys",
        lambda device_id: ["device-setting"],
    )

    deleted_rows = server.purge_device_app_settings(cursor, "swt-purge-app-settings-retry")

    assert deleted_rows == 2
    assert cursor.calls == 3


def test_purge_device_data_removes_device_scoped_tables_and_settings():
    device_id = "swt-purge-device-001"
    now = server.now_utc().strftime(server.TIMESTAMP_FORMAT)
    app_setting_keys = server.device_scoped_app_setting_keys(device_id)

    server.purge_device_data(device_id, remember_deleted_device=False)
    for setting_key in app_setting_keys:
        server.set_app_setting(setting_key, "purge-test")

    try:
        with server.get_db() as db:
            db.execute(
                """
                INSERT INTO customer_accounts(device_id, display_name, password_hash)
                VALUES (?, ?, ?)
                """,
                (device_id, "Purge Test", "test-password-hash"),
            )
            db.execute(
                """
                INSERT INTO registered_devices(device_id, registration_source)
                VALUES (?, ?)
                """,
                (device_id, "purge_test"),
            )
            db.execute(
                """
                INSERT INTO device_auth_keys(device_id, device_key_hash, registration_source)
                VALUES (?, ?, ?)
                """,
                (device_id, "hash", "purge_test"),
            )
            db.execute("INSERT INTO device_service_configs(device_id) VALUES (?)", (device_id,))
            db.execute(
                """
                INSERT INTO customer_password_reset_tokens(device_id, token_hash, expires_at)
                VALUES (?, ?, ?)
                """,
                (device_id, "purge-token-hash-001", now),
            )
            db.execute(
                """
                INSERT INTO tank_data(device_id, device_source, level, motor, mode, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (device_id, server.DEVICE_SOURCE_REAL, 42, "OFF", "AUTO", now),
            )
            db.execute(
                """
                INSERT INTO device_command_queue(target_device, command)
                VALUES (?, ?)
                """,
                (device_id, "PING"),
            )
            db.execute(
                """
                INSERT INTO device_mobile_action_queue(target_device, action)
                VALUES (?, ?)
                """,
                (device_id, "refresh"),
            )
            db.execute(
                """
                INSERT INTO relay_queue(payload, next_attempt_at)
                VALUES (?, CURRENT_TIMESTAMP)
                """,
                (server.json.dumps({"device_id": device_id, "level": 42}, separators=(",", ":")),),
            )
            db.execute(
                """
                INSERT INTO firmware_artifacts(
                    target_device, target_role, original_filename, stored_filename, md5, size_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (device_id, "master", "firmware.bin", "stored.bin", "abc123", 12),
            )
            db.execute(
                """
                INSERT INTO ops_alerts(device_id, kind, severity, message)
                VALUES (?, ?, ?, ?)
                """,
                (device_id, "purge_test", "warning", "purge test alert"),
            )
            db.execute(
                """
                INSERT INTO ops_audit_log(actor, action, target_type, target_id, device_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test", "device_scoped", "device", device_id, device_id, "{}"),
            )
            db.execute(
                """
                INSERT INTO ops_audit_log(actor, action, target_type, target_id, device_id, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test", "target_scoped", "device", device_id, None, "{}"),
            )
            db.execute(
                """
                INSERT INTO device_events(
                    event_key, device_id, event_kind, severity, message, event_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("purge-event-key-001", device_id, "purge_test", "info", "purge test event", now),
            )
            db.execute(
                """
                INSERT INTO ignored_devices(device_id, note)
                VALUES (?, ?)
                """,
                (device_id, "purge_test"),
            )

        deleted_counts = server.purge_device_data(device_id, remember_deleted_device=False)

        assert deleted_counts["customer_accounts"] >= 1
        assert deleted_counts["registered_devices"] >= 1
        assert deleted_counts["device_auth_keys"] >= 1
        assert deleted_counts["device_service_configs"] >= 1
        assert deleted_counts["customer_password_reset_tokens"] >= 1
        assert deleted_counts["tank_data"] >= 1
        assert deleted_counts["device_command_queue"] >= 1
        assert deleted_counts["device_mobile_action_queue"] >= 1
        assert deleted_counts["relay_queue"] >= 1
        assert deleted_counts["firmware_artifacts"] >= 1
        assert deleted_counts["ops_alerts"] >= 1
        assert deleted_counts["ops_audit_log"] >= 2
        assert deleted_counts["device_events"] >= 1
        assert deleted_counts["ignored_devices"] >= 1
        assert deleted_counts["app_settings"] == len(app_setting_keys)

        with server.get_db() as db:
            assert count_device_rows(db, "customer_accounts", "device_id", device_id) == 0
            assert count_device_rows(db, "registered_devices", "device_id", device_id) == 0
            assert count_device_rows(db, "device_auth_keys", "device_id", device_id) == 0
            assert count_device_rows(db, "device_service_configs", "device_id", device_id) == 0
            assert count_device_rows(db, "customer_password_reset_tokens", "device_id", device_id) == 0
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0
            assert count_device_rows(db, "device_command_queue", "target_device", device_id) == 0
            assert count_device_rows(db, "device_mobile_action_queue", "target_device", device_id) == 0
            relay_row = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM relay_queue
                WHERE payload LIKE ? ESCAPE '='
                """,
                (f'%"device_id":"{server.sql_like_escape(device_id)}"%',),
            ).fetchone()
            assert int(relay_row["count"] or 0) == 0
            assert count_device_rows(db, "firmware_artifacts", "target_device", device_id) == 0
            assert count_device_rows(db, "ops_alerts", "device_id", device_id) == 0
            assert count_device_rows(db, "device_events", "device_id", device_id) == 0
            assert count_device_rows(db, "ignored_devices", "device_id", device_id) == 0
            audit_row = db.execute(
                """
                SELECT COUNT(*) AS count
                FROM ops_audit_log
                WHERE device_id = ?
                   OR (target_type = 'device' AND target_id = ?)
                """,
                (device_id, device_id),
            ).fetchone()
            assert int(audit_row["count"] or 0) == 0
            placeholders = ",".join("?" for _ in app_setting_keys)
            settings_row = db.execute(
                f"SELECT COUNT(*) AS count FROM app_settings WHERE `key` IN ({placeholders})",
                tuple(app_setting_keys),
            ).fetchone()
            assert int(settings_row["count"] or 0) == 0
    finally:
        server.purge_device_data(device_id, remember_deleted_device=False)


def test_delete_known_device_keeps_marker_and_ignored_telemetry_does_not_recreate_rows():
    device_id = "swt-purge-ignore-001"
    server.purge_device_data(device_id, remember_deleted_device=False)

    try:
        with server.get_db() as db:
            db.execute(
                """
                INSERT INTO tank_data(device_id, device_source, level, motor, mode, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (device_id, server.DEVICE_SOURCE_REAL, 55, "OFF", "AUTO"),
            )

        deleted_counts = server.delete_known_device(device_id)
        assert deleted_counts["tank_data"] >= 1

        with server.get_db() as db:
            assert count_device_rows(db, "ignored_devices", "device_id", device_id) == 1
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0

        result = server.process_telemetry_payload(
            {
                "device_id": device_id,
                "device_source": server.DEVICE_SOURCE_REAL,
                "level": 64,
                "motor": "OFF",
                "mode": "AUTO",
            }
        )

        assert result["_telemetry_sync_result"] == "ignored"
        with server.get_db() as db:
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0
            assert count_device_rows(db, "device_events", "device_id", device_id) == 0
    finally:
        server.purge_device_data(device_id, remember_deleted_device=False)


def test_purge_device_table_rows_retries_transient_deadlock(monkeypatch):
    device_id = "swt-purge-row-retry-001"
    server.purge_device_data(device_id, remember_deleted_device=False)
    original_table_column_names = server.table_column_names
    state = {"raised": False}

    def flaky_table_column_names(cursor, table_name):
        if table_name == "tank_data" and not state["raised"]:
            state["raised"] = True
            raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")
        return original_table_column_names(cursor, table_name)

    try:
        with server.get_db() as db:
            db.execute(
                """
                INSERT INTO tank_data(device_id, device_source, level, motor, mode, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (device_id, server.DEVICE_SOURCE_REAL, 71, "OFF", "AUTO"),
            )

        monkeypatch.setattr(server, "table_column_names", flaky_table_column_names)
        with server.get_db() as db:
            deleted_rows = server.purge_device_table_rows(db.cursor(), "tank_data", device_id)

        assert state["raised"] is True
        assert deleted_rows >= 1
        with server.get_db() as db:
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0
    finally:
        monkeypatch.setattr(server, "table_column_names", original_table_column_names)
        server.purge_device_data(device_id, remember_deleted_device=False)


def test_delete_known_device_falls_back_when_purge_keep_deadlocking(monkeypatch):
    device_id = "swt-purge-fallback-001"
    server.purge_device_data(device_id, remember_deleted_device=False)
    original_purge = server.purge_device_table_rows
    original_app_settings = server.purge_device_app_settings

    def always_deadlock(cursor, table_name, normalized_device_id):
        raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")

    def always_deadlock_app_settings(cursor, device_id):
        raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")

    try:
        with server.get_db() as db:
            db.execute(
                """
                INSERT INTO tank_data(device_id, device_source, level, motor, mode, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (device_id, server.DEVICE_SOURCE_REAL, 71, "OFF", "AUTO"),
            )

        monkeypatch.setattr(server, "purge_device_table_rows", always_deadlock)
        monkeypatch.setattr(server, "purge_device_app_settings", always_deadlock_app_settings)
        deleted_counts = server.delete_known_device(device_id)

        assert deleted_counts["tank_data"] >= 1
        with server.get_db() as db:
            assert count_device_rows(db, "ignored_devices", "device_id", device_id) == 1
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0
    finally:
        monkeypatch.setattr(server, "purge_device_table_rows", original_purge)
        monkeypatch.setattr(server, "purge_device_app_settings", original_app_settings)
        server.purge_device_data(device_id, remember_deleted_device=False)


def test_delete_known_device_retries_transient_database_deadlock(monkeypatch):
    device_id = "swt-purge-retry-001"
    server.purge_device_data(device_id, remember_deleted_device=False)
    original_purge = server.purge_device_table_rows
    state = {"raised": False}

    def flaky_purge(cursor, table_name, normalized_device_id):
        if normalized_device_id == device_id and not state["raised"]:
            state["raised"] = True
            raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")
        return original_purge(cursor, table_name, normalized_device_id)

    try:
        with server.get_db() as db:
            db.execute(
                """
                INSERT INTO tank_data(device_id, device_source, level, motor, mode, created_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (device_id, server.DEVICE_SOURCE_REAL, 71, "OFF", "AUTO"),
            )

        monkeypatch.setattr(server, "purge_device_table_rows", flaky_purge)
        deleted_counts = server.delete_known_device(device_id)

        assert state["raised"] is True
        assert deleted_counts["tank_data"] >= 1
        with server.get_db() as db:
            assert count_device_rows(db, "tank_data", "device_id", device_id) == 0
            assert count_device_rows(db, "ignored_devices", "device_id", device_id) == 1
    finally:
        monkeypatch.setattr(server, "purge_device_table_rows", original_purge)
        server.purge_device_data(device_id, remember_deleted_device=False)


def test_delete_known_device_marks_ignored_before_table_purge():
    server_source = (server.PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "attempts=6" in server_source
    assert "initial_delay_s=0.5" in server_source
    assert "if remember_deleted_device:" in server_source
    assert "add_deleted_device_marker(db.cursor(), normalized_device_id)" in server_source
    assert 'if remember_deleted_device and table_name == "ignored_devices":' in server_source
    assert "check-ins are rejected instead of racing with row deletion" in server_source
