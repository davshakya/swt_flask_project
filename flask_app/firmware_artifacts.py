"""Firmware artifact upload, metadata, and response helpers."""

from pathlib import Path
import hashlib
import re
import secrets
import time


def sanitize_firmware_filename(filename):
    raw_name = Path(str(filename or "")).name.strip()
    if not raw_name:
        return "firmware.bin"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip(" .-_")
    if not safe_name:
        safe_name = "firmware"
    if not safe_name.lower().endswith(".bin"):
        safe_name = f"{safe_name}.bin"
    return safe_name


def firmware_artifact_storage_path(base_dir, stored_filename):
    return Path(base_dir) / Path(str(stored_filename or "")).name


def extract_firmware_version_label(payload):
    matches = re.findall(rb"\b\d+\.\d+\.\d+\+\d+\b", payload)
    if not matches:
        raise ValueError(
            "Firmware version was not found inside the uploaded binary. "
            "Build the firmware first and upload .pio/build/nodemcuv2/firmware.bin."
        )
    return matches[-1].decode("ascii")


def read_uploaded_firmware(uploaded_file, max_bytes):
    if uploaded_file is None:
        raise ValueError("Choose a compiled firmware .bin file to upload.")

    raw_name = Path(str(uploaded_file.filename or "")).name.strip()
    if not raw_name:
        raise ValueError("Choose a compiled firmware .bin file to upload.")
    if Path(raw_name).suffix.lower() != ".bin":
        raise ValueError("Upload a compiled firmware .bin file.")

    payload = uploaded_file.stream.read(max_bytes + 1)
    if not payload:
        raise ValueError("Uploaded firmware file was empty.")
    if len(payload) > max_bytes:
        raise ValueError(f"Firmware upload is too large. Current limit is {max_bytes // (1024 * 1024)} MB.")

    return {
        "original_filename": sanitize_firmware_filename(raw_name),
        "payload": payload,
        "version_label": extract_firmware_version_label(payload),
        "md5": hashlib.md5(payload).hexdigest(),
        "content_type": str(uploaded_file.mimetype or "application/octet-stream").strip() or "application/octet-stream",
    }


def make_stored_firmware_filename(device_id):
    return f"{device_id}-{int(time.time())}-{secrets.token_hex(4)}.bin"


def build_firmware_artifact_payload(artifact, target_device=None, download_endpoint=None, normalize_device_id=lambda value: value):
    if not artifact:
        return None

    payload = {
        "id": int(artifact.get("id") or 0),
        "device_id": normalize_device_id(target_device or artifact.get("target_device")),
        "original_filename": artifact.get("original_filename") or "firmware.bin",
        "version_label": artifact.get("version_label") or "",
        "notes": artifact.get("notes") or "",
        "md5": artifact.get("md5") or "",
        "size_bytes": int(artifact.get("size_bytes") or 0),
        "content_type": artifact.get("content_type") or "application/octet-stream",
        "uploaded_by": artifact.get("uploaded_by") or "",
        "created_at": artifact.get("created_at") or "",
    }
    if download_endpoint:
        payload["download_url"] = download_endpoint
    return payload


def build_firmware_artifact_file_response(send_file_func, artifact, storage_path):
    response = send_file_func(
        str(storage_path),
        mimetype=artifact.get("content_type") or "application/octet-stream",
        as_attachment=False,
        download_name=artifact.get("original_filename") or storage_path.name,
        conditional=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["x-MD5"] = artifact.get("md5") or ""
    if artifact.get("version_label"):
        response.headers["X-Firmware-Version"] = artifact["version_label"]
    response.headers["X-Firmware-Artifact-Id"] = str(artifact["id"])
    return response
