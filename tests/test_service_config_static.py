from __future__ import annotations

from pathlib import Path


SERVER_SOURCE = (Path(__file__).resolve().parents[1] / "flask_app" / "server.py").read_text(encoding="utf-8")


def _function_block(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_fetch_device_service_config_selects_municipal_sensor_enabled():
    block = _function_block(
        SERVER_SOURCE,
        "def fetch_device_service_config(device_id, account=None, snapshot=None):",
        "def list_device_service_configs(device_ids=None, accounts_by_device=None, snapshots_by_device=None):",
    )
    assert "source_tank_monitoring_enabled, municipal_sensor_enabled, relay_enabled, ai_analysis_enabled" in block


def test_list_device_service_configs_selects_municipal_sensor_enabled():
    block = _function_block(
        SERVER_SOURCE,
        "def list_device_service_configs(device_ids=None, accounts_by_device=None, snapshots_by_device=None):",
        "def upsert_device_service_config(",
    )
    assert "source_tank_monitoring_enabled, municipal_sensor_enabled, relay_enabled, ai_analysis_enabled" in block
