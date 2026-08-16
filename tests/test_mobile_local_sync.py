from __future__ import annotations

from pathlib import Path

from flask_app import server


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
ANDROID_SOURCE = (
    PROJECT_ROOT.parent
    / "swt_android_app_project"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "smartwatertank"
    / "app"
    / "MainActivity.kt"
)
ANDROID_STRINGS_SOURCE = (
    PROJECT_ROOT.parent
    / "swt_android_app_project"
    / "app"
    / "src"
    / "main"
    / "res"
    / "values"
    / "strings.xml"
)
DEVICE_DETAIL_TEMPLATE_SOURCE = PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html"


def test_mobile_local_sync_route_exists_for_android_bridge():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    android_source = ANDROID_SOURCE.read_text(encoding="utf-8")

    assert '@app.route("/api/mobile/local-sync", methods=["POST"])' in server_source
    assert "def mobile_local_sync():" in server_source
    assert '"api/mobile/local-sync"' in android_source


def test_mobile_local_sync_preserves_scope_and_transport_markers():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "current_mobile_scope_device_id" in server_source
    assert '"device_id does not match authenticated device"' in server_source
    assert 'source_ip="android_local_wifi"' in server_source
    assert 'transport="android_local_wifi"' in server_source


def test_mobile_local_sync_defers_database_heavy_postprocessing():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = server_source.index("def mobile_local_sync():")
    route_body = server_source[route_start : server_source.index("\n\n@app.route", route_start)]

    assert "defer_postprocess=True" in route_body
    assert "refresh_operational_alerts(" not in route_body


def test_telemetry_worker_debounces_before_taking_permit_and_uses_cross_process_lease():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    worker_start = server_source.index("def schedule_telemetry_postprocess(")
    worker_body = server_source[worker_start : server_source.index("\n\ndef process_telemetry_payload", worker_start)]

    assert worker_body.index("time.sleep(remaining)") < worker_body.index(
        "telemetry_background_semaphore.acquire"
    )
    assert "fcntl.LOCK_EX | fcntl.LOCK_NB" in worker_body
    assert "fcntl.LOCK_UN" in worker_body
    assert "lease_db = get_db()" not in worker_body
    assert "telemetry_postprocess_pending[normalized_device_id] = pending" in worker_body


def test_android_local_sync_duplicate_payload_is_deduplicated():
    device_id = "swt-android-sync-dedupe-001"
    payload = {
        "device_id": device_id,
        "device_source": server.DEVICE_SOURCE_REAL,
        "level": 68.5,
        "motor": "OFF",
        "mode": "AUTO",
        "sensor": "OK",
        "wifi": "ONLINE",
        "firmware_version": "26.1.642",
        "tank_capacity_liters": 1000.0,
        "lower_tank_level": 54.0,
        "telemetry_service": "ON",
        "command_service": "ON",
        "ota_service": "ON",
        "lower_tank_service": "ON",
        "buzzer_service": "ON",
        "led_display_service": "ON",
        "local_firmware_upload_service": "ON",
        "arch_id": "arch-1",
        "node_role": "master",
        "device_type": "master",
    }

    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    try:
        first = server.process_telemetry_payload(
            dict(payload),
            source_ip="android_local_wifi",
            transport="android_local_wifi",
        )
        second = server.process_telemetry_payload(
            dict(payload),
            source_ip="android_local_wifi",
            transport="android_local_wifi",
        )
        with server.get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) AS count FROM tank_data WHERE device_id = ?",
                (device_id,),
            ).fetchone()
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    assert first.get("_telemetry_sync_result") == "saved"
    assert second.get("_telemetry_sync_result") == "duplicate"
    assert int(row["count"]) == 1


def test_simulator_flags_survive_telemetry_snapshot_refresh():
    device_id = "swt-simulator-state-001"
    payload = {
        "device_id": device_id,
        "device_source": server.DEVICE_SOURCE_REAL,
        "level": 50.0,
        "motor": "OFF",
        "mode": "AUTO",
        "sensor": "OK",
        "municipal_valve_simulated": True,
        "source_outlet_valve_simulated": True,
        "source_pump_fill_feature_enabled": True,
        "lower_turbidity_simulated": True,
        "upper_turbidity_simulated": True,
    }

    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    try:
        server.process_telemetry_payload(payload, source_ip="test", transport="http")
        snapshot = server.fetch_device_snapshot(device_id)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_events WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))

    assert snapshot is not None
    assert bool(snapshot["municipal_valve_simulated"])
    assert bool(snapshot["source_outlet_valve_simulated"])
    assert bool(snapshot["source_pump_fill_feature_enabled"])
    assert bool(snapshot["lower_turbidity_simulated"])
    assert bool(snapshot["upper_turbidity_simulated"])


