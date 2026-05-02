"""Mobile firmware artifact API routes."""

from flask import jsonify, request, url_for


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

        artifact = fetch_latest_firmware_artifact(target_device)
        if not artifact:
            return jsonify({"error": "No firmware upload is available for this device yet."}), 404

        download_url = url_for(
            "mobile_device_firmware_download",
            artifact_id=int(artifact["id"]),
            device_id=target_device,
        )
        return jsonify(
            {
                "device_id": target_device,
                "artifact": build_firmware_artifact_payload(
                    artifact,
                    target_device=target_device,
                    download_endpoint=download_url,
                ),
            }
        )

    @app.route("/api/mobile/device/firmware/<int:artifact_id>/download")
    @mobile_auth_required
    def mobile_device_firmware_download(artifact_id):
        target_device = current_mobile_scope_device_id(request.args.get("device_id", type=str))
        if not target_device:
            return jsonify({"error": "device not found"}), 404

        artifact = fetch_firmware_artifact(artifact_id, device_id=target_device)
        if not artifact:
            return jsonify({"error": "firmware artifact not found"}), 404

        storage_path = firmware_artifact_storage_path(artifact.get("stored_filename"))
        if not storage_path.is_file():
            logger.warning("Firmware artifact %s is registered but missing on disk: %s", artifact_id, storage_path)
            return jsonify({"error": "firmware artifact file is missing"}), 404

        return build_firmware_artifact_file_response(artifact, storage_path)
