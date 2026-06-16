from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"


def test_command_activity_descriptions_cover_pump_reboot_thresholds_and_setup():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def describe_command_activity(command):")
    function_source = source[function_start : source.index("\n\ndef send_alert_webhook", function_start)]

    assert 'if normalized == "ON":' in function_source
    assert '"Pump start requested."' in function_source
    assert 'if normalized == "OFF":' in function_source
    assert '"Pump stop requested."' in function_source
    assert 'if normalized == "REBOOT":' in function_source
    assert '"Device restart requested."' in function_source
    assert 'if normalized.startswith("THRESHOLDS:"):' in function_source
    assert '"Auto thresholds update requested:' in function_source
    assert 'if normalized.startswith("SERVICECFG4:"):' in function_source


def test_command_events_use_friendly_activity_messages():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def build_command_events(limit=20, device_id=None):")
    function_source = source[function_start : source.index("\n\ndef build_ota_events", function_start)]

    assert "command_activity = describe_command_activity(command)" in function_source
    assert "command_activity[\"queued_message\"]" in function_source
    assert "command_activity[\"ack_message\"]" in function_source
    assert "command_activity[\"pending_message\"]" in function_source
    assert "command_activity[\"failed_message\"]" in function_source
    assert "command_activity['kind_suffix']" in function_source


def test_ota_events_surface_upload_pending_and_success_progress_messages():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def build_ota_events(limit=20, device_id=None):")
    function_source = source[function_start : source.index("\n\ndef normalize_device_event_time", function_start)]

    assert "Firmware uploaded for" in function_source
    assert "Upgrade package is ready." in function_source
    assert "Upgrade finished successfully." in function_source
    assert "Firmware update is pending on device." in function_source
