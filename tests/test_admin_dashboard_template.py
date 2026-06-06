from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_admin_customer_page_renders_one_popup_status_message_slot():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert template.count('id="upload_result_panel"') == 1
    assert template.count("data-upload-result-message") == 2
    assert "data-auto-open-panel" not in template


def test_landing_page_uses_compressed_responsive_marketing_images():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert 'rel="preload" as="image" type="image/webp"' in template
    assert "smart-water-tank-hero-ai-1280.webp" in template
    assert template.count("<source type=\"image/webp\"") >= 7
    assert template.count('decoding="async"') >= 7
    assert template.count('width="1536" height="1024"') >= 7


def test_booking_form_requires_typed_client_side_validation():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert "const enquiryFieldRules = {" in template
    assert 'pattern="[A-Za-z][A-Za-z .\'\\-]{1,79}"' in template
    assert 'pattern="\\+?[0-9][0-9 ()\\-]{8,18}[0-9]"' in template
    assert 'id="lead_email" name="email" type="email"' in template
    assert 'autocomplete="email" maxlength="120"' in template
    assert 'id="lead_device_count" name="device_count" type="number" min="1" max="10000" step="1" inputmode="numeric"' in template
    assert 'textarea id="lead_message" name="message" minlength="10" maxlength="800"' in template
    assert "field.setCustomValidity(message)" in template
    assert "enquirySubmitButton.disabled = !isReady" in template
    assert 'class="field-warning" id="lead_phone_warning"' in template


def test_flask_static_assets_have_cache_and_compression_support():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    service_worker = (PROJECT_ROOT / "flask_app" / "static" / "service-worker.js").read_text(encoding="utf-8")

    assert 'app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)' in server_source
    assert '"Cache-Control", "public, max-age=2592000, immutable"' in server_source
    assert "def should_gzip_response(response):" in server_source
    assert "gzip.compress(payload, compresslevel=6)" in server_source
    assert 'const CACHE_NAME = "swt-pwa-v5";' in service_worker
    assert "/static/marketing/smart-water-tank-hero-ai-1280.webp" in service_worker


def test_sales_enquiry_server_validation_matches_booking_form_rules():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'valid_segments = {' in server_source
    assert 're.fullmatch(r"[A-Za-z][A-Za-z .\'-]*", cleaned["name"])' in server_source
    assert 'len(cleaned["email"]) > 120' in server_source
    assert 're.fullmatch(r"\\+?[0-9][0-9 ()-]*[0-9]", cleaned["phone"])' in server_source
    assert 'len(phone_digits) > 15' in server_source
    assert 'cleaned["segment"] not in valid_segments' in server_source
    assert 'len(cleaned["message"]) < 10' in server_source


def test_admin_customer_page_uses_single_relay_alert_cleanup_hook():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def resolve_transient_relay_alerts" in server_source
    assert "message LIKE 'Cloud relay returned HTTP 5%'" in server_source
    assert "set_alert(\"relay_failure\", \"warning\", f\"Cloud relay returned HTTP {response.status_code}.\", active=True)" not in server_source


def test_admin_device_list_reconciles_alerts_from_current_feed():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def refresh_admin_entry_alerts" in server_source
    assert "refresh_admin_entry_alerts(entry)" in server_source
    refresh_call = server_source.index("refresh_admin_entry_alerts(entry)")
    summaries_call = server_source.index("fetch_active_alert_summaries(", refresh_call)
    assert refresh_call < summaries_call


def test_admin_dashboard_hides_alerts_older_than_24_hours():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def admin_alert_cutoff_timestamp(hours=24)" in server_source
    assert "def resolve_admin_expired_alerts" in server_source
    assert "WHERE active = 1" in server_source
    assert "AND updated_at < ?" in server_source
    assert "fetch_filtered_alerts(limit=10, updated_since=alert_cutoff)" in server_source
    assert "fetch_active_alert_device_ids(updated_since=admin_alert_cutoff_timestamp())" in server_source
    assert "updated_since=admin_alert_cutoff_timestamp()" in server_source


