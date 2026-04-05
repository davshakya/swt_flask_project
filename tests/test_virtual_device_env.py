import sqlite3
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import run_virtual_devices as virtual_device
import generate_virtual_device_envs


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


def test_discover_virtual_device_env_files_finds_multiple_envs(tmp_path):
    env_dir = tmp_path / "tests" / "virtual_devices"
    env_dir.mkdir(parents=True)
    (env_dir / "device-002.env").write_text("SWT_VIRTUAL_DEVICE_ID=device-002\n", encoding="utf-8")
    (env_dir / "device-001.env").write_text("SWT_VIRTUAL_DEVICE_ID=device-001\n", encoding="utf-8")
    (env_dir / "device-template.env.example").write_text("ignored\n", encoding="utf-8")

    discovered = virtual_device.discover_virtual_device_env_files(env_dir)

    assert [path.name for path in discovered] == ["device-001.env", "device-002.env"]


def test_format_sequenced_value_preserves_numeric_suffix():
    assert virtual_device.format_sequenced_value("swt-000-000-000-001", 1) == "swt-000-000-000-001"
    assert virtual_device.format_sequenced_value("swt-000-000-000-001", 5) == "swt-000-000-000-005"
    assert virtual_device.format_sequenced_value("virtual-device", 3) == "virtual-device-003"


def test_build_parser_uses_virtual_device_specific_env_defaults(monkeypatch):
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_BASE_URL", "http://127.0.0.1:9100/")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_ID", "virtual-test-device")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_KEY", "virtual-test-key")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_TELEMETRY_INTERVAL", "9.5")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_TIME_SCALE", "7.5")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK", "false")
    monkeypatch.setenv("SWT_VIRTUAL_DEVICE_LOG_LEVEL", "debug")

    args = virtual_device.build_parser().parse_args([])

    assert args.base_url == "http://127.0.0.1:9100/"
    assert args.device_id == "virtual-test-device"
    assert args.device_key == "virtual-test-key"
    assert args.telemetry_interval == 9.5
    assert args.time_scale == 7.5
    assert args.enable_source_tank is False
    assert args.log_level == "DEBUG"


def test_build_parser_ignores_shared_cloud_url_for_virtual_device_defaults(monkeypatch):
    monkeypatch.delenv("SWT_VIRTUAL_DEVICE_BASE_URL", raising=False)
    monkeypatch.delenv("SWT_LOCAL_FLASK_BASE_URL", raising=False)
    monkeypatch.setenv("SWT_CLOUD_BASE_URL", "https://smart-water-tank-v1.onrender.com/")

    args = virtual_device.build_parser().parse_args([])

    assert args.base_url == "http://127.0.0.1:8000/"


def test_build_config_from_env_uses_per_file_values_over_shared_defaults():
    env = {
        "SWT_DEVICE_ID": "shared-device",
        "SWT_DEVICE_API_KEY": "shared-key",
        "SWT_VIRTUAL_DEVICE_ID": "virtual-device-002",
        "SWT_VIRTUAL_DEVICE_KEY": "virtual-key-002",
        "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:9102/",
    }

    config = virtual_device.build_config_from_env(env)

    assert config.device_id == "virtual-device-002"
    assert config.device_key == "virtual-key-002"
    assert config.base_url == "http://127.0.0.1:9102/"


def test_generated_device_env_values_increment_device_id_and_seed():
    args = virtual_device.build_parser().parse_args(
        [
            "--base-url", "http://127.0.0.1:8000/",
            "--device-id", "swt-000-000-000-001",
            "--device-key", "shared-key",
            "--seed", "42",
        ]
    )

    first = virtual_device.generated_device_env_values(args, 1)
    third = virtual_device.generated_device_env_values(args, 3)

    assert first["SWT_VIRTUAL_DEVICE_ID"] == "swt-000-000-000-001"
    assert third["SWT_VIRTUAL_DEVICE_ID"] == "swt-000-000-000-003"
    assert first["SWT_VIRTUAL_DEVICE_KEY"] == "shared-key"
    assert third["SWT_VIRTUAL_DEVICE_KEY"] == "shared-key"
    assert first["SWT_VIRTUAL_DEVICE_SEED"] == "42"
    assert third["SWT_VIRTUAL_DEVICE_SEED"] == "44"


