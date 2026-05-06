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