def test_device_event_feed_uses_actionable_health_events():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS device_events" in server_source
    assert "event_key TEXT NOT NULL UNIQUE" in server_source
    assert "def persist_device_events" in server_source
    assert "def fetch_device_events" in server_source
    assert "def sync_device_events" in server_source
    assert "sync_device_events(device_id=cleaned.get(\"device_id\"))" in server_source
    assert "return fetch_device_events(limit=limit, device_id=device_id)" in server_source

    for event_kind in (
        "telemetry_recovered",
        "pump_no_level_rise",
        "source_tank_low",
        "source_tank_recovered",
        "wifi_signal_weak",
        "wifi_signal_recovered",
        "wifi_signal_drop",
        "wifi_signal_improved",
        "wifi_disconnect_frequency_high",
        "sensor_recovered",
        "heap_low",
        "heap_recovered",
        "firmware_changed",
        "config_changed",
        "device_rebooted",
        "reboot_frequency_high",
        "command_queued",
        "command_acknowledged",
        "command_delivery_failed",
        "ota_published",
        "ota_succeeded",
        "ota_failed",
    ):
        assert event_kind in server_source


def test_project_device_env_can_disable_local_relay_defaults():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '"RELAY_STATUS_URLS"' in server_source
    assert '"RELAY_COMMAND_URLS"' in server_source
    assert "blank_overrides.add(key)" in server_source
    assert "if key in blank_overrides:" in server_source


def test_project_device_env_can_override_smtp_settings():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    device_env_example = (PROJECT_ROOT / "device.env.example").read_text(encoding="utf-8")

    assert "DEVICE_ENV_OVERRIDE_KEYS" in server_source
    assert '"SMTP_PASSWORD"' in server_source
    assert "forced_overrides.add(key)" in server_source
    assert "if key in forced_overrides:" in server_source
    assert "SMTP_HOST=mail.salewell.co.in" in device_env_example
    assert "SMTP_PASSWORD=replace-with-support-mailbox-password" in device_env_example


def test_admin_slave_status_uses_direct_peer_packet_freshness():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "DIRECT_PEER_STALE_AFTER_SECONDS" in server_source
    assert "def direct_peer_packet_is_fresh" in server_source
    assert "peer_packet_fresh = direct_peer_packet_is_fresh(entry)" in server_source
    assert "slave_upper_enabled = bool(service_config.get(\"slave_upper_sensor_enabled\"))" in server_source
    assert "upper_reachable = online and peer_packet_fresh is True and tank_level_is_valid" in server_source
    assert '"direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s")' in server_source
    assert 'cleaned.get("direct_peer_last_packet_age_s")' in server_source
    assert '"direct_peer_last_packet_age_s": "INTEGER"' in server_source


def test_admin_device_table_shows_raw_upper_echo_distance():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert '"sensor_distance_cm": payload.get("sensor_distance_cm")' in server_source
    assert '"sensor_distance_label": sensor_distance_label' in server_source
    assert '"water_depth_label": payload.get("water_depth_label")' in server_source
    assert "Depth {{ device.water_depth_label" in admin_template
    assert "Echo {{ device.sensor_distance_label" in admin_template
    assert '"%.1f"|format(device.sensor_distance_cm)' not in admin_template


def test_admin_customer_page_renders_string_sensor_distance():
    if str(PROJECT_ROOT / "flask_app") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "flask_app"))
    import server

    client = server.app.test_client()
    with client.session_transaction() as session:
        auth_marker = server.current_auth_marker_for_identity("admin", username=server.LOGIN_USERNAME)
        platform_session_id = server.register_active_platform_session(
            server.SESSION_PLATFORM_DASHBOARD,
            "admin",
            username=server.LOGIN_USERNAME,
        )
        session["logged_in"] = True
        session["username"] = server.LOGIN_USERNAME
        session["role"] = "admin"
        session["device_id"] = None
        session["auth_marker"] = auth_marker
        session["platform_session_id"] = platform_session_id
        session["admin_logged_in"] = True
        session["admin_username"] = server.LOGIN_USERNAME
        session["admin_device_id"] = None
        session["admin_auth_marker"] = auth_marker
        session["admin_platform_session_id"] = platform_session_id
        session["csrf_token"] = "test-csrf-token"
    response = client.get("/admin/customers")
    assert response.status_code == 200