def test_build_configs_from_env_paths_applies_override_env(tmp_path):
    env_path = tmp_path / "device-001.env"
    env_path.write_text(
        "SWT_VIRTUAL_DEVICE_ID=swt-override-001\n"
        "SWT_VIRTUAL_DEVICE_KEY=override-key\n"
        "SWT_VIRTUAL_DEVICE_BASE_URL=http://127.0.0.1:8000/\n",
        encoding="utf-8",
    )

    configs = virtual_device.build_configs_from_env_paths(
        [env_path],
        override_env={
            "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:9100/",
            "SWT_VIRTUAL_DEVICE_RUN_SECONDS": "15",
        },
        base_environ={},
    )

    assert len(configs) == 1
    assert configs[0].device_id == "swt-override-001"
    assert configs[0].base_url == "http://127.0.0.1:9100/"
    assert configs[0].run_seconds == 15.0


def test_virtual_device_payload_includes_source_tank_aliases():
    config = virtual_device.build_config_from_env(
        {
            "SWT_VIRTUAL_DEVICE_ID": "swt-source-001",
            "SWT_VIRTUAL_DEVICE_KEY": "source-key",
            "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:8000/",
            "SWT_VIRTUAL_DEVICE_START_SOURCE_LEVEL_PERCENT": "71.5",
            "SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK": "true",
        }
    )

    device = virtual_device.VirtualDevice(config)
    payload = device.build_payload(online=True)

    assert payload["lower_tank_level"] == 71.5
    assert payload["source_tank_level"] == 71.5
    assert payload["source_tank_sensor"] == payload["lower_sensor"]
    assert payload["source_tank_sensor_info"] == payload["lower_sensor_info"]
    assert payload["source_tank_sensor_distance_cm"] == payload["lower_sensor_distance_cm"]
    assert payload["source_tank_service"] == payload["lower_tank_service"] == "ON"


def test_virtual_device_time_scale_accelerates_fill_and_drain():
    slow_config = virtual_device.build_config_from_env(
        {
            "SWT_VIRTUAL_DEVICE_ID": "swt-speed-001",
            "SWT_VIRTUAL_DEVICE_KEY": "speed-key",
            "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:8000/",
            "SWT_VIRTUAL_DEVICE_TANK_CAPACITY_LITERS": "1000",
            "SWT_VIRTUAL_DEVICE_START_LEVEL_PERCENT": "60",
            "SWT_VIRTUAL_DEVICE_USAGE_LITERS_PER_HOUR": "60",
            "SWT_VIRTUAL_DEVICE_FILL_LITERS_PER_HOUR": "300",
            "SWT_VIRTUAL_DEVICE_TIME_SCALE": "1",
            "SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK": "false",
        }
    )
    fast_config = virtual_device.build_config_from_env(
        {
            "SWT_VIRTUAL_DEVICE_ID": "swt-speed-002",
            "SWT_VIRTUAL_DEVICE_KEY": "speed-key",
            "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:8000/",
            "SWT_VIRTUAL_DEVICE_TANK_CAPACITY_LITERS": "1000",
            "SWT_VIRTUAL_DEVICE_START_LEVEL_PERCENT": "60",
            "SWT_VIRTUAL_DEVICE_USAGE_LITERS_PER_HOUR": "60",
            "SWT_VIRTUAL_DEVICE_FILL_LITERS_PER_HOUR": "300",
            "SWT_VIRTUAL_DEVICE_TIME_SCALE": "6",
            "SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK": "false",
        }
    )

    slow_drain = virtual_device.VirtualDevice(slow_config)
    fast_drain = virtual_device.VirtualDevice(fast_config)
    slow_drain.rng.seed(123)
    fast_drain.rng.seed(123)

    slow_drain.update_simulation(60.0, services_online=True)
    fast_drain.update_simulation(60.0, services_online=True)

    assert fast_drain.level < slow_drain.level

    slow_fill = virtual_device.VirtualDevice(slow_config)
    fast_fill = virtual_device.VirtualDevice(fast_config)
    slow_fill.rng.seed(456)
    fast_fill.rng.seed(456)
    slow_fill.motor_on = True
    fast_fill.motor_on = True

    slow_fill.update_simulation(60.0, services_online=True)
    fast_fill.update_simulation(60.0, services_online=True)

    assert fast_fill.level > slow_fill.level


