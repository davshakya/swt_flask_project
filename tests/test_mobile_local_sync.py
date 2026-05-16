from __future__ import annotations

from pathlib import Path


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


def test_dashboard_local_sync_route_polls_private_lan_device():
    server_source = SERVER_SOURCE.read_text(encoding="utf-8")
    dashboard_source = (PROJECT_ROOT / "flask_app" / "templates" / "index.html").read_text(encoding="utf-8")

    assert '@app.route("/dashboard/local-sync", methods=["POST"])' in server_source
    assert "def fetch_local_device_status" in server_source
    assert "is_private_device_base_url" in server_source
    assert 'host.endswith(".local")' in server_source
    assert "local device_id does not match requested device" in server_source
    assert 'source_ip="dashboard_local_wifi"' in server_source
    assert "syncLocalDashboardSnapshot" in dashboard_source
    assert '`${API}/dashboard/local-sync`' in dashboard_source


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
