import hashlib
import hmac
from pathlib import Path

from flask_app.mobile_firmware_routes import build_ota_authorization

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_mobile_firmware_payload_carries_scoped_expiring_ota_auth():
    route_source = (PROJECT_ROOT / "flask_app" / "mobile_firmware_routes.py").read_text(encoding="utf-8")
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'artifact_payload["ota_authorization"]' in route_source
    assert '"upload_device_key"' not in route_source
    assert '"local_auth_password"' not in route_source
    assert 'response.headers["Cache-Control"] = "no-store"' in route_source
    assert "configured_device_key_for_id=configured_device_key_for_id" in server_source


def test_ota_authorization_is_artifact_scoped_and_hmac_signed():
    artifact = {
        "id": 42,
        "version_label": "26.1.700",
        "md5": "0123456789abcdef0123456789abcdef",
    }
    auth = build_ota_authorization("SWT-000-000-000-001", artifact, "secret-device-key", now=1_800_000_000)

    assert auth["device_id"] == "swt-000-000-000-001"
    assert auth["artifact_id"] == 42
    assert auth["expires_at"] == 1_800_000_600
    message = "\n".join(
        (
            auth["device_id"],
            str(auth["artifact_id"]),
            auth["version"],
            auth["md5"],
            str(auth["expires_at"]),
        )
    )
    assert auth["signature"] == hmac.new(
        b"secret-device-key",
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
