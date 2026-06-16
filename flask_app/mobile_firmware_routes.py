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
    queue_device_command,
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

    @app.route("/api/mobile/device/firmware/cloud-upgrade", methods=["POST"])
    @mobile_auth_required
    def mobile_device_firmware_cloud_upgrade():
        payload = request.get_json(silent=True) or {}
        target_device = current_mobile_scope_device_id(payload.get("device_id") or request.args.get("device_id", type=str))
        if not target_device:
            return jsonify({"error": "device not found"}), 404
        requested_role = str(payload.get("role") or request.args.get("role", "master", type=str) or "master").strip().lower()
        include_slave = bool(payload.get("include_slave")) or requested_role in {"all", "both", "bundle", "master_slave"}
        try:
            target_role = "master" if include_slave else normalize_firmware_artifact_role(requested_role)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        artifact_id = payload.get("artifact_id")
        if artifact_id:
            try:
                artifact = fetch_firmware_artifact(int(artifact_id), device_id=target_device, role=target_role)
            except (TypeError, ValueError):
                artifact = None
        else:
            artifact = fetch_latest_firmware_artifact(target_device, role=target_role)
        if not artifact:
            return jsonify({"error": f"No {target_role} firmware upload is available for this device yet."}), 404

        master_download_url = url_for(
            "device_firmware_artifact_chunk",
            artifact_id=int(artifact["id"]),
            device_id=target_device,
            role=target_role,
            _external=True,
        )
        slave_artifact = fetch_latest_firmware_artifact(target_device, role="slave") if include_slave else None
        slave_download_url = ""
        if slave_artifact:
            slave_download_url = url_for(
                "device_firmware_artifact_chunk",
                artifact_id=int(slave_artifact["id"]),
                device_id=target_device,
                role="slave",
                _external=True,
            )
        command = (
            f"OTA_BUNDLE:{master_download_url}|{slave_download_url}"
            if slave_download_url
            else f"OTA_URL:{master_download_url}"
        )
        queue_device_command(command, target_device)
        logger.info(
            "Queued cloud firmware upgrade for %s role=%s artifact=%s slave_artifact=%s",
            target_device,
            target_role,
            artifact["id"],
            slave_artifact["id"] if slave_artifact else None,
        )

        return jsonify(
            {
                "ok": True,
                "device_id": target_device,
                "role": "master_slave" if slave_artifact else target_role,
                "command": "OTA_BUNDLE" if slave_artifact else "OTA_URL",
                "message": "Cloud firmware upgrade queued. The tank will download it on its next command poll.",
                "artifact": build_firmware_artifact_payload(
                    artifact,
                    target_device=target_device,
                    download_endpoint=master_download_url,
                ),
                "slave_artifact": build_firmware_artifact_payload(
                    slave_artifact,
                    target_device=target_device,
                    download_endpoint=slave_download_url,
                ) if slave_artifact else None,
            }
        )
