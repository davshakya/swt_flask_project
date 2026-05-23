"""Mobile firmware artifact API routes."""

from flask import jsonify, request, url_for
from flask_app.firmware_artifacts import normalize_firmware_artifact_role


def register_mobile_firmware_routes(
    app,
    *,
    mobile_auth_required,
    current_mobile_scope_device_id,
    fetch_latest_firmware_artifact,
    fetch_firmware_artifact,
    build_firmware_artifact_payload,
    firmware_artifact_storage_path,
    build_firmware_artifact_file_response,
    logger,
):
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

        artifact = fetch_latest_firmware_artifact(target_device, role=target_role)
        if not artifact:
            return jsonify({"error": f"No {target_role} firmware upload is available for this device yet."}), 404

        download_url = url_for(
            "mobile_device_firmware_download",
            artifact_id=int(artifact["id"]),
            device_id=target_device,
            role=target_role,
        )
        return jsonify(
            {
                "device_id": target_device,
                "role": target_role,
                "artifact": build_firmware_artifact_payload(
                    artifact,
                    target_device=target_device,
                    download_endpoint=download_url,
                ),
                "delivery": f"device_specific_{target_role}",
            }
        )

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

        artifact = fetch_firmware_artifact(artifact_id, device_id=target_device, role=target_role)
        if not artifact:
            return jsonify({"error": "firmware artifact not found"}), 404

        storage_path = firmware_artifact_storage_path(artifact.get("stored_filename"))
        if not storage_path.is_file():
            logger.warning("Firmware artifact %s is registered but missing on disk: %s", artifact_id, storage_path)
            return jsonify({"error": "firmware artifact file is missing"}), 404

        return build_firmware_artifact_file_response(artifact, storage_path)
