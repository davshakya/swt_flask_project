from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import virtual_device


def test_load_local_env_files_prefers_tests_virtual_device_env(tmp_path):
    (tmp_path / "device.env").write_text(
        "SWT_DEVICE_ID=shared-device\n"
        "SWT_VIRTUAL_DEVICE_BASE_URL=http://shared-host:8000/\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "virtual_device.env").write_text(
        "SWT_VIRTUAL_DEVICE_ID=virtual-test-device\n"
        "SWT_VIRTUAL_DEVICE_BASE_URL=http://127.0.0.1:9100/\n",
        encoding="utf-8",
    )

    env = {}
    virtual_device.load_local_env_files(
        env_paths=virtual_device.resolve_env_file_paths(tmp_path),
        environ=env,
    )

    assert env["SWT_DEVICE_ID"] == "shared-device"
    assert env["SWT_VIRTUAL_DEVICE_ID"] == "virtual-test-device"
    assert env["SWT_VIRTUAL_DEVICE_BASE_URL"] == "http://127.0.0.1:9100/"


def test_build_parser_uses_virtual_device_specific_env_defaults(monkeypatch):
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_BASE_URL", "http://127.0.0.1:9100/")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_ID", "virtual-test-device")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_KEY", "virtual-test-key")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_TELEMETRY_INTERVAL", "9.5")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK", "false")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_LOG_LEVEL", "debug")

    args = virtual_device.build_parser().parse_args([])

    assert args.base_url == "http://127.0.0.1:9100/"
    assert args.device_id == "virtual-test-device"
    assert args.device_key == "virtual-test-key"
    assert args.telemetry_interval == 9.5
    assert args.enable_source_tank is False
    assert args.log_level == "DEBUG"
