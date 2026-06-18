from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_mobile_firmware_payload_carries_scoped_local_upgrade_auth():
    route_source = (PROJECT_ROOT / "flask_app" / "mobile_firmware_routes.py").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'artifact_payload["upload_device_id"]' in route_source
    assert 'artifact_payload["upload_device_key"]' in route_source
    assert 'artifact_payload["local_auth_username"]' in route_source
    assert 'artifact_payload["local_auth_password"]' in route_source
    assert 'artifact_payload["local_auth_fallback_password"]' in route_source
    assert 'response.headers["Cache-Control"] = "no-store"' in route_source
    assert "fetch_device_local_web_password=fetch_device_local_web_password" in server_source
    assert "default_local_web_auth_password=default_local_web_auth_password" in server_source
