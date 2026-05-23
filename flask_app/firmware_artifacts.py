"""Firmware artifact upload, metadata, and response helpers."""

from pathlib import Path
import hashlib
import re
import secrets
import time

FIRMWARE_RELEASE_VERSION_PATTERN = re.compile(r"^\d{2}\.[1-9]\d*\.[1-9]\d*$")
FIRMWARE_RELEASE_VERSION_BYTES_PATTERN = re.compile(rb"\b\d{2}\.[1-9]\d*\.[1-9]\d*\b")
FIRMWARE_ARTIFACT_ROLES = ("master", "slave")
FIRMWARE_BINARY_ROLE_MARKERS = {
    "master": (
        b"SWT_FIRMWARE_ROLE=master",
        b'"firmware_role":"master"',
        b'"node_role":"master_control"',
        b'"device_type":"swt_master"',
        b"swt_master",
    ),
    "slave": (
        b"SWT_FIRMWARE_ROLE=slave",
        b'"firmware_role":"slave"',
        b'"node_role":"slave_tank"',
        b'"device_type":"swt_slave"',
        b"swt_slave",
    ),
}
FIRMWARE_BINARY_DEVICE_ID_PATTERNS = {
    "master": re.compile(rb"\bswt-000-\d{3}-\d{3}-\d{3}\b", re.IGNORECASE),
    "slave": re.compile(rb"\bswt-100-\d{3}-\d{3}-\d{3}\b", re.IGNORECASE),
}


def normalize_firmware_artifact_role(value, default="master"):
    normalized = str(value or default or "master").strip().lower()
    if normalized not in FIRMWARE_ARTIFACT_ROLES:
        raise ValueError("Choose master or slave firmware role.")
    return normalized


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
    matches = FIRMWARE_RELEASE_VERSION_BYTES_PATTERN.findall(payload)
    if not matches:
        raise ValueError(
            "Firmware version in YY.n.n format was not found inside the uploaded binary. "
            "Build the firmware first and upload .pio/build/nodemcuv2/firmware.bin."
        )
    version_label = matches[-1].decode("ascii")
    if not FIRMWARE_RELEASE_VERSION_PATTERN.match(version_label):
        raise ValueError("Firmware version must use YY.n.n format, for example 26.1.276.")
    return version_label


def detect_firmware_binary_role(payload):
    normalized_payload = bytes(payload or b"").lower()
    if not normalized_payload:
        return None

    exact_marker_matches = [
        role
        for role in FIRMWARE_ARTIFACT_ROLES
        if f"SWT_FIRMWARE_ROLE={role}".encode("ascii").lower() in normalized_payload
    ]
    if len(exact_marker_matches) == 1:
        return exact_marker_matches[0]

    marker_matches = [
        role
        for role, markers in FIRMWARE_BINARY_ROLE_MARKERS.items()
        if any(marker.lower() in normalized_payload for marker in markers)
    ]
    if len(marker_matches) == 1:
        return marker_matches[0]

    device_id_matches = [
        role
        for role, pattern in FIRMWARE_BINARY_DEVICE_ID_PATTERNS.items()
        if pattern.search(normalized_payload)
    ]
    if len(device_id_matches) == 1:
        return device_id_matches[0]
    return None


def validate_firmware_binary_role(payload, expected_role, filename="firmware.bin"):
    normalized_role = normalize_firmware_artifact_role(expected_role)
    detected_role = detect_firmware_binary_role(payload)
    if not detected_role:
        raise ValueError(
            "Could not verify whether this firmware binary is master or slave. "
            "Rebuild the firmware with the current role marker and upload the matching .bin file."
        )
    if detected_role != normalized_role:
        safe_name = sanitize_firmware_filename(filename)
        raise ValueError(
            f"Selected {safe_name} is {detected_role} firmware, "
            f"but this upload slot expects {normalized_role} firmware."
        )
    return detected_role


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
        "detected_role": detect_firmware_binary_role(payload),
        "md5": hashlib.md5(payload).hexdigest(),
        "content_type": str(uploaded_file.mimetype or "application/octet-stream").strip() or "application/octet-stream",
    }


def make_stored_firmware_filename(device_id, role="master"):
    safe_role = normalize_firmware_artifact_role(role)
    return f"{device_id}-{safe_role}-{int(time.time())}-{secrets.token_hex(4)}.bin"


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
        "role": normalize_firmware_artifact_role(artifact.get("target_role") or artifact.get("role") or "master"),
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