def test_dashboards_render_company_icon_home_links():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert 'class="dashboard-brand-link" href="/"' in admin_template
    assert 'id="brandLogo" class="brand-logo" href="/"' in customer_template
    assert "url_for('homepage')" not in customer_template


def test_dashboard_titles_are_simple_and_icon_precedes_title():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    customer_template = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")

    assert '<h1 class="hero-title">Dashboard</h1>' in admin_template
    assert "<h1>Dashboard</h1>" in customer_template
    assert admin_template.index('class="dashboard-brand-link"') < admin_template.index('<h1 class="hero-title">Dashboard</h1>')
    assert customer_template.index('id="brandLogo"') < customer_template.index("<h1>Dashboard</h1>")
    assert "Admin Dashboard</h1>" not in admin_template
    assert "Home Water Dashboard" not in customer_template
    assert "Home Water Dashboard" in login_template


def test_device_detail_dashboard_button_returns_to_admin_dashboard():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert '<a class="btn" href="{{ url_for(\'admin_customers\') }}">Dashboard</a>' in device_template
    assert '<a class="btn" href="/">Dashboard</a>' not in device_template


def test_device_detail_uses_compact_balanced_cards_and_buttons():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert ".hero-actions .btn{height:36px;min-height:36px;min-width:96px" in device_template
    assert ".summary-grid{grid-template-columns:repeat(5,minmax(0,1fr));align-items:stretch}" in device_template
    assert ".admin-grid{grid-template-columns:repeat(3,minmax(0,1fr));align-items:stretch}" in device_template
    assert ".admin-grid .admin-form .btn,.admin-grid .admin-form .btn-full,.firmware-upload-grid .btn{width:100%;max-width:none;justify-self:stretch}" in device_template
    assert ".sensor-setup-card{align-content:start}" in device_template
    assert ".tank-setup-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}" in device_template
    assert ".summary-value.tone-ok,.summary-value.tone-warn,.summary-value.tone-bad,.summary-value.tone-info" in device_template


def test_device_detail_upload_result_uses_closable_popup():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'data-firmware-upload-form' in device_template
    assert 'modal.dataset.resultModal=""' in device_template
    assert 'request.setRequestHeader("X-Requested-With","XMLHttpRequest")' in device_template
    assert "showResultModal(ok?\"Upload successful\":\"Upload failed\"" in device_template
    assert 'request.headers.get("X-Requested-With") == "XMLHttpRequest"' in server_source
    assert '<div class="message success" style="margin-top:14px">{{ config_message }}</div>' not in device_template
    assert '<div class="message error" style="margin-top:14px">{{ config_error }}</div>' not in device_template
    assert "{% if config_message or config_error %}" not in device_template


def test_device_detail_exposes_admin_tank_setup_controls():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "{{ upper_setup_label }} Sensor Setup" in device_template
    assert "Lower / Source Sensor Setup" in device_template
    assert 'id="upperSensorSetupSection"' in device_template
    assert 'id="lowerSensorSetupSection"' in device_template
    assert '{% set upper_setup_label = "Slave Upper" if slave_upper_checked else "Master Upper" %}' in device_template
    assert "{% if master_upper_checked or slave_upper_checked %}" in device_template
    assert '{% if service_config.get("source_tank_monitoring_enabled") %}' in device_template
    assert "upperTankSetupForm" in device_template
    assert "lowerTankSetupForm" in device_template
    assert "admin_upper_tank_height_cm" in device_template
    assert "admin_upper_tank_capacity_liters" in device_template
    assert "admin_lower_tank_height_cm" in device_template
    assert "admin_lower_tank_capacity_liters" in device_template
    assert "upperTankSetupCalibrateButton" in device_template
    assert "lowerTankSetupCalibrateButton" in device_template
    assert 'class="tank-setup-actions"' in device_template
    assert "document.querySelectorAll(\".tank-setup-form\")" in device_template
    assert "heightInput?.checkValidity()&&capacityInput?.checkValidity()" in device_template
    assert 'if(button?.name&&!formData.has(button.name))formData.append(button.name,button.value||"");' in device_template
    assert "Save Height & Capacity" in device_template
    assert ">Calibrate Sensor</span>" in device_template
    assert 'data-confirm-button="Calibrate Sensor"' in device_template
    assert "admin_device_detail_sensor_configure" in device_template
    assert "admin_device_detail_sensor_calibrate" in server_source
    assert 'name="sensor" value="upper"' in device_template
    assert 'name="sensor" value="lower"' in device_template
    assert 'data-confirm-title="Save and calibrate {{ upper_setup_label|lower }} sensor?"' in device_template
    assert 'command = f"CONFIG_LOWER:{height_cm:.1f}:{capacity_liters:.1f}"' in server_source
    assert 'f"CONFIG_UPPER:{height_cm:.1f}:{capacity_liters:.1f}"' in server_source
    assert 'command = f"CONFIG_CAPACITY:{capacity_liters:.1f}"' in server_source
    assert 'calibration_command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"' in server_source
    assert 'command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"' in server_source
    assert "The master will forward upper calibration to the slave MCU" in server_source


