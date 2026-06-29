from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
TEMPLATE_SOURCE = PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html"


def test_mobile_threshold_route_saves_shared_device_settings():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/api/mobile/device/thresholds", methods=["GET", "POST"])')
    route_source = source[route_start : source.index("\n\nregister_mobile_firmware_routes(", route_start)]

    assert "fetch_device_automation_settings(target_device, snapshot=snapshot)" in route_source
    assert "upsert_device_automation_settings(" in route_source
    assert "build_device_automation_command(updated_settings)" in route_source
    assert '"mobile access required"' in route_source
    assert '"current_saved_config": build_current_saved_config(target_device)' in route_source


def test_device_detail_page_exposes_shared_threshold_settings():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index('def device_detail_page(device_id):')
    function_source = source[function_start : source.index("\n\ndef device_simulator_state_key", function_start)]

    assert "current_saved_config = build_current_saved_config(scoped_device_id, account=account)" in function_source
    assert 'service_config = current_saved_config.get("service_config")' in function_source
    assert 'automation_settings = current_saved_config.get("automation_settings")' in function_source
    assert "automation_settings=automation_settings," in function_source
    assert "current_saved_config=current_saved_config," in function_source


def test_admin_device_detail_template_has_threshold_save_form():
    source = TEMPLATE_SOURCE.read_text(encoding="utf-8")

    assert "Tank Auto Thresholds" in source
    assert "url_for('admin_device_detail_thresholds', device_id=device_id)" in source
    assert "name=\"auto_start_pct\"" in source
    assert "name=\"auto_stop_pct\"" in source
    assert "shared across Flask, Android, and firmware" in source


def test_device_detail_status_and_template_include_live_configuration_grid():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/devices/<device_id>/status")')
    route_source = source[route_start : source.index('\n\n@app.route("/status", methods=["GET", "POST"])', route_start)]

    assert '"service_config": resolve_device_service_config(scoped_device_id, snapshot=snapshot)' in route_source
    assert '"automation_settings": fetch_device_automation_settings(scoped_device_id, snapshot=snapshot)' in route_source
    assert '"current_saved_config": build_current_saved_config(scoped_device_id)' in route_source

    template_source = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    assert "Device Configuration" in template_source
    assert 'id="deviceInfoGrid"' in template_source
    assert "renderDeviceInfo(snapshot,systemStatus,monitoringSummary,serviceConfig,automationSettings,currentSavedConfig);" in template_source


def test_live_service_config_sync_uses_firmware_snapshot_as_source_of_truth():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def snapshot_device_service_config(snapshot, device_id=None, account=None, existing=None):")
    function_source = source[function_start : source.index("\n\ndef resolve_device_service_config", function_start)]

    assert 'snapshot.get("upper_sensor_source")' in function_source
    assert '("lower_tank_service", "source_tank_monitoring_enabled")' in function_source
    assert '("relay_service", "relay_enabled")' in function_source
    assert '("buzzer_service", "buzzer_enabled")' in function_source
    assert '("led_display_service", "led_display_enabled")' in function_source
    assert '("local_firmware_upload_service", "local_firmware_upload_enabled")' in function_source
    assert '("tank_height_cm", "tank_height_cm")' in function_source
    assert '("source_tank_capacity_liters", "lower_tank_capacity_liters")' in function_source
    assert '("telemetry_service", "telemetry_service_state")' in function_source
    assert '("slave_device_service", "slave_device_service_state")' in function_source

    services_route_start = source.index('@app.route("/api/mobile/device/services", methods=["GET", "POST"])')
    services_route_source = source[services_route_start : source.index('\n\n@app.route("/api/mobile/device/thresholds", methods=["GET", "POST"])', services_route_start)]
    assert '"config": fetch_device_service_config(target_device, snapshot=snapshot),' in services_route_source
    assert '"current_saved_config": build_current_saved_config(target_device)' in services_route_source


