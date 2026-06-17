from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_SOURCE = PROJECT_ROOT / "flask_app" / "server.py"
TEMPLATE_SOURCE = PROJECT_ROOT / "flask_app" / "templates" / "device_detail.html"


def test_mobile_threshold_route_saves_shared_device_settings():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/api/mobile/device/thresholds", methods=["GET", "POST"])')
    route_source = source[route_start : source.index("\n\nregister_mobile_firmware_routes(", route_start)]

    assert "fetch_device_automation_settings(target_device, snapshot=snapshot)" in route_source
    assert "upsert_device_automation_settings(" in route_source
    assert "build_device_automation_command(updated_settings)" in route_source
    assert '"admin access required"' in route_source


def test_device_detail_page_exposes_shared_threshold_settings():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    function_start = source.index('def device_detail_page(device_id):')
    function_source = source[function_start : source.index("\n\ndef device_simulator_state_key", function_start)]

    assert "automation_settings = fetch_device_automation_settings(scoped_device_id, snapshot=snapshot)" in function_source
    assert "automation_settings=automation_settings," in function_source


def test_admin_device_detail_template_has_threshold_save_form():
    source = TEMPLATE_SOURCE.read_text(encoding="utf-8")

    assert "Tank Auto Thresholds" in source
    assert "url_for('admin_device_detail_thresholds', device_id=device_id)" in source
    assert "name=\"auto_start_pct\"" in source
    assert "name=\"auto_stop_pct\"" in source
    assert "shared across Flask, Android, and firmware" in source


def test_device_detail_status_and_template_include_live_configuration_grid():
    source = SERVER_SOURCE.read_text(encoding="utf-8")
    route_start = source.index('@app.route("/devices/<device_id>/status")')
    route_source = source[route_start : source.index('\n\n@app.route("/status", methods=["GET", "POST"])', route_start)]

    assert '"automation_settings": fetch_device_automation_settings(scoped_device_id, snapshot=snapshot)' in route_source

    template_source = TEMPLATE_SOURCE.read_text(encoding="utf-8")
    assert "Device Configuration" in template_source
    assert 'id="deviceInfoGrid"' in template_source
    assert "renderDeviceInfo(snapshot,systemStatus,monitoringSummary,serviceConfig,automationSettings);" in template_source