def test_device_detail_keeps_upper_sensor_sources_mutually_exclusive():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '{% set slave_upper_checked = slave_device_checked and service_config.get("slave_upper_sensor_enabled", slave_device_checked) %}' in device_template
    assert '{% set master_upper_checked = ((not slave_upper_checked) and service_config.get("master_upper_sensor_enabled", not slave_device_checked)) or not slave_upper_checked %}' in device_template
    assert 'id="masterUpperSensorOption" type="radio" name="upper_sensor_source" value="master" {% if master_upper_checked %}checked{% endif %} onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert 'id="slaveUpperSensorOption" type="radio" name="upper_sensor_source" value="slave" {% if slave_upper_checked %}checked{% endif %} onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert 'Only one upper sensor source can be active.' in device_template
    assert 'onchange="window.swtSyncRuntimeConfigurationOptions&&window.swtSyncRuntimeConfigurationOptions()"' in device_template
    assert "function syncRuntimeConfigurationOptions()" in device_template
    assert "function syncUpperSensorSetupLabels()" in device_template
    assert 'const label=slaveUpper?.checked?(section.dataset.slaveUpperLabel||"Slave Upper"):(section.dataset.masterUpperLabel||"Master Upper");' in device_template
    assert 'if(saveButton)saveButton.textContent="Save Height & Capacity";' in device_template
    assert 'calibrateButton.dataset.confirmButton="Calibrate Sensor";' in device_template
    assert 'if(span)span.textContent="Calibrate Sensor";' in device_template
    assert "window.swtSyncRuntimeConfigurationOptions=syncRuntimeConfigurationOptions;" in device_template
    assert 'window.addEventListener("pageshow",syncRuntimeConfigurationOptions);' in device_template
    assert "slaveUpper.checked=false;" in device_template
    assert "masterUpper.checked=false;" in device_template
    assert "slaveUpper.disabled=true;" in device_template
    assert "slaveUpper.disabled=false;" in device_template
    assert 'upper_sensor_source = request.form.get("upper_sensor_source")' in server_source
    assert 'if upper_sensor_source in {"master", "slave"}:' in server_source
    assert 'slave_upper_sensor_enabled = slave_device_enabled and upper_sensor_source == "slave"' in server_source
    assert 'elif slave_upper_sensor_enabled:' in server_source
    assert 'master_upper_sensor_enabled = False' in server_source