def test_snapshot_uses_saved_source_capacity_when_history_has_no_capacity_column():
    device_id = "swt-source-capacity-snapshot-001"
    with server.get_db() as db:
        db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
        db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))

    try:
        server.upsert_device_service_config(
            device_id,
            lower_tank_capacity_liters=1000.0,
        )
        server.process_telemetry_payload(
            {
                "device_id": device_id,
                "device_source": server.DEVICE_SOURCE_REAL,
                "level": 50.0,
                "lower_tank_level": 100.0,
                "motor": "OFF",
                "mode": "AUTO",
                "sensor": "OK",
            },
            source_ip="test",
            transport="http",
        )
        snapshot = server.fetch_device_snapshot(device_id)
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_service_configs WHERE device_id = ?", (device_id,))

    assert snapshot is not None
    assert snapshot["source_tank_capacity_liters"] == 1000.0
    assert snapshot["lower_water_available_label"] == "1000.0 L / 1000.0 L"


def test_mobile_simulator_route_queues_firmware_simulator_commands():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert '@app.route("/api/mobile/simulator", methods=["POST"])' in server_source
    assert '@app.route("/api/mobile/device/simulator", methods=["POST"])' in server_source
    assert "def resolve_simulator_command(payload):" in server_source
    assert '"all": "SIMULATOR"' in server_source
    assert '"upper": "SIMULATOR"' in server_source
    assert '"source": "SIMULATOR"' in server_source
    assert 'f"{command_prefix}_{state_suffix}"' in server_source


def test_simulator_state_prefers_live_telemetry_over_cached_admin_request():
    device_id = "swt-simulator-cache-test-001"
    state_key = server.device_simulator_state_key(device_id)
    try:
        server.record_device_simulator_state(device_id, True, source="admin_command")

        assert server.device_simulator_enabled(
            device_id,
            snapshot={"telemetry_status": "live", "simulator": "OFF"},
        ) is False
        assert server.device_simulator_enabled(
            device_id,
            snapshot={"telemetry_status": "live", "upper_tank_simulator": "ON"},
        ) is True
        assert server.device_simulator_enabled(
            device_id,
            snapshot={"telemetry_status": "no-data", "simulator": "OFF"},
        ) is False
        assert server.enrich_snapshot({"device_id": device_id, "level": 50}).get("simulator") == "OFF"

        server.record_device_simulator_state(device_id, True, source="http")
        assert server.enrich_snapshot({"device_id": device_id, "level": 50}).get("simulator") == "ON"
    finally:
        if state_key:
            server.delete_app_setting(state_key)


def test_device_detail_simulator_ui_prefers_live_status_over_redirect_override():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    template_source = DEVICE_DETAIL_TEMPLATE_SOURCE.read_text(encoding="utf-8")

    page_start = server_source.index("def device_detail_page(device_id):")
    page_body = server_source[page_start : server_source.index("\n\ndef device_simulator_state_key", page_start)]
    simulator_start = template_source.index("function simulatorActive(snapshot)")
    simulator_body = template_source[simulator_start : template_source.index("function simulatorStatusText", simulator_start)]

    assert "live_simulator_status = simulator_payload_status(snapshot)" in page_body
    assert "live_simulator_status is None" in page_body
    assert 'str(snapshot.get("telemetry_status") or "").strip().lower() == "no-data"' in page_body
    assert "load_device_simulator_state(data.get(\"device_id\"))" in server_source
    assert "source=\"admin_command\"" not in server_source[server_source.index("def admin_device_detail_simulator") : server_source.index("\n\n@app.route(\"/devices/<device_id>/status\")")]
    assert "function simulatorSnapshotStatus(snapshot)" in template_source
    assert "if(typeof snapshot?.simulator_enabled===\"boolean\")return snapshot.simulator_enabled;" in simulator_body
    assert "if(liveStatus!==null&&telemetryStatus!==\"no-data\")return liveStatus;" in simulator_body
    assert simulator_body.index("if(liveStatus!==null&&telemetryStatus!==\"no-data\")") < simulator_body.index("const override=")


