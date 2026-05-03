from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_CWD = PROJECT_ROOT / "tests"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_root_gunicorn_config_loads_from_any_working_directory(monkeypatch):
    monkeypatch.chdir(WORKSPACE_CWD)

    config = runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))

    assert config["bind"] == "0.0.0.0:8000"
    assert config["workers"] == 1
    assert config["threads"] == 4


def test_root_gunicorn_config_falls_back_when_numeric_env_values_are_invalid(monkeypatch):
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("WEB_CONCURRENCY", "oops")
    monkeypatch.setenv("GUNICORN_THREADS", "")
    monkeypatch.setenv("GUNICORN_TIMEOUT", "bad")
    monkeypatch.setenv("GUNICORN_GRACEFUL_TIMEOUT", "bad")
    monkeypatch.setenv("GUNICORN_KEEPALIVE", "bad")

    config = runpy.run_path(str(PROJECT_ROOT / "gunicorn.conf.py"))

    assert config["bind"] == "0.0.0.0:8000"
    assert config["workers"] == 1
    assert config["threads"] == 4
    assert config["timeout"] == 120
    assert config["graceful_timeout"] == 30
    assert config["keepalive"] == 5


def test_root_gunicorn_config_loads_without_flask_app_package_on_sys_path():
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    code = f"""
import json
import runpy
import sys

project_root = r"{PROJECT_ROOT}"
sys.path = [entry for entry in sys.path if entry not in ("", project_root)]
config = runpy.run_path(r"{PROJECT_ROOT / 'gunicorn.conf.py'}")
print(json.dumps({{"bind": config["bind"], "workers": config["workers"], "threads": config["threads"]}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=WORKSPACE_CWD,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload == {"bind": "0.0.0.0:8000", "workers": 1, "threads": 4}


def test_server_import_falls_back_when_startup_numeric_env_values_are_invalid():
    env = os.environ.copy()
    env.update(
        {
            "SESSION_LIFETIME_HOURS": "",
            "TANK_CAPACITY_LITERS": "not-a-number",
            "DATA_RETENTION_DAYS": "bad",
            "DB_TARGET_SIZE_MB": "",
            "MQTT_BROKER_PORT": "bad-port",
            "MQTT_KEEPALIVE_SEC": "",
            "MQTT_QOS": "bad",
            "SNAPSHOT_CACHE_TTL_SECONDS": "",
        }
    )

    code = """
import json
import server

print(json.dumps({
    "session_hours": server.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds() / 3600,
    "tank_capacity_liters": server.TANK_CAPACITY_LITERS,
    "data_retention_days": server.DATA_RETENTION_DAYS,
    "db_target_size_mb": server.DB_TARGET_SIZE_MB,
    "mqtt_broker_port": server.MQTT_BROKER_PORT,
    "mqtt_keepalive_sec": server.MQTT_KEEPALIVE_SEC,
    "mqtt_qos": server.MQTT_QOS,
    "snapshot_cache_ttl_seconds": server.SNAPSHOT_CACHE_TTL_SECONDS,
}))
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())

    assert payload == {
        "session_hours": 12.0,
        "tank_capacity_liters": 1000.0,
        "data_retention_days": 7,
        "db_target_size_mb": 0.0,
        "mqtt_broker_port": 1883,
        "mqtt_keepalive_sec": 30,
        "mqtt_qos": 1,
        "snapshot_cache_ttl_seconds": 2.0,
    }


def test_local_runner_starts_dev_server_when_executed_as_main(monkeypatch):
    import flask

    captured = {}

    def fake_run(self, host=None, port=None, threaded=None, **kwargs):
        captured["host"] = host
        captured["port"] = port
        captured["threaded"] = threaded
        captured["extra"] = kwargs

    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setattr(flask.Flask, "run", fake_run)

    runpy.run_path(str(PROJECT_ROOT / "run_local.py"), run_name="__main__")

    assert captured == {
        "host": "0.0.0.0",
        "port": 8123,
        "threaded": True,
        "extra": {},
    }