def test_device_detail_renders_master_slave_memory_health_graph():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    firmware_source = (PROJECT_ROOT.parent / "swt_firmware_project" / "src" / "two_node_udp.cpp").read_text(encoding="utf-8")

    assert "Memory Health" in device_template
    assert 'id="memoryChartWrap"' in device_template
    assert "function renderMemoryHealth(history,snapshot,serviceConfig)" in device_template
    assert "snapshotHeap=Number(snapshot?.free_heap)" in device_template
    assert "snapshotSlaveHeap=Number(snapshot?.slave_free_heap)" in device_template
    assert "const usesSlave=Boolean(serviceConfig?.slave_device_enabled)" in device_template
    assert 'const slaveStat=usesSlave?' in device_template
    assert 'const slaveLegend=usesSlave?' in device_template
    assert "Master CPU" not in device_template
    assert "Slave CPU" not in device_template
    assert "memory-line-master" in device_template
    assert "memory-line-slave" in device_template
    assert "health-chart-grid" not in device_template
    assert 'title:"Heap Graph"' in device_template
    assert 'title:"CPU Graph"' not in device_template
    assert "chart-series-legend" in device_template
    assert "Master + Slave" in device_template
    assert "memory-live-wave" not in device_template
    assert "wavePathFor" not in device_template
    assert "let latestHistory=[]" in device_template
    assert "renderMemoryHealth(latestHistory,snapshot,serviceConfig)" in device_template
    assert 'statusUrl.searchParams.set("history","0")' in device_template
    assert "slave_free_heap" in server_source
    assert "slave_cpu_utilization_pct" in server_source
    assert "cpu_utilization_pct REAL" in server_source
    assert '"free_heap": row["free_heap"]' in server_source
    assert '"slave_free_heap": row["slave_free_heap"]' in server_source
    assert "fetch_device_history(scoped_device_id, limit=48)" in server_source
    assert 'doc["slave_free_heap"] = lastSlaveFreeHeap' in firmware_source
    assert 'doc["cpu_utilization_pct"] = cpuUtilizationPct' in firmware_source
    assert 'doc["free_heap"] = ESP.getFreeHeap()' in firmware_source


def test_android_release_upload_modal_is_detached_and_shows_progress():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert 'data-android-upload-form' in admin_template
    assert 'data-android-upload-progress' in admin_template
    assert 'id="upload_result_panel"' in admin_template
    assert 'data-upload-result-message' in admin_template
    assert 'showUploadResultPanel("Upload failed"' in admin_template
    assert 'showUploadResultPanel("Upload successful"' in admin_template
    assert 'document.write(request.responseText)' not in admin_template[
        admin_template.index('const androidUploadForm = document.querySelector("[data-android-upload-form]")')
        : admin_template.index('const registerDeviceForm = document.querySelector("[data-register-device-form]")')
    ]
    assert 'request.upload.addEventListener("progress"' in admin_template
    assert 'document.body.appendChild(panel)' in admin_template
    assert 'panel.dataset.busy === "true"' in admin_template
    assert '.android-upload-progress-bar' in admin_template


def test_android_release_upload_rejects_debug_or_unsigned_apks():
    android_release_helper = (PROJECT_ROOT / "flask_app" / "android_releases.py").read_text(encoding="utf-8")

    assert 'ANDROID_BLOCKED_APK_FILENAME_MARKERS = ("debug", "unsigned")' in android_release_helper
    assert "Upload a signed release APK. Debug or unsigned APK files are not allowed for customer updates." in android_release_helper


def test_android_release_cleanup_removes_all_builds_copy():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert "Remove All Android Builds" in admin_template
    assert "Remove every uploaded Android build record and APK file" in admin_template
    assert "Latest Android build will stay active" not in admin_template


def test_release_channel_keeps_firmware_on_device_detail_page():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    mobile_firmware_routes = (PROJECT_ROOT / "flask_app" / "mobile_firmware_routes.py").read_text(encoding="utf-8")

    assert 'action="/admin/releases/firmware"' not in admin_template
    assert "Master and slave firmware are uploaded from each device detail page." in admin_template
    assert 'name="firmware_role" value="master"' in device_template
    assert 'name="firmware_role" value="slave"' in device_template
    assert "Master Configuration" in device_template
    assert "Slave Configuration" in device_template
    assert "Other Features" in device_template
    assert 'name="upper_sensor_source" value="master"' in device_template
    assert 'name="upper_sensor_source" value="slave"' in device_template
    assert 'name="relay_enabled"' in device_template
    assert "syncRuntimeConfigurationOptions" in device_template
    assert "Upload Master Firmware" in device_template
    assert "Upload Slave Firmware" in device_template
    assert ".release-action-row button,.release-action-row a,.release-utility-form button{min-height:34px" in admin_template
    assert "latest_global_firmware_artifact" not in server_source
    assert '"__all_customers__"' not in mobile_firmware_routes
    assert "target_role" in server_source
    assert "target_device=target_device" in mobile_firmware_routes