def test_mobile_bootstrap_can_trigger_android_firmware_upgrade():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    android_source = ANDROID_SOURCE.read_text(encoding="utf-8")

    assert "device_mobile_action_queue" in server_source
    assert 'CREATE TABLE IF NOT EXISTS device_mobile_action_queue' in server_source
    assert "idx_device_mobile_action_queue_target_pending" in server_source
    assert '"mobile_action": pop_device_mobile_action(scoped_device_id)' in server_source
    assert '@app.route("/devices/<device_id>/mobile/firmware-upgrade", methods=["POST"])' in server_source
    assert "def admin_device_detail_mobile_firmware_upgrade(device_id):" in server_source
    assert '"message": "Flask requested a firmware upgrade."' in server_source
    assert "scheduleRemoteFirmwareUpgradeIfRequested(payload)" in android_source
    assert 'optJSONObject("mobile_action")' in android_source
    assert 'optString("message")' in android_source
    assert "local_firmware_upgrade_requested_from_flask" in android_source
    assert "startAutomaticLocalFirmwareUpgrade(triggeredByFlask = true)" in android_source
    assert '"START_FIRMWARE_UPGRADE"' in android_source


def test_android_firmware_upgrade_action_is_queued_with_a_complete_result():
    device_id = "swt-test-mobile-ota-queue-001"
    with server.get_db() as db:
        db.execute("DELETE FROM device_mobile_action_queue WHERE target_device = ?", (device_id,))

    try:
        result = server.queue_device_mobile_action(
            server.MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE,
            device_id,
            payload={"source": "device_detail"},
        )

        assert not isinstance(result, tuple)
        assert result["status"] == "queued"
        assert result["action"] == server.MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE
        assert result["target_device"] == device_id
        assert isinstance(result["queue_id"], int)
        with server.get_db() as db:
            queued = db.execute(
                "SELECT action, payload_json FROM device_mobile_action_queue WHERE id = ?",
                (result["queue_id"],),
            ).fetchone()
        assert queued is not None
        assert queued["action"] == server.MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE
        assert '"source":"device_detail"' in queued["payload_json"]
    finally:
        with server.get_db() as db:
            db.execute("DELETE FROM device_mobile_action_queue WHERE target_device = ?", (device_id,))


def test_mobile_bootstrap_returns_fast_cloud_and_ai_payload_for_android():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = server_source.index("def mobile_bootstrap():")
    route_body = server_source[route_start : server_source.index('\n\n@app.route("/api/mobile/analytics")', route_start)]

    assert 'include_analytics = str(request.args.get("include_analytics", "0"))' in route_body
    assert "load_persisted_dashboard_summary(scoped_device_id)" in route_body
    assert '"events": list(summary.get("events") or [])[:event_limit]' in route_body
    assert '"audit": list(summary.get("audit") or [])[:audit_limit]' in route_body
    assert "build_monitoring_summary_payload(" not in route_body
    assert "fetch_audit_events(" not in route_body
    assert "if include_analytics and current_customer_ai_analysis_enabled()" in route_body
    assert 'payload["analytics"] = build_dashboard_analytics(start_dt, end_exclusive, label, device_id=scoped_device_id)' in route_body
    assert 'payload["analytics"] = build_analytics_fallback_payload(' in route_body


def test_mobile_pump_slider_requires_hold_confirmation_before_sending_command():
    android_source = ANDROID_SOURCE.read_text(encoding="utf-8")
    strings_source = ANDROID_STRINGS_SOURCE.read_text(encoding="utf-8")

    assert "pumpCommandHoldAnimator" in android_source
    assert "pumpCommandHoldAction" in android_source
    assert "pumpSliderConfirmProgress" in android_source
    assert "startPumpCommandHoldConfirmation(renderedPumpButtonAction)" in android_source
    assert "startPumpCommandHoldConfirmation(DeviceAction.OFF)" in android_source
    assert "PUMP_COMMAND_CONFIRM_DELAY_MS = 2000L" in android_source
    assert "PUMP_SLIDER_COMPLETE_PROGRESS = 0.95f" in android_source
    assert "pump_hold_confirm_start" in strings_source
    assert "pump_hold_confirm_stop" in strings_source
    assert "pump_command_starting" in strings_source
    assert "pump_command_stopping" in strings_source


def test_dashboard_local_sync_route_polls_private_lan_device():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    dashboard_source = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert '@app.route("/dashboard/local-sync", methods=["POST"])' in server_source
    assert "def fetch_local_device_status" in server_source
    assert "is_private_device_base_url" in server_source
    assert 'host.endswith(".local") or host.endswith(".lan")' in server_source
    assert "local device_id does not match requested device" in server_source
    assert 'source_ip="dashboard_local_wifi"' in server_source
    assert "syncLocalDashboardSnapshot" in dashboard_source
    assert '`${API}/dashboard/local-sync`' in dashboard_source


