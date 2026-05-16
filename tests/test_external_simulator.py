import argparse
import http.client
from pathlib import Path
import random
import sys
import urllib.error

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

external_tank_simulator = pytest.importorskip(
    "external_tank_simulator",
    reason="external_tank_simulator.py is not present under scripts/ in this checkout.",
)

FirmwareClient = external_tank_simulator.FirmwareClient
SimulatorState = external_tank_simulator.SimulatorState
advance_main_tank_level = external_tank_simulator.advance_main_tank_level
advance_source_tank_level = external_tank_simulator.advance_source_tank_level
build_feed_payload = external_tank_simulator.build_feed_payload
parse_args = external_tank_simulator.parse_args
run = external_tank_simulator.run


def test_advance_main_tank_level_drops_when_motor_is_off():
    next_level = advance_main_tank_level(
        current_level_percent=60.0,
        capacity_liters=1000.0,
        motor_on=False,
        delta_seconds=60.0,
        usage_rate_lpm=6.0,
    )

    assert next_level == 59.4


def test_advance_main_tank_level_rises_when_motor_fill_exceeds_usage():
    next_level = advance_main_tank_level(
        current_level_percent=40.0,
        capacity_liters=1000.0,
        motor_on=True,
        delta_seconds=60.0,
        pump_flow_lpm=120.0,
        usage_rate_lpm=6.0,
    )

    assert next_level == 51.4


def test_advance_source_tank_level_refills_when_motor_is_off():
    next_level = advance_source_tank_level(
        current_level_percent=50.0,
        capacity_liters=1000.0,
        motor_on=False,
        delta_seconds=60.0,
        refill_rate_lpm=8.0,
    )

    assert next_level == 50.8


def test_build_feed_payload_only_pushes_enabled_simulators():
    snapshot = {
        "device_id": "swt-000-000-000-001",
        "motor": "OFF",
        "simulator": "ON",
        "source_tank_simulator": "OFF",
        "level": 42.0,
        "lower_tank_level": 88.0,
        "tank_capacity_liters": 1000.0,
    }
    state = SimulatorState(main_level=42.0, source_level=88.0, last_tick_at=None)

    payload = build_feed_payload(snapshot, state, delta_seconds=1.0, rng=random.Random(42))

    assert "main_level" in payload
    assert payload["main_sensor"] == "ok"
    assert "source_level" not in payload
    assert state.source_level == 88.0


def test_build_feed_payload_time_scale_accelerates_level_change():
    snapshot = {
        "device_id": "swt-000-000-000-001",
        "motor": "OFF",
        "simulator": "ON",
        "source_tank_simulator": "OFF",
        "level": 60.0,
        "lower_tank_level": 88.0,
        "tank_capacity_liters": 1000.0,
    }

    normal_state = SimulatorState(main_level=60.0, source_level=88.0, last_tick_at=None)
    fast_state = SimulatorState(main_level=60.0, source_level=88.0, last_tick_at=None)

    normal_payload = build_feed_payload(
        snapshot,
        normal_state,
        delta_seconds=1.0,
        rng=random.Random(42),
        time_scale=1.0,
        usage_base_lpm=6.0,
        usage_jitter_lpm=0.0,
    )
    fast_payload = build_feed_payload(
        snapshot,
        fast_state,
        delta_seconds=1.0,
        rng=random.Random(42),
        time_scale=6.0,
        usage_base_lpm=6.0,
        usage_jitter_lpm=0.0,
    )

    assert float(fast_payload["main_level"]) < float(normal_payload["main_level"])


def test_parse_args_uses_shorter_default_timeout():
    args = parse_args([])

    assert args.timeout_seconds == 5.0


def test_request_wraps_incomplete_read_as_url_error(monkeypatch):
    class FakeHeaders:
        @staticmethod
        def get_content_charset():
            return "utf-8"

    class FakeResponse:
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            raise http.client.IncompleteRead(b'{"ok": true}', 26)

    def fake_urlopen(request, timeout):
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = FirmwareClient("http://swt-000-000-000-002.local", "user", "pass", 12.0)

    with pytest.raises(urllib.error.URLError, match="incomplete response"):
        client.fetch_status()


def test_run_stops_cleanly_on_keyboard_interrupt_during_sleep(monkeypatch, capsys):
    args = argparse.Namespace(
        password="secret",
        device_url="http://swt-000-000-000-002.local",
        username="swt-000-000-000-001",
        interval_seconds=1.0,
        timeout_seconds=12.0,
        max_step_seconds=5.0,
        time_scale=6.0,
        pump_flow_lpm=120.0,
        usage_base_lpm=6.0,
        usage_jitter_lpm=2.0,
        source_refill_base_lpm=8.0,
        source_refill_jitter_lpm=2.5,
        seed=42,
    )

    monkeypatch.setattr(
        FirmwareClient,
        "fetch_status",
        lambda self: {
            "device_id": "swt-000-000-000-001",
            "motor": "OFF",
            "simulator": "OFF",
            "source_tank_simulator": "OFF",
            "level": 60.0,
            "lower_tank_level": 88.0,
            "tank_capacity_liters": 1000.0,
        },
    )
    monkeypatch.setattr("time.sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert run(args) == 0
    assert "Simulator stopped." in capsys.readouterr().out
