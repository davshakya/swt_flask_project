from __future__ import annotations

from pathlib import Path


SERVER_SOURCE = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")
DEVICE_TEMPLATE_SOURCE = (Path(__file__).resolve().parents[1] / "flask_app" / "templates" / "device_detail.html").read_text(encoding="utf-8")


def _function_block(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_fetch_device_service_config_selects_optional_water_features():
    block = _function_block(
        SERVER_SOURCE,
        "def fetch_device_service_config(device_id, account=None, snapshot=None):",
        "def list_device_service_configs(device_ids=None, accounts_by_device=None, snapshots_by_device=None):",
    )
    assert "master_turbidity_enabled, slave_turbidity_enabled, relay_enabled, ai_analysis_enabled" in block


def test_list_device_service_configs_selects_optional_water_features():
    block = _function_block(
        SERVER_SOURCE,
        "def list_device_service_configs(device_ids=None, accounts_by_device=None, snapshots_by_device=None):",
        "def upsert_device_service_config(",
    )
    assert "master_turbidity_enabled, slave_turbidity_enabled, relay_enabled, ai_analysis_enabled" in block


def test_existing_municipal_option_is_the_optional_feature_master_switch():
    assert 'name="municipal_sensor_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert "Municipal Water Feature" in DEVICE_TEMPLATE_SOURCE
    assert 'name="master_turbidity_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert 'name="slave_turbidity_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert "Lower Turbidity" in DEVICE_TEMPLATE_SOURCE
    assert "Upper Turbidity" in DEVICE_TEMPLATE_SOURCE

    build_block = _function_block(
        SERVER_SOURCE,
        "def build_device_service_command(service_config):",
        "def device_automation_settings_key(device_id):",
    )
    assert 'municipal_sensor_enabled = bool(config.get("municipal_sensor_enabled", False))' in build_block
    assert "SERVICECFG12:" in build_block
    assert 'name="municipal_valve_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert 'name="source_outlet_valve_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert 'name="multi_tank_enabled"' in DEVICE_TEMPLATE_SOURCE
    assert "municipal_valve=1 if municipal_valve_enabled else 0" in build_block
    assert "source_outlet_valve=1 if source_outlet_valve_enabled else 0" in build_block
    assert "municipal=1 if municipal_sensor_enabled else 0" in build_block
    assert "master_turbidity=1 if master_turbidity_enabled else 0" in build_block
    assert "slave_turbidity=1 if slave_turbidity_enabled else 0" in build_block


def test_device_detail_shows_live_master_and_slave_turbidity_status():
    assert 'id="lowerTurbidityStatus"' in DEVICE_TEMPLATE_SOURCE
    assert 'id="upperTurbidityStatus"' in DEVICE_TEMPLATE_SOURCE
    assert 'id="lowerTurbidityReading"' in DEVICE_TEMPLATE_SOURCE
    assert 'id="upperTurbidityReading"' in DEVICE_TEMPLATE_SOURCE
    assert "lower_turbidity_estimated_ntu" in DEVICE_TEMPLATE_SOURCE
    assert "upper_turbidity_estimated_ntu" in DEVICE_TEMPLATE_SOURCE
    assert 'turbidityStatus(snapshot,serviceConfig,"lower")' in DEVICE_TEMPLATE_SOURCE
    assert 'turbidityStatus(snapshot,serviceConfig,"upper")' in DEVICE_TEMPLATE_SOURCE
    assert 'id="municipalSensorStatus"' in DEVICE_TEMPLATE_SOURCE
    assert 'id="municipalSensorReading"' in DEVICE_TEMPLATE_SOURCE
    assert "municipalSensorStatus(snapshot,serviceConfig)" in DEVICE_TEMPLATE_SOURCE


def test_optional_confirmation_sensors_are_admin_configurable_and_transported():
    for name in (
        "starter_contactor_sensor_enabled",
        "motor_current_sensor_enabled",
        "water_flow_sensor_enabled",
        "water_pressure_sensor_enabled",
    ):
        assert f'name="{name}"' in DEVICE_TEMPLATE_SOURCE
        assert f'config.get("{name}")' in SERVER_SOURCE
    build_block = _function_block(
        SERVER_SOURCE,
        "def build_device_service_command(service_config):",
        "def device_automation_settings_key(device_id):",
    )
    assert "SERVICECFG12:" in build_block
    assert ":{starter_aux}:{motor_current}:{water_flow}:{water_pressure}:{multi_tank}" in build_block
