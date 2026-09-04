from pathlib import Path


SERVER = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")


def test_start_and_stop_share_highest_priority_latest_intent_queue():
    start = SERVER.index("def queue_device_command(")
    body = SERVER[start : SERVER.index("def recent_device_command_row", start)]
    assert 'if normalized_family == "pump":' in body
    assert "priority = 1000" in body
    assert 'if compact in {"ON", "OFF"}:\n        return "pump"' in SERVER
    assert "device_command_family(row[\"command\"]) == normalized_family" in body


def test_firmware_manual_commands_keep_hard_safety_interlocks():
    firmware = (Path(__file__).resolve().parents[2] / "swt_firmware_project" / "src" / "two_node_udp.cpp").read_text(encoding="utf-8")
    assert "const bool manualStartCommand = starterPanelManualStartReason(reason);" in firmware
    assert "upperHighFloatAvailable && upperHighFloatActive," in firmware
    assert "SWT_FEATURE_SOURCE_LOW_FLOAT && SOURCE_LOW_FLOAT_PIN >= 0 && sourceLowFloatActive," in firmware
    assert "FirmwareLogic::pumpStartBlockReason(" in firmware
    assert "const char* startBlockReason = on ? pumpStartBlockReason() : nullptr;" in firmware
    assert 'strcmp(reason, "upper_tank_high") == 0' in firmware
