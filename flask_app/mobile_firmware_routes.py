"""Mobile firmware artifact API routes."""

import hashlib
import hmac
import time

from flask import jsonify, request, url_for
from flask_app.firmware_artifacts import normalize_firmware_artifact_role


OTA_AUTH_TTL_SECONDS = 60 * 60


def firmware_role_device_id(device_id, role):
    normalized_device_id = str(device_id or "").strip().lower()
    if role == "slave":
        if normalized_device_id.startswith("swt-test-000-"):
            return "swt-test-100-" + normalized_device_id[len("swt-test-000-") :]
        if normalized_device_id.startswith("swt-000-"):
            return "swt-100-" + normalized_device_id[len("swt-000-") :]
    return normalized_device_id


def build_ota_authorization(device_id, artifact, device_key, now=None):
    normalized_device_id = str(device_id or "").strip().lower()
    secret = str(device_key or "").strip()
    artifact_id = int((artifact or {}).get("id") or 0)
    version_label = str((artifact or {}).get("version_label") or "").strip()
    md5 = str((artifact or {}).get("md5") or "").strip().lower()
    if not normalized_device_id or not secret or artifact_id <= 0 or not md5:
        return None

    expires_at = int(now if now is not None else time.time()) + OTA_AUTH_TTL_SECONDS
    message = "\n".join(
        (
            normalized_device_id,
            str(artifact_id),
            version_label,
            md5,
            str(expires_at),
        )
    )
    signature = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "device_id": normalized_device_id,
        "artifact_id": artifact_id,
        "version": version_label,
        "md5": md5,
        "expires_at": expires_at,
        "signature": signature,
    }


def register_mobile_firmware_routes(
    app,
    *,
    mobile_auth_required,
    current_mobile_scope_device_id,
    fetch_latest_firmware_artifact,
    fetch_firmware_artifact,
    fetch_device_service_config,
    build_firmware_artifact_payload,
    configured_device_key_for_id,
    firmware_artifact_storage_path,
    build_firmware_artifact_file_response,
    logger,
):
    def firmware_service_disabled_response(service_name, field_name):
        return jsonify(
            {
                "error": (
                    f"{service_name} is disabled for this device. "
                    "Enable it from the Flask admin device detail page before retrying."
                ),
                field_name: False,
            }
        ), 403

    @app.route("/api/mobile/device/firmware")
    @mobile_auth_required
    def mobile_device_firmware():
        target_device = current_mobile_scope_device_id(request.args.get("device_id", type=str))
        if not target_device:
            return jsonify({"error": "device not found"}), 404
        try:
            target_role = normalize_firmware_artifact_role(request.args.get("role", "master", type=str))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        service_config = fetch_device_service_config(target_device)
        if not service_config.get("local_firmware_upload_enabled"):
            return firmware_service_disabled_response("Local firmware upload", "local_firmware_upload_enabled")

        artifact = fetch_latest_firmware_artifact(target_device, role=target_role)
        if not artifact:
            return jsonify({"error": f"No {target_role} firmware upload is available for this device yet."}), 404

        download_url = url_for(
            "mobile_device_firmware_download",
            artifact_id=int(artifact["id"]),
            device_id=target_device,
            role=target_role,
        )
        artifact_payload = build_firmware_artifact_payload(
            artifact,
            target_device=target_device,
            download_endpoint=download_url,
        )
        local_upload_device_id = firmware_role_device_id(target_device, target_role)
        local_upload_device_key = configured_device_key_for_id(local_upload_device_id) or configured_device_key_for_id(target_device)
        ota_authorization = build_ota_authorization(
            local_upload_device_id,
            artifact,
            local_upload_device_key,
        )
        if ota_authorization is None:
            return jsonify(
                {
                    "error": (
                        f"OTA authorization is not configured for {local_upload_device_id}. "
                        "Configure the matching raw key in SWT_DEVICE_KEYS, or set SWT_DEVICE_API_KEY "
                        "when all SWT devices intentionally share that key, then restart Flask."
                    )
                }
            ), 503
        if artifact_payload is not None:
            artifact_payload["ota_authorization"] = ota_authorization
        response = jsonify(
            {
                "device_id": target_device,
                "role": target_role,
                "artifact": artifact_payload,
                "delivery": f"device_specific_{target_role}",
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/mobile/device/firmware/<int:artifact_id>/download")
    @mobile_auth_required
    def mobile_device_firmware_download(artifact_id):
        target_device = current_mobile_scope_device_id(request.args.get("device_id", type=str))
        if not target_device:
            return jsonify({"error": "device not found"}), 404
        try:
            target_role = normalize_firmware_artifact_role(request.args.get("role", "master", type=str))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        service_config = fetch_device_service_config(target_device)
        if not service_config.get("local_firmware_upload_enabled"):
            return firmware_service_disabled_response("Local firmware upload", "local_firmware_upload_enabled")

        artifact = fetch_firmware_artifact(artifact_id, device_id=target_device, role=target_role)
        if not artifact:
            return jsonify({"error": "firmware artifact not found"}), 404

        storage_path = firmware_artifact_storage_path(artifact.get("stored_filename"))
        if not storage_path.is_file():
            logger.warning("Firmware artifact %s is registered but missing on disk: %s", artifact_id, storage_path)
            return jsonify({"error": "firmware artifact file is missing"}), 404

        return build_firmware_artifact_file_response(artifact, storage_path)
