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


def test_mobile_customer_device_scope_is_mandatory_and_rejects_cross_device_requests():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    function_start = server_source.index("def current_mobile_scope_device_id")
    function_body = server_source[
        function_start : server_source.index("\ndef mobile_customer_cloud_feed_block_response", function_start)
    ]

    assert 'mobile_user.get("role") != "customer"' in function_body
    assert 'abort(401, description="Authenticated customer account required.")' in function_body
    assert 'abort(403, description="The logged-in account has no assigned device.")' in function_body
    assert "normalized_requested != scoped_device_id" in function_body
    assert 'abort(403, description="The requested device is not assigned to the logged-in account.")' in function_body
    assert "return scoped_device_id" in function_body
    assert "scoped_device_id or normalized_requested" not in function_body
    assert "current_mobile_scope_device_id(requested_device_id) or latest_device_id()" not in server_source


def test_mobile_firmware_routes_always_use_authenticated_device_scope():
    route_source = (PROJECT_ROOT / "flask_app" / "mobile_firmware_routes.py").read_text(encoding="utf-8")

    assert route_source.count("current_mobile_scope_device_id(request.args.get(\"device_id\", type=str))") == 2
    assert "fetch_latest_firmware_artifact(target_device, role=target_role)" in route_source
    assert "fetch_firmware_artifact(artifact_id, device_id=target_device, role=target_role)" in route_source


def test_ota_key_resolution_can_use_shared_swt_key_when_registry_is_absent():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    function_start = server_source.index("def configured_device_key_for_id")
    function_body = server_source[
        function_start : server_source.index("\ndef hash_device_api_key", function_start)
    ]

    assert 'os.environ.get("SWT_DEVICE_API_KEY", "").strip()' in function_body
    assert 'normalized_device_id.startswith("swt-")' in function_body
    assert "not device_config_value_is_placeholder(shared_swt_key)" in function_body
    assert "return shared_swt_key" in function_body


def test_health_exposes_signed_ota_contract_version():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
    health_start = server_source.index("def health():")
    health_body = server_source[health_start : server_source.index("\n\n@app.route", health_start)]

    assert "OTA_CONTRACT_VERSION = 2" in server_source
    assert '"ota_contract_version": OTA_CONTRACT_VERSION' in health_body
    assert '"ota_authorization": "artifact_hmac_sha256"' in health_body


def test_firmware_events_compare_each_role_against_its_own_reported_version():
    server_source = (PROJECT_ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")

    assert 'cleaned.get("slave_firmware_version")' in server_source
    assert '"slave_firmware_version": "TEXT"' in server_source
    assert 'current_slave_firmware = str((latest_snapshot or {}).get("slave_firmware_version")' in server_source
    assert 'current_firmware = current_slave_firmware if target_role == "slave" else current_master_firmware' in server_source


def test_ota_authorization_is_artifact_scoped_and_hmac_signed():
    artifact = {
        "id": 42,
        "version_label": "26.1.700",
        "md5": "0123456789abcdef0123456789abcdef",
    }
    auth = build_ota_authorization("SWT-000-000-000-001", artifact, "secret-device-key", now=1_800_000_000)

    assert auth["device_id"] == "swt-000-000-000-001"
    assert auth["artifact_id"] == 42
    assert auth["expires_at"] == 1_800_003_600
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