def test_telemetry_snapshot_persists_live_auto_threshold_fields():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    process_start = source.index("def process_telemetry_payload(data, source_ip=None, transport=\"http\"):")
    process_source = source[process_start : source.index("\n\ndef mysql_connection_config", process_start)]

    assert 'cleaned.get("auto_start_pct")' in process_source
    assert 'cleaned.get("auto_stop_pct")' in process_source
    assert 'cleaned.get("auto_start_stable_ms")' in process_source
    assert 'cleaned.get("auto_level_average_samples")' in process_source
    assert "auto_start_pct, auto_stop_pct," in process_source
    assert "auto_start_stable_ms, auto_level_average_samples," in process_source

    schema_start = source.index("def ensure_tank_data_columns(cursor):")
    schema_source = source[schema_start : source.index("\n\ndef ensure_tank_data_mysql_column_types", schema_start)]
    assert '"auto_start_pct": "REAL"' in schema_source
    assert '"auto_stop_pct": "REAL"' in schema_source
    assert '"auto_start_stable_ms": "INTEGER"' in schema_source
    assert '"auto_level_average_samples": "INTEGER"' in schema_source


def test_device_service_config_table_persists_shared_device_settings_via_upsert():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    table_start = source.index("def ensure_device_service_configs_table(cursor):")
    table_source = source[table_start : source.index("\n\ndef ensure_device_service_configs_columns", table_start)]
    assert "tank_height_cm REAL" in table_source
    assert "tank_capacity_liters REAL" in table_source
    assert "upper_tank_height_cm REAL" in table_source
    assert "lower_tank_height_cm REAL" in table_source
    assert "auto_start_pct REAL" in table_source
    assert "auto_stop_pct REAL" in table_source
    assert "telemetry_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'" in table_source
    assert "slave_device_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'" in table_source

    upsert_start = source.index("def upsert_device_service_config(")
    upsert_source = source[upsert_start : source.index("\n\ndef build_device_service_command", upsert_start)]
    assert "tank_height_cm=None," in upsert_source
    assert "lower_tank_capacity_liters=None," in upsert_source
    assert "local_web_password=None," in upsert_source
    assert "tank_height_cm=excluded.tank_height_cm" in upsert_source
    assert "auto_stop_pct=excluded.auto_stop_pct" in upsert_source
    assert "slave_device_service_state=excluded.slave_device_service_state" in upsert_source
    assert "save_device_local_web_password(normalized_device_id, default_local_web_auth_password())" in upsert_source


def test_threshold_persistence_prefers_device_config_table_and_local_auth_uses_db_password():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    automation_start = source.index("def fetch_device_automation_settings(device_id, snapshot=None):")
    automation_source = source[automation_start : source.index("\n\ndef upsert_device_automation_settings", automation_start)]
    assert 'stored_service_config = fetch_device_service_config(normalized_device_id) if normalized_device_id else {}' in automation_source
    assert '"device_service_config"' in automation_source
    assert "upsert_device_service_config(" in automation_source

    local_status_start = source.index("def fetch_local_device_status(base_url, device_id=None):")
    local_status_source = source[local_status_start : source.index("\n\ndef is_loopback_device_target", local_status_start)]
    assert "password = fetch_device_local_web_password(device_id)" in local_status_source

    saved_config_start = source.index("def build_current_saved_config(device_id, account=None):")
    saved_config_source = source[saved_config_start : source.index("\n\ndef upsert_device_automation_settings", saved_config_start)]
    assert '"configuration_source": "db_upsert"' in saved_config_source
    assert '"service_config": saved_service_config' in saved_config_source
    assert '"automation_settings": saved_automation_settings' in saved_config_source


def test_sensor_configuration_routes_upsert_expected_tank_dimensions():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    mobile_route_start = source.index('def mobile_sensor_configure():')
    mobile_route_source = source[mobile_route_start : source.index('\n\n@app.route("/api/mobile/simulator"', mobile_route_start)]
    assert "upsert_device_service_config(" in mobile_route_source
    assert "upper_tank_height_cm=height_cm" in mobile_route_source
    assert "upper_tank_capacity_liters=capacity_liters" in mobile_route_source

    admin_route_start = source.index("def admin_device_detail_sensor_configure(device_id):")
    admin_route_source = source[admin_route_start : source.index('\n\n@app.route("/admin/customers/<device_id>/simulator"', admin_route_start)]
    assert "lower_tank_height_cm=height_cm" in admin_route_source
    assert "lower_tank_capacity_liters=capacity_liters" in admin_route_source
    assert "upper_tank_capacity_liters=capacity_liters" in admin_route_source