def test_device_detail_install_profile_template_has_deploy_fallback():
    device_template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")

    assert "{% if firmware_install_profile is not defined %}" in device_template
    assert '"SWT_ARCH_ID", "value": "4"' in device_template
    assert '"SWT_DIRECT_PEER_ENABLED", "value": "0"' in device_template
    assert "Required master firmware build flags" not in device_template


def test_android_app_update_check_compares_installed_version_code():
    android_source = (
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
    ).read_text(encoding="utf-8")

    assert '"currentVersionCode" to installedAppVersionCode().toString()' in android_source
    assert "latestVersionCode > currentVersionCode" in android_source
    assert "serverUpdateAvailable && latestVersionCode > currentVersionCode && apkUrl.isNotBlank()" in android_source


def test_release_versions_use_year_train_increment_syntax():
    android_build = (PROJECT_ROOT.parent / "swt_android_app_project" / "app" / "build.gradle.kts").read_text(
        encoding="utf-8"
    )
    firmware_loader = (PROJECT_ROOT.parent / "swt_firmware_project" / "scripts" / "platformio_shared_device_env.py").read_text(
        encoding="utf-8"
    )
    android_release_helper = (PROJECT_ROOT / "flask_app" / "android_releases.py").read_text(encoding="utf-8")
    firmware_release_helper = (PROJECT_ROOT / "flask_app" / "firmware_artifacts.py").read_text(encoding="utf-8")

    assert 'return "${releaseVersionYearPrefix()}.$train.$patch"' in android_build
    assert 'return f"{current_version_year_prefix()}.{train}.{patch}"' in firmware_loader
    assert r"^\d{2}\.[1-9]\d*\.[1-9]\d*$" in android_release_helper
    assert rb"\b\d{2}\.[1-9]\d*\.[1-9]\d*\b".decode("ascii") in firmware_release_helper


def test_register_device_modal_is_detached_and_shows_progress():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert 'data-register-device-form' in admin_template
    assert 'data-register-device-progress' in admin_template
    assert 'setRegisterProgress' in admin_template
    assert 'document.body.appendChild(panel)' in admin_template
    assert 'panel.dataset.busy === "true"' in admin_template
    assert '.form-submit-progress-bar' in admin_template


def test_admin_dashboard_renders_online_offline_pie_chart():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert "fleet-health-chart" in admin_template
    assert "fleet-pie" in admin_template
    assert "--online-pct: {{ online_percent }}%" in admin_template
    assert "Online <b>{{ online_devices }}</b>" in admin_template
    assert "Offline <b>{{ offline_devices }}</b>" in admin_template


def test_admin_dashboard_uses_compact_aligned_layout():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert ".admin-control-room{padding:14px}" in admin_template
    assert ".hero-grid{grid-template-columns:minmax(0,1.15fr) minmax(360px,.85fr);gap:16px;align-items:start}" in admin_template
    assert ".hero-panel{padding:18px}" in admin_template
    assert ".hero-actions a,.hero-actions button{min-height:38px" in admin_template
    assert ".admin-control-room .summary-card{min-height:118px" in admin_template
    assert ".searchControls button,.searchControls a,.searchInput{min-height:38px" in admin_template
    assert ".alert-table-shell{max-height:14rem}" in admin_template
    assert ".device-table-shell{max-height:35rem}" in admin_template


def test_homepage_shows_active_identity_and_logout():
    login_template = (PROJECT_ROOT / "flask_app" / "templates" / "login.html").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert "def homepage_login_status():" in server_source
    assert "homepage_user=homepage_login_status()" in server_source
    assert "active_homepage_user = nav_auth.active_user|default(homepage_user)" in login_template
    assert "Logged in as - {{ active_homepage_user.display_name }}" in login_template
    assert '<form class="logout-form" method="post" action="/logout">' in login_template
