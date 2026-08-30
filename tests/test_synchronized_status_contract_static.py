from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "flask_app" / "server.py").read_text(encoding="utf-8")
ANDROID = (
    ROOT.parent
    / "swt_android_app_project"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "smartwatertank"
    / "app"
    / "MainActivity.kt"
).read_text(encoding="utf-8")


def test_flask_exposes_one_versioned_cross_project_status_contract():
    assert "STATUS_CONTRACT_VERSION = 1" in SERVER
    assert "def build_synchronized_status_payload(" in SERVER
    for section in ('"nodes": {', '"pump": {', '"sensors": {', '"ai_ml": {', '"configuration": {'):
        assert section in SERVER
    assert '"synchronized_status": synchronized_status' in SERVER
    assert '"synchronized_status_current_status"' in SERVER
    assert '"status_contract": synchronized_status' in SERVER


def test_android_prefers_flask_normalized_status_contract():
    assert 'systemStatus?.optJSONObject("synchronized_status")' in ANDROID
    assert 'synchronizedStatus?.optJSONObject("sensors")' in ANDROID
    assert 'synchronizedStatus?.optJSONObject("pump")' in ANDROID
    assert 'applySynchronizedNodeStatus(snapshot, synchronizedStatus)' in ANDROID
    assert 'synchronizedUpperSensor.ifBlank { mainSensorStatus.label }' in ANDROID
    assert 'synchronizedSourceSensor.ifBlank { lowerSensorStatus.label }' in ANDROID


def test_logical_pump_state_precedes_physical_relay_fallback():
    assert 'pump_state = relay_state_label(cleaned.get("pump"))' in SERVER
    assert 'pump_state = relay_state_label(cleaned.get("relay_on"))' in SERVER
    assert SERVER.index('pump_state = relay_state_label(cleaned.get("pump"))') < SERVER.index(
        'pump_state = relay_state_label(cleaned.get("relay_on"))'
    )
    android_pump_reader = ANDROID[ANDROID.index("private fun snapshotPumpRaw") : ANDROID.index("private fun snapshotPumpText")]
    assert android_pump_reader.index('jsonText(snapshot, "pump")') < android_pump_reader.index(
        'relayStateLabel(snapshot?.opt("relay_on"))'
    )
