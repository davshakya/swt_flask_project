import pytest

from flask_app.firmware_artifacts import (
    detect_firmware_binary_role,
    validate_firmware_binary_role,
)


def test_detect_firmware_binary_role_from_embedded_marker():
    assert detect_firmware_binary_role(b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master") == "master"
    assert detect_firmware_binary_role(b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=slave") == "slave"


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