def test_sync_virtual_device_envs_to_local_registry_restores_deleted_devices(tmp_path):
    (tmp_path / "device.env").write_text("DB_FILE=data/test.db\n", encoding="utf-8")

    env_dir = tmp_path / "tests" / "virtual_devices" / "generated"
    env_dir.mkdir(parents=True)
    env_path_1 = env_dir / "device-001.env"
    env_path_2 = env_dir / "device-002.env"
    env_path_1.write_text("SWT_VIRTUAL_DEVICE_ID=swt-000-000-000-001\n", encoding="utf-8")
    env_path_2.write_text("SWT_VIRTUAL_DEVICE_ID=swt-000-000-000-002\n", encoding="utf-8")

    db_path = tmp_path / "data" / "test.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        cursor = db.cursor()
        virtual_device.ensure_local_device_registry_tables(cursor)
        cursor.execute(
            "INSERT INTO ignored_devices(device_id, note) VALUES (?, ?)",
            ("swt-000-000-000-001", "admin_delete"),
        )
        db.commit()

    result = virtual_device.sync_virtual_device_envs_to_local_registry(
        [env_path_1, env_path_2],
        base_environ={},
        project_root=tmp_path,
    )

    assert result["skipped"] is False
    assert result["unhidden_devices"] == 1
    assert result["registered_devices"] == 2

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        ignored_rows = db.execute("SELECT device_id FROM ignored_devices").fetchall()
        registered_rows = db.execute(
            "SELECT device_id, registration_source, key_rule FROM registered_devices ORDER BY device_id"
        ).fetchall()

    assert ignored_rows == []
    assert [dict(row) for row in registered_rows] == [
        {
            "device_id": "swt-000-000-000-001",
            "registration_source": "virtual_device_env",
            "key_rule": "tests/virtual_devices/generated/device-001.env",
        },
        {
            "device_id": "swt-000-000-000-002",
            "registration_source": "virtual_device_env",
            "key_rule": "tests/virtual_devices/generated/device-002.env",
        },
    ]


def test_virtual_device_build_override_env_normalizes_explicit_values():
    argv = [
        "--base-url", "127.0.0.1:8000",
        "--run-seconds", "20",
        "--time-scale", "8",
    ]
    args = virtual_device.build_parser().parse_args(argv)

    overrides = virtual_device.build_override_env(args, argv=argv)

    assert overrides == {
        "SWT_VIRTUAL_DEVICE_BASE_URL": "http://127.0.0.1:8000/",
        "SWT_VIRTUAL_DEVICE_RUN_SECONDS": "20.0",
        "SWT_VIRTUAL_DEVICE_TIME_SCALE": "8.0",
    }


def test_virtual_device_build_override_env_skips_defaults_without_cli_flags():
    args = virtual_device.build_parser().parse_args([])

    overrides = virtual_device.build_override_env(args, argv=[])

    assert overrides == {}


def test_resolve_virtual_device_env_paths_prefers_generated_dir(monkeypatch, tmp_path):
    virtual_dir = tmp_path / "tests" / "virtual_devices"
    generated_dir = virtual_dir / "generated"
    generated_dir.mkdir(parents=True)
    virtual_dir.mkdir(exist_ok=True)
    generated_env = generated_dir / "device-001.env"
    fallback_env = virtual_dir / "device-002.env"
    generated_env.write_text("SWT_VIRTUAL_DEVICE_ID=swt-generated-001\n", encoding="utf-8")
    fallback_env.write_text("SWT_VIRTUAL_DEVICE_ID=swt-fallback-002\n", encoding="utf-8")

    monkeypatch.setattr(virtual_device, "DEFAULT_GENERATED_VIRTUAL_DEVICE_ENV_DIR", generated_dir)
    monkeypatch.setattr(virtual_device, "DEFAULT_VIRTUAL_DEVICE_ENV_DIR", virtual_dir)
    monkeypatch.setattr(virtual_device, "LEGACY_VIRTUAL_DEVICE_ENV_PATH", tmp_path / "tests" / "virtual_device.env")

    args = virtual_device.build_parser().parse_args([])
    resolved = virtual_device.resolve_virtual_device_env_paths(
        args,
        cli_overrides_present=False,
        argv=[],
        environ={},
    )

    assert resolved == [generated_env]


def test_generate_virtual_device_envs_module_exposes_main():
    assert callable(generate_virtual_device_envs.main)
