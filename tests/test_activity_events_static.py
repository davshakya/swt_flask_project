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
    assert 'if normalized.startswith("SERVICECFG12:") or normalized.startswith("SERVICECFG11:")' in function_source
    assert '"municipal sensor"' in function_source
    assert '"turbidity monitoring"' in function_source


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
    assert "def persist_command_activity_events" in source
    assert "persist_command_activity_events(device_command_target)" in source
    assert "persist_command_activity_events(normalized_device_id)" in source


def test_firmware_local_logs_feed_activity_events():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")
    function_start = source.index("def build_firmware_log_events_from_payload")
    function_source = source[function_start : source.index("\n\ndef build_ota_events", function_start)]
    events_start = source.index("def build_events(")
    events_source = source[events_start : source.index("\n\ndef build_snapshot_activity_events", events_start)]

    assert "def fetch_local_device_logs" in source
    assert "local_device_logs_url" in source
    assert 'f"{normalized}/api/logs"' in source
    assert "FIRMWARE_LOG_LINE_PATTERN" in source
    assert "def build_firmware_log_events_from_payload" in source
    assert 'raw_firmware_logs = raw_payload.get("firmware_logs")' in source
    assert 'firmware_log_payload["firmware_logs"] = raw_firmware_logs' in source
    assert 'logs = payload.get("firmware_logs")' in source
    assert "firmware_log_events = build_firmware_log_events_from_payload(" in source
    assert "firmware_log_kind(message)" in function_source
    assert '"source_table": "firmware_local_log"' in function_source
    assert "include_local_logs=False" in events_source
    assert "build_local_firmware_log_events(" in events_source
    assert "persist_device_events(local_log_events" in events_source
    assert 'request.args.get("local_logs", "0")' in source
    assert 'eventsUrl.searchParams.set("local_logs","1")' not in template


def test_ota_events_surface_upload_pending_and_success_progress_messages():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index("def build_ota_events(limit=20, device_id=None):")
    function_source = source[function_start : source.index("\n\ndef normalize_device_event_time", function_start)]

    assert "Firmware uploaded for" in function_source
    assert "Upgrade package is ready." in function_source
    assert "Upgrade finished successfully." in function_source
    assert "Firmware update is pending on device." in function_source
