from pathlib import Path
import re

from flask_app import server
from werkzeug.exceptions import MethodNotAllowed

ROOT = Path(__file__).resolve().parents[2]
ANDROID = ROOT / "swt_android_app_project/app/src/main/java/com/smartwatertank/app"
FIRMWARE = ROOT / "swt_firmware_project"


def test_every_android_mobile_endpoint_is_registered():
    adapter = server.app.url_map.bind("localhost")
    paths = set()
    for file in ANDROID.glob("*.kt"):
        paths.update(re.findall(r'"(api/mobile/[^"\n]+)"', file.read_text(encoding="utf-8")))
    assert len(paths) >= 10
    for path in sorted(paths):
        path = re.sub(r'\$\{[^}]+\}', "contract-test", path)
        try:
            adapter.match("/" + path, method="GET")
        except MethodNotAllowed:
            adapter.match("/" + path, method="POST")


def test_firmware_generated_cloud_endpoints_have_correct_methods():
    script = (FIRMWARE / "scripts/platformio_shared_device_env.py").read_text(encoding="utf-8")
    methods = {rule.rule: rule.methods for rule in server.app.url_map.iter_rules()}
    for route, method in {"/status": "POST", "/device/command": "GET", "/api/device/sync": "POST", "/device/command/ack": "POST"}.items():
        assert method in methods[route]
        if not route.endswith("/ack"):
            assert f'endpoint(cloud_base_url, "{route}")' in script


def test_android_ota_paths_are_registered_in_firmware():
    android = "\n".join(file.read_text(encoding="utf-8") for file in ANDROID.glob("*.kt"))
    firmware = (FIRMWARE / "src/two_node_udp.cpp").read_text(encoding="utf-8")
    routes = set(re.findall(r'server.on\("(/[^"\n]+)"', firmware))
    paths = set(re.findall(r'"(api/ota/[^"\n]+)"', android))
    assert paths
    assert {"/" + path for path in paths} <= routes


def test_leak_context_changes_are_not_deduplicated():
    initial = {"device_id": "contract-device", "level": 70, "slow_leak": True,
        "leak_detected_during_quiet_hours": False}
    changed = dict(initial, leak_detected_during_quiet_hours=True)
    assert server.build_telemetry_sync_fingerprint(initial) != server.build_telemetry_sync_fingerprint(changed)
