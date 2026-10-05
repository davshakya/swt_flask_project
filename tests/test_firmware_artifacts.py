from types import SimpleNamespace

import pytest

from flask_app.firmware_artifacts import (
    build_firmware_artifact_chunk_response,
    build_firmware_artifact_file_response,
    detect_firmware_binary_build_flags,
    detect_firmware_binary_role,
    extract_firmware_version_label,
    validate_firmware_binary_build_flags,
    validate_firmware_binary_role,
)


def test_extract_firmware_version_accepts_current_and_legacy_formats():
    assert extract_firmware_version_label(b"demo v26.8.614 SWT_FIRMWARE_ROLE=master") == "v26.8.614"
    assert extract_firmware_version_label(b"demo 26.1.613 SWT_FIRMWARE_ROLE=master") == "26.1.613"


def test_extract_firmware_version_rejects_invalid_current_month():
    with pytest.raises(ValueError, match=r"vYY\.M\.increment"):
        extract_firmware_version_label(b"demo v26.13.614 SWT_FIRMWARE_ROLE=master")


class FakeResponse:
    def __init__(self, payload, **kwargs):
        self.payload = payload
        self.kwargs = kwargs
        self.headers = {}


def test_detect_firmware_binary_role_from_embedded_marker():
    assert detect_firmware_binary_role(b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master") == "master"
    assert detect_firmware_binary_role(b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=slave") == "slave"
    assert detect_firmware_binary_role(b"\xe9demo v26.9.3 SWT repeater started swt_repeater") == "repeater"


def test_repeater_binary_can_be_stored_in_either_independent_slot():
    payload = b"\xe9demo v26.9.3 SWT repeater started swt_repeater"

    assert validate_firmware_binary_role(payload, "repeater1") == "repeater"
    assert validate_firmware_binary_role(payload, "repeater2") == "repeater"


def test_detect_slave_role_ignores_generic_master_route_strings():
    payload = b"swt_master route labels slave_tank peer-only-slave swt_slave"

    assert detect_firmware_binary_role(payload) == "slave"


def test_validate_firmware_binary_role_rejects_mismatch():
    with pytest.raises(ValueError, match="is slave firmware, but this upload slot expects master firmware"):
        validate_firmware_binary_role(
            b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=slave",
            "master",
            "slave-firmware.bin",
        )


def test_validate_firmware_binary_role_rejects_unmarked_binary():
    with pytest.raises(ValueError, match="Could not verify whether this firmware binary is master or slave"):
        validate_firmware_binary_role(b"\xe9demo 26.1.276", "master", "firmware.bin")


def test_detect_firmware_binary_build_flags_from_embedded_marker():
    payload = (
        b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master "
        b"SWT_BUILD_FLAGS SWT_FEATURE_MASTER_LOWER_SENSOR=0 SWT_ARCH_ID=4 "
        b"SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT=1 SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT=0 "
        b"SWT_DIRECT_PEER_ENABLED=0"
    )

    assert detect_firmware_binary_build_flags(payload) == {
        "SWT_FEATURE_MASTER_LOWER_SENSOR": "0",
        "SWT_ARCH_ID": "4",
        "SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT": "1",
        "SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT": "0",
        "SWT_DIRECT_PEER_ENABLED": "0",
    }


def test_validate_firmware_binary_build_flags_allows_install_profile_mismatch():
    payload = (
        b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master "
        b"SWT_BUILD_FLAGS SWT_FEATURE_MASTER_LOWER_SENSOR=1 SWT_ARCH_ID=1 "
        b"SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT=0 SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT=1 "
        b"SWT_DIRECT_PEER_ENABLED=1"
    )

    detected = validate_firmware_binary_build_flags(
        payload,
        {
            "SWT_FEATURE_MASTER_LOWER_SENSOR": "0",
            "SWT_ARCH_ID": "4",
            "SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT": "1",
            "SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT": "0",
            "SWT_DIRECT_PEER_ENABLED": "0",
        },
        "master-firmware.bin",
    )

    assert detected["SWT_ARCH_ID"] == "1"
    assert detected["SWT_DIRECT_PEER_ENABLED"] == "1"


def test_firmware_artifact_download_response_is_ota_friendly(tmp_path):
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master")

    def fake_send_file(*args, **kwargs):
        return SimpleNamespace(headers={})

    response = build_firmware_artifact_file_response(
        fake_send_file,
        {
            "id": 42,
            "content_type": "application/octet-stream",
            "original_filename": "firmware.bin",
            "md5": "abc123",
            "version_label": "26.1.276",
        },
        firmware_path,
    )

    assert response.headers["X-Firmware-Size"] == str(firmware_path.stat().st_size)
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["Connection"] == "close"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["x-MD5"] == "abc123"


def test_firmware_artifact_chunk_response_returns_requested_slice(tmp_path):
    firmware_path = tmp_path / "firmware.bin"
    firmware_path.write_bytes(b"0123456789abcdef")

    response = build_firmware_artifact_chunk_response(
        FakeResponse,
        {
            "id": 7,
            "content_type": "application/octet-stream",
            "md5": "def456",
            "version_label": "26.1.276",
        },
        firmware_path,
        offset=4,
        size=5,
    )

    assert response.payload == b"45678"
    assert response.kwargs["mimetype"] == "application/octet-stream"
    assert response.headers["Content-Length"] == "5"
    assert response.headers["X-Firmware-Size"] == "16"
    assert response.headers["X-Chunk-Offset"] == "4"
    assert response.headers["X-Chunk-Length"] == "5"
