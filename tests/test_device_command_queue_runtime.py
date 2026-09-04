from flask_app import server
from datetime import timedelta


def test_named_test_device_credentials_are_included_in_registry(monkeypatch):
    monkeypatch.setenv("SWT_DEVICE_KEYS", "swt-test-000-000-002:old-key")
    monkeypatch.setenv("SWT_TEST_DEVICE_ID", "swt-test-000-000-002")
    monkeypatch.setenv("SWT_TEST_DEVICE_API_KEY", "current-test-key")
    monkeypatch.delenv("DEVICE_KEYS", raising=False)

    registry, source = server.resolve_device_key_registry()

    assert registry.endswith("swt-test-000-000-002:current-test-key")
    assert "SWT_TEST_DEVICE_ID/SWT_TEST_DEVICE_API_KEY" in source


def test_device_key_registry_resolves_bare_and_explicit_env_references(monkeypatch):
    monkeypatch.setenv("SWT_TEST_DEVICE_API_KEY", "resolved-test-key")

    assert server.resolve_device_key_value("SWT_TEST_DEVICE_API_KEY") == "resolved-test-key"
    assert server.resolve_device_key_value("$SWT_TEST_DEVICE_API_KEY") == "resolved-test-key"
    assert server.resolve_device_key_value("${SWT_TEST_DEVICE_API_KEY}") == "resolved-test-key"


def test_motorized_valve_feature_defaults_off_and_persists_independently():
    device_id = "swt-valve-feature-test-001"
    with server.get_db() as db:
        db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))
    try:
        default_config = server.default_device_service_config(device_id)
        assert default_config["municipal_valve_enabled"] is False
        assert default_config["source_outlet_valve_enabled"] is False

        saved = server.upsert_device_service_config(
            device_id,
            municipal_sensor_enabled=True,
            municipal_valve_enabled=True,
            source_outlet_valve_enabled=True,
        )
        assert saved["municipal_sensor_enabled"] is True
        assert saved["municipal_valve_enabled"] is True
        assert saved["source_outlet_valve_enabled"] is True
        assert server.build_device_service_command(saved).startswith("SERVICECFG12:")
        fields = server.build_device_service_command(saved).split(":")
        assert fields[13:15] == ["1", "1"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))


def test_municipal_detection_sensors_are_mutually_exclusive():
    device_id = "swt-municipal-choice-test-001"
    with server.get_db() as db:
        db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))
    try:
        saved = server.upsert_device_service_config(
            device_id,
            municipal_sensor_enabled=True,
            water_flow_sensor_enabled=True,
            water_pressure_sensor_enabled=True,
        )
        assert saved["water_flow_sensor_enabled"] is True
        assert saved["water_pressure_sensor_enabled"] is False
        assert server.build_device_service_command(saved).split(":")[-3:-1] == ["1", "0"]

        saved = server.upsert_device_service_config(
            device_id,
            municipal_sensor_enabled=True,
            water_flow_sensor_enabled=False,
            water_pressure_sensor_enabled=True,
        )
        assert saved["water_flow_sensor_enabled"] is False
        assert saved["water_pressure_sensor_enabled"] is True
        assert server.build_device_service_command(saved).split(":")[-3:-1] == ["0", "1"]

        saved = server.upsert_device_service_config(
            device_id,
            municipal_sensor_enabled=False,
            water_flow_sensor_enabled=True,
            water_pressure_sensor_enabled=True,
        )
        assert saved["water_flow_sensor_enabled"] is False
        assert saved["water_pressure_sensor_enabled"] is False
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))


def test_turbidity_enablement_survives_old_snapshot_and_requeues_servicecfg9(monkeypatch):
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
    assert sync["command"].startswith("SERVICECFG12:")
    fields = sync["command"].split(":")
    assert fields[11:15] == ["1", "1", "0", "0"]


def test_multi_tank_service_defaults_off_and_can_be_enabled_from_flask():
    device_id = "swt-multi-tank-service-001"
    with server.get_db() as db:
        server.ensure_device_multi_tank_configs_table(db)
        db.execute("DELETE FROM device_multi_tank_configs WHERE device_id = ?", (device_id,))
    try:
        assert server.fetch_device_service_config(device_id)["multi_tank_enabled"] is False
        server.set_device_multi_tank_enabled(device_id, True)
        enabled = server.fetch_device_service_config(device_id)
        assert enabled["multi_tank_enabled"] is True
        assert server.build_device_service_command(enabled).startswith("SERVICECFG12:")
        assert server.build_device_service_command(enabled).split(":")[-1] == "1"
        server.set_device_multi_tank_enabled(device_id, False)
        assert server.fetch_device_service_config(device_id)["multi_tank_enabled"] is False
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_multi_tank_configs WHERE device_id = ?", (device_id,))


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


def test_ping_command_jumps_ahead_of_routine_simulator_commands():
    device_id = "swt-ping-priority-test-001"
    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))
    try:
        server.queue_device_command("SIMULATOR_OFF", device_id)
        server.queue_device_command("MUNICIPAL_SIMULATOR_OFF", device_id)
        ping_id = server.queue_device_command("PING_MASTER:492095472", device_id)

        queued = server.peek_queued_command(device_id)

        assert queued["id"] == ping_id
        assert queued["command"] == "PING_MASTER:492095472"
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))


def test_simulator_commands_dedupe_only_their_own_target():
    assert server.device_command_family("SIMULATOR_ON") == "simulator:tank"
    assert server.device_command_family("SIMULATOR_OFF") == "simulator:tank"
    assert server.device_command_family("MUNICIPAL_SIMULATOR_ON") == "simulator:municipal_simulator"
    assert server.device_command_family("MUNICIPAL_VALVE_SIMULATOR_ON") == "simulator:municipal_valve_simulator"
    assert server.device_command_family("LOWER_TURBIDITY_SIMULATOR_ON") == "simulator:lower_turbidity_simulator"
    assert server.device_command_family("UPPER_TURBIDITY_SIMULATOR_ON") == "simulator:upper_turbidity_simulator"


def test_pump_start_commands_remain_available_during_controller_reconnect():
    device_id = "swt-pump-start-queue-001"
    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    try:
        before_queue = server.now_utc()
        server.queue_device_command("ON", device_id)
        with server.get_db() as db:
            row = db.execute(
                "SELECT expires_at FROM device_command_queue WHERE target_device = ?",
                (device_id,),
            ).fetchone()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (device_id,))

    assert row is not None
    expires_at = server.parse_timestamp(row["expires_at"])
    assert expires_at is not None
    assert before_queue + timedelta(minutes=9) <= expires_at <= before_queue + timedelta(minutes=11)


def test_stop_cancels_timed_pump_start_and_normalizes_target_device():
    device_id = "  swt-pump-stop-queue-001  "
    normalized_device_id = device_id.strip()
    with server.get_db() as db:
        db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (normalized_device_id,))

    try:
        server.queue_device_command("ON_FOR:900", device_id)
        server.queue_device_command("OFF", device_id)
        with server.get_db() as db:
            pending_rows = db.execute(
                """
                SELECT target_device, command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id ASC
                """,
                (normalized_device_id,),
            ).fetchall()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (normalized_device_id,))

    assert [(row["target_device"], row["command"]) for row in pending_rows] == [
        (normalized_device_id, "OFF"),
    ]


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
