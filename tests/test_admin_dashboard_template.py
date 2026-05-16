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
