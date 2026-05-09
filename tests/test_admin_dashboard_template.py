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
    assert server_source.index("refresh_admin_entry_alerts(entry)") < server_source.index("fetch_active_alert_summaries(merged.keys())")


def test_project_device_env_can_disable_local_relay_defaults():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert '"RELAY_STATUS_URLS"' in server_source
    assert '"RELAY_COMMAND_URLS"' in server_source
    assert "blank_overrides.add(key)" in server_source
    assert "if key in blank_overrides:" in server_source