def test_local_device_status_uses_device_key_before_local_web_password():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = server_source.index("def fetch_local_device_status(")
    function_body = server_source[function_start : server_source.index("def mobile_local_sync()", function_start)]
    auth_block = function_body[function_body.index("username = ") : function_body.index("timeout = ", function_body.index("username = "))]

    env_username = 'os.environ.get("SWT_LOCAL_WEB_AUTH_USERNAME", "").strip()'

    assert "device_key = configured_device_key_for_id(normalized_device_id)" in function_body
    assert '"X-Device-Id": normalized_device_id' in function_body
    assert "response = requests.get(status_url, headers=headers, timeout=timeout)" in function_body
    assert "response = requests.get(status_url, headers=headers, auth=auth, timeout=timeout)" in function_body
    assert env_username in auth_block
    assert 'or "swtadmin"' in auth_block
    assert "normalize_device_id(device_id)" not in auth_block


def test_cloud_status_ingest_defers_slow_postprocess_work():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = server_source.index('def status():')
    route_body = server_source[route_start : server_source.index('\n\n@app.route("/device/status"', route_start)]
    process_start = server_source.index("def process_telemetry_payload(")
    process_body = server_source[process_start : server_source.index("\n\ndef mysql_connection_config", process_start)]

    assert "def postprocess_telemetry_payload(" in server_source
    assert "def schedule_telemetry_postprocess(" in server_source
    assert "defer_postprocess=False" in process_body
    assert "defer_postprocess=True" in route_body
    assert "schedule_telemetry_postprocess(cleaned, raw_firmware_logs, source_ip, transport, latest_row_id)" in process_body
    assert "threading.Thread(" in server_source


def test_empty_cloud_analytics_do_not_emit_zero_liter_predictions():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = server_source.index("def build_empty_analytics(")
    function_body = server_source[function_start : server_source.index("\n\ndef meaningful_forecast_hours", function_start)]

    assert '"avg_daily_usage": None' in function_body
    assert '"tomorrow_usage": None' in function_body
    assert '"status": "insufficient_data"' in function_body
    assert '"daily": {"dates": daily_dates, "values": daily_values}' in function_body
    assert '"Live snapshot is available for {normalized_device_id}; more history is needed for forecasts."' in function_body


def test_blank_relay_env_values_explicitly_clear_runtime_relay_config():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "CLEARABLE_DEVICE_ENV_KEYS" in server_source
    assert '"RELAY_STATUS_URLS"' in server_source
    assert '"RELAY_COMMAND_URLS"' in server_source
    assert "key in CLEARABLE_DEVICE_ENV_KEYS" in server_source


def test_local_flask_can_auto_relay_to_shared_cloud_without_explicit_relay_urls():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "AUTO_RELAY_LOCAL_TO_SHARED_CLOUD" in server_source
    assert "should_auto_relay_local_request_to_shared_cloud" in server_source
    assert "host_is_private_or_local" in server_source
    assert 'relay_urls_for_current_request(RELAY_STATUS_URL_LIST, "/status")' in server_source


def test_cloud_ingestion_accepts_firmware_device_ip_url():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "def apply_device_status_aliases(cleaned):" in server_source
    assert 'cleaned.get("device_ip_url")' in server_source
    assert 'cleaned["device_local_url"] = cleaned.get("device_ip_url")' in server_source
    assert 'cleaned["level"] = cleaned.get("main_tank_level")' in server_source
    assert 'relay_state_label(cleaned.get("relay_on"))' in server_source
    assert 'relay_state_label(cleaned.get("relay"))' in server_source
    assert 'cleaned["motor"] = pump_state' in server_source
    assert 'pump_state = relay_state_label(cleaned.get("pump"))' in server_source
    assert 'cleaned["sensor"] = cleaned.get("upper_sensor")' in server_source


def test_registered_device_key_can_recover_from_stale_wildcard_key():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")

    assert "def registered_device_auth_rule_matches(device_id, device_key):" in server_source
    assert "Admin registration is the authoritative per-device credential" in server_source
    assert "registered_rule = registered_device_auth_rule_matches(normalized_device_id, device_key)" in server_source
    assert "matched_rule = registered_rule" in server_source


def test_admin_templates_do_not_show_public_source_ip_as_local_ip():
    admin_source = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    detail_source = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert "{% elif device.source_ip %}" not in admin_source
    assert "snapshot.device_local_url||snapshot.source_ip" not in detail_source
