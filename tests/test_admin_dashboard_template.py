from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_admin_customer_page_renders_one_status_message_slot():
    template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert template.count("{% if success %}") == 1
    assert template.count("{% if error %}") == 1


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
    assert '"direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s")' in server_source
    assert 'cleaned.get("direct_peer_last_packet_age_s")' in server_source
    assert '"direct_peer_last_packet_age_s": "INTEGER"' in server_source


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


def test_android_release_upload_modal_is_detached_and_shows_progress():
    admin_template = (PROJECT_ROOT / "flask_app" / "templates" / "admin_customers.html").read_text(encoding="utf-8")

    assert 'data-android-upload-form' in admin_template
    assert 'data-android-upload-progress' in admin_template
    assert 'request.upload.addEventListener("progress"' in admin_template
    assert 'document.body.appendChild(panel)' in admin_template
    assert 'panel.dataset.busy === "true"' in admin_template
    assert '.android-upload-progress-bar' in admin_template


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
    assert "Upload Master Firmware" in device_template
    assert "Upload Slave Firmware" in device_template
    assert ".release-action-row button,.release-action-row a,.release-utility-form button{min-height:34px" in admin_template
    assert "latest_global_firmware_artifact" not in server_source
    assert '"__all_customers__"' not in mobile_firmware_routes
    assert "target_role" in server_source
    assert "target_device=target_device" in mobile_firmware_routes


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
