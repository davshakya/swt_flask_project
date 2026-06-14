import pytest

from flask_app.firmware_artifacts import (
    detect_firmware_binary_build_flags,
    detect_firmware_binary_role,
    validate_firmware_binary_build_flags,
    validate_firmware_binary_role,
)
from flask_app.server import build_device_firmware_install_profile


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


def test_validate_firmware_binary_build_flags_rejects_install_profile_mismatch():
    payload = (
        b"\xe9demo 26.1.276 SWT_FIRMWARE_ROLE=master "
        b"SWT_BUILD_FLAGS SWT_FEATURE_MASTER_LOWER_SENSOR=1 SWT_ARCH_ID=1 "
        b"SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT=0 SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT=1 "
        b"SWT_DIRECT_PEER_ENABLED=1"
    )

    with pytest.raises(ValueError, match="does not match this device installation profile"):
        validate_firmware_binary_build_flags(
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


def test_master_slave_install_profile_respects_source_sensor_service():
    without_source = build_device_firmware_install_profile(
        {
            "slave_device_enabled": True,
            "source_tank_monitoring_enabled": False,
        }
    )
    assert without_source["configuration_type"] == "master_slave"
    assert without_source["flags"]["SWT_FEATURE_MASTER_LOWER_SENSOR"] == "0"
    assert without_source["flags"]["SWT_ARCH_ID"] == "1"
    assert without_source["flags"]["SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT"] == "1"

    with_source = build_device_firmware_install_profile(
        {
            "slave_device_enabled": True,
            "source_tank_monitoring_enabled": True,
        }
    )
    assert with_source["flags"]["SWT_FEATURE_MASTER_LOWER_SENSOR"] == "1"
