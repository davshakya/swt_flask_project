#!/usr/bin/env python
"""Virtual Smart Water Tank MCU for Flask backend testing.

This emulator behaves like a device client:
- sends telemetry to `/status`
- polls `/device/command`
- acknowledges commands at `/device/command/ack`
- reports service connect/disconnect periods through telemetry fields

By default it identifies itself as a virtual device source using:
    X-Device-Source: virtual
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = PROJECT_ROOT / "tests"
DEFAULT_VIRTUAL_DEVICE_ENV_DIR = TESTS_ROOT / "virtual_devices"
DEFAULT_GENERATED_VIRTUAL_DEVICE_ENV_DIR = DEFAULT_VIRTUAL_DEVICE_ENV_DIR / "generated"
LEGACY_VIRTUAL_DEVICE_ENV_PATH = TESTS_ROOT / "virtual_device.env"


def parse_simple_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            values[key] = value
    except OSError:
        return {}
    return values


def resolve_shared_env_file_paths(project_root: Path = PROJECT_ROOT) -> list[Path]:
    return [
        project_root / "device.env",
        project_root / ".env",
    ]


def resolve_env_file_paths(project_root: Path = PROJECT_ROOT) -> list[Path]:
    return [
        *resolve_shared_env_file_paths(project_root),
        project_root / "tests" / "virtual_device.env",
    ]


def load_local_env_files(env_paths: list[Path] | None = None, environ: Any = None) -> None:
    target_env = os.environ if environ is None else environ
    original = set(target_env)
    loaded = set()
    for dotenv_path in (env_paths or resolve_env_file_paths()):
        for key, value in parse_simple_dotenv(dotenv_path).items():
            if key in original and key not in loaded:
                continue
            target_env[key] = value
            loaded.add(key)


def merge_env_files(env_paths: list[Path], base_environ: Any = None) -> dict[str, str]:
    merged = dict(os.environ if base_environ is None else base_environ)
    for dotenv_path in env_paths:
        merged.update(parse_simple_dotenv(dotenv_path))
    return merged


def resolve_runtime_path(raw_path: str | Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (Path.cwd() / path).resolve()


def discover_virtual_device_env_files(env_dir: Path) -> list[Path]:
    if not env_dir.exists() or not env_dir.is_dir():
        return []
    return sorted(
        path
        for path in env_dir.iterdir()
        if path.is_file() and path.suffix.lower() == ".env"
    )


load_local_env_files(resolve_shared_env_file_paths())


LOG = logging.getLogger("virtual-device")
DEVICE_SOURCE_VIRTUAL = "virtual"


def normalize_base_url(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("base URL is required")
    if "://" not in text:
        text = f"http://{text}"
    return text.rstrip("/") + "/"


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def env_text(*names: str, default: str = "", environ: Any = None) -> str:
    source = os.environ if environ is None else environ
    for name in names:
        value = source.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def env_float(*names: str, default: float, environ: Any = None) -> float:
    text = env_text(*names, default="", environ=environ)
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def env_int(*names: str, default: int, environ: Any = None) -> int:
    text = env_text(*names, default="", environ=environ)
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def env_flag(*names: str, default: bool, environ: Any = None) -> bool:
    text = env_text(*names, default="", environ=environ)
    if not text:
        return default
    normalized = text.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def env_choice(*names: str, allowed: set[str], default: str, environ: Any = None) -> str:
    text = env_text(*names, default="", environ=environ).lower()
    if text in allowed:
        return text
    return default


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def bool_to_env_text(value: bool) -> str:
    return "true" if value else "false"


def format_env_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return bool_to_env_text(value)
    return str(value)


def numeric_suffix_parts(value: str) -> tuple[str, int | None, int]:
    text = str(value or "").strip()
    match = re.search(r"(\d+)$", text)
    if not match:
        return text, None, 3
    digits = match.group(1)
    return text[: -len(digits)], int(digits), len(digits)


def format_sequenced_value(base_value: str, index: int) -> str:
    prefix, start_number, width = numeric_suffix_parts(base_value)
    if start_number is not None:
        return f"{prefix}{start_number + index - 1:0{width}d}"
    cleaned_prefix = prefix.rstrip("-_ ")
    separator = "" if not cleaned_prefix else "-"
    return f"{cleaned_prefix}{separator}{index:03d}" if cleaned_prefix else f"{index:03d}"


def wildcard_rule_for_generated_devices(base_device_id: str, shared_key: str) -> str:
    prefix, start_number, _width = numeric_suffix_parts(base_device_id)
    if start_number is not None and prefix:
        wildcard = f"{prefix}*"
    else:
        cleaned_prefix = str(base_device_id or "").strip().rstrip("-_ ")
        wildcard = f"{cleaned_prefix}-*" if cleaned_prefix else "virtual-device-*"
    return f"{wildcard}:{shared_key}"


@dataclass
class Config:
    base_url: str
    device_id: str
    device_key: str
    device_source: str
    device_local_url: str | None
    firmware_version: str
    tank_height_cm: float
    tank_capacity_liters: float
    telemetry_interval: float
    command_poll_interval: float
    loop_sleep: float
    run_seconds: float
    connected_seconds: float
    disconnected_seconds: float
    auto_start_percent: float
    auto_stop_percent: float
    source_min_run_percent: float
    start_level_percent: float
    start_source_level_percent: float
    usage_liters_per_hour: float
    fill_liters_per_hour: float
    source_recovery_liters_per_hour: float
    enable_source_tank: bool
    channel_mode: str
    seed: int
    require_active_source: bool


class VirtualDevice:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.rng = random.Random(config.seed)
        self.started_at = time.monotonic()
        self.last_step_at = self.started_at
        self.last_telemetry_at = 0.0
        self.last_command_poll_at = 0.0
        self.last_connectivity_state = self.connectivity_online(0.0)
        self.level = clamp(config.start_level_percent, 0.0, 100.0)
        self.source_level = (
            clamp(config.start_source_level_percent, 0.0, 100.0) if config.enable_source_tank else None
        )
        self.mode = "AUTO"
        self.motor_on = False
        self.manual_override: str | None = None
        self.current_runtime_s = 0.0
        self.last_runtime_s = 0.0
        self.total_runtime_s = 0.0
        self.uptime_s = 0.0
        self.free_heap = 48672
        self.reset_reason = "POWERON_RESET"
        self.note_text = "Virtual device booted and ready."
        self.note_tone = "info"
        self.note_until = 0.0
        self.calibrating_until = 0.0
        self.last_posted_service_state = "ON" if self.last_connectivity_state else "OFF"
        self.last_backend_unreachable_hint_at = 0.0
        self.last_source_mode_hint_at = 0.0

    def log(self, level: int, message: str, *args: Any) -> None:
        LOG.log(level, f"[%s] {message}", self.config.device_id, *args)

    def info(self, message: str, *args: Any) -> None:
        self.log(logging.INFO, message, *args)

    def warning(self, message: str, *args: Any) -> None:
        self.log(logging.WARNING, message, *args)

    @property
    def status_url(self) -> str:
        return urljoin(self.config.base_url, "status")

    @property
    def command_url(self) -> str:
        return urljoin(self.config.base_url, "device/command")

    @property
    def command_ack_url(self) -> str:
        return urljoin(self.config.base_url, "device/command/ack")

    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Device-Id": self.config.device_id,
            "X-Device-Key": self.config.device_key,
            "X-Device-Source": self.config.device_source,
        }

    def log_backend_unreachable_hint(self) -> None:
        now = time.monotonic()
        if (now - self.last_backend_unreachable_hint_at) < 30.0:
            return
        self.last_backend_unreachable_hint_at = now
        self.warning(
            "Backend %s is unreachable. Start Flask with `python server.py` or pass --base-url to a running backend.",
            self.config.base_url,
        )

    def log_source_mode_hint(self) -> None:
        now = time.monotonic()
        if (now - self.last_source_mode_hint_at) < 30.0:
            return
        self.last_source_mode_hint_at = now
        self.warning(
            "Backend command mode does not match this emulator source=%s. "
            "Set SWT_DEVICE_SOURCE_MODE=%s in device.env and restart Flask, "
            "or switch /admin/device-source-mode after logging in.",
            self.config.device_source,
            self.config.device_source,
        )

    def handle_request_exception(self, context: str, exc: requests.RequestException) -> None:
        self.warning("%s: %s", context, exc)
        message = str(exc)
        if any(
            marker in message
            for marker in (
                "Connection refused",
                "actively refused it",
                "Failed to establish a new connection",
                "Max retries exceeded",
            )
        ):
            self.log_backend_unreachable_hint()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def connectivity_online(self, elapsed_seconds: float) -> bool:
        if self.config.disconnected_seconds <= 0:
            return True
        connected = max(0.0, self.config.connected_seconds)
        disconnected = max(0.0, self.config.disconnected_seconds)
        cycle = connected + disconnected
        if cycle <= 0:
            return True
        if connected <= 0:
            return False
        return (elapsed_seconds % cycle) < connected

    def source_tank_can_run(self) -> bool:
        if not self.config.enable_source_tank:
            return True
        if self.source_level is None:
            return False
        return self.source_level >= self.config.source_min_run_percent

    def fill_percent_per_hour(self) -> float:
        return (self.config.fill_liters_per_hour / self.config.tank_capacity_liters) * 100.0

    def use_percent_per_hour(self) -> float:
        return (self.config.usage_liters_per_hour / self.config.tank_capacity_liters) * 100.0

    def source_recovery_percent_per_hour(self) -> float:
        return (self.config.source_recovery_liters_per_hour / self.config.tank_capacity_liters) * 100.0

    def set_note(self, text: str, tone: str = "info", ttl_seconds: float = 25.0) -> None:
        self.note_text = text
        self.note_tone = tone
        self.note_until = time.monotonic() + max(1.0, ttl_seconds)

    def compute_sensor_distance(self, level_percent: float | None) -> float | None:
        if level_percent is None:
            return None
        return round(self.config.tank_height_cm * (1.0 - (level_percent / 100.0)), 1)

    def update_simulation(self, dt: float, services_online: bool) -> None:
        if dt <= 0:
            return

        self.uptime_s += dt
        previous_motor = self.motor_on
        noise = self.rng.uniform(-0.04, 0.04)

        if self.motor_on:
            self.level = clamp(
                self.level + ((self.fill_percent_per_hour() / 3600.0) * dt) + noise,
                0.0,
                100.0,
            )
            self.current_runtime_s += dt
            self.total_runtime_s += dt
            if self.config.enable_source_tank and self.source_level is not None:
                self.source_level = clamp(
                    self.source_level - ((self.fill_percent_per_hour() / 3600.0) * dt) - abs(noise * 0.5),
                    0.0,
                    100.0,
                )
        else:
            self.level = clamp(
                self.level - ((self.use_percent_per_hour() / 3600.0) * dt) + noise,
                0.0,
                100.0,
            )
            if self.config.enable_source_tank and self.source_level is not None:
                self.source_level = clamp(
                    self.source_level + ((self.source_recovery_percent_per_hour() / 3600.0) * dt),
                    0.0,
                    100.0,
                )

        if self.config.enable_source_tank and self.source_level is not None and self.source_level <= 2.0 and self.motor_on:
            self.motor_on = False
            self.mode = "AUTO"
            self.manual_override = None
            self.set_note("Source tank ran too low. Pump stopped for dry-run protection.", tone="warn", ttl_seconds=35)

        if self.mode == "AUTO":
            if self.motor_on and (self.level >= self.config.auto_stop_percent or not self.source_tank_can_run()):
                self.motor_on = False
            elif (not self.motor_on) and self.level <= self.config.auto_start_percent and self.source_tank_can_run():
                self.motor_on = True
        elif self.manual_override == "ON":
            if self.level >= self.config.auto_stop_percent or not self.source_tank_can_run():
                self.motor_on = False
                self.mode = "AUTO"
                self.manual_override = None
                self.set_note("Manual start completed and control returned to AUTO.", tone="ok")
        elif self.manual_override == "OFF":
            if self.level <= self.config.auto_start_percent and self.source_tank_can_run():
                self.motor_on = True
                self.mode = "AUTO"
                self.manual_override = None
                self.set_note("Manual stop released and AUTO resumed at the low threshold.", tone="ok")

        if previous_motor and not self.motor_on:
            self.last_runtime_s = self.current_runtime_s
            self.current_runtime_s = 0.0
        elif (not previous_motor) and self.motor_on:
            self.current_runtime_s = 0.0

        heap_drift = self.rng.randint(-64, 64)
        self.free_heap = int(clamp(self.free_heap + heap_drift, 38000, 62000))

        if not services_online and self.note_until < time.monotonic():
            self.set_note("Relay link is offline. Telemetry will resume after reconnect.", tone="warn", ttl_seconds=10)

    def apply_command(self, command: str) -> None:
        normalized = str(command or "").strip().upper()
        now = time.monotonic()
        if not normalized:
            return

        if normalized == "ON":
            self.mode = "MANUAL"
            self.manual_override = "ON"
            if self.source_tank_can_run():
                self.motor_on = True
                self.set_note("Manual start accepted by the virtual device.", tone="warn")
            else:
                self.motor_on = False
                self.set_note("Manual start blocked because the source tank is below the run threshold.", tone="warn")
            return

        if normalized == "OFF":
            self.mode = "MANUAL"
            self.manual_override = "OFF"
            self.motor_on = False
            self.set_note("Manual stop accepted by the virtual device.", tone="warn")
            return

        if normalized == "AUTO":
            self.mode = "AUTO"
            self.manual_override = None
            self.set_note("AUTO control restored on the virtual device.", tone="info")
            return

        if normalized == "CALIBRATE":
            self.calibrating_until = now + 15.0
            self.set_note("Sensor calibration started on the virtual device.", tone="info")
            return

        if normalized.startswith("CONFIG:"):
            parts = normalized.split(":")
            if len(parts) == 3:
                try:
                    self.config.tank_height_cm = clamp(float(parts[1]), 30.0, 500.0)
                    self.config.tank_capacity_liters = clamp(float(parts[2]), 50.0, 50000.0)
                    self.set_note(
                        f"Tank config updated to {self.config.tank_height_cm:.1f} cm / {self.config.tank_capacity_liters:.1f} L.",
                        tone="info",
                        ttl_seconds=40,
                    )
                except ValueError:
                    self.set_note("Received CONFIG command with invalid values.", tone="warn", ttl_seconds=20)
            return

        self.set_note(f"Unsupported command received: {normalized}", tone="warn", ttl_seconds=20)

    def service_state(self, online: bool) -> str:
        return "ON" if online else "OFF"

    def lower_tank_service_state(self, online: bool) -> str:
        if not self.config.enable_source_tank:
            return "OFF"
        return self.service_state(online)

    def status_note(self, online: bool) -> tuple[str, str, str]:
        now = time.monotonic()
        if now < self.note_until:
            tone = self.note_tone if self.note_tone in {"ok", "warn", "bad", "info"} else "info"
            timer = (
                format_duration(max(0.0, self.calibrating_until - now))
                if now < self.calibrating_until
                else ("Stop near the high threshold." if self.motor_on else "Waiting for the next threshold change.")
            )
            return self.note_text, tone, timer

        if not online:
            return "Relay link is offline. Waiting to reconnect.", "warn", "Retrying cloud services."
        if now < self.calibrating_until:
            return "Sensor calibration is running on the virtual device.", "info", format_duration(self.calibrating_until - now)
        if self.mode == "MANUAL" and self.motor_on:
            return "Manual Start is active on the virtual device.", "warn", "Stop near the high threshold."
        if self.mode == "MANUAL":
            return "Manual Stop is active on the virtual device.", "warn", "Waiting for the low threshold."
        if self.motor_on:
            return "Auto fill is running on the virtual device.", "ok", "Stopping near the high threshold."
        return "Auto is waiting for the next start condition.", "info", "Waiting for the low threshold."

    def build_payload(self, online: bool) -> dict[str, Any]:
        auto_status, auto_tone, auto_timer = self.status_note(online)
        sensor_distance = self.compute_sensor_distance(self.level)
        lower_distance = self.compute_sensor_distance(self.source_level) if self.config.enable_source_tank else None
        usage_rate = round(self.config.usage_liters_per_hour, 2)
        tomorrow_prediction = round(self.config.usage_liters_per_hour * 24.0 * 1.05, 2)
        wifi_rssi = int(self.rng.randint(-68, -54) if online else -92)
        sensor_ok = online and time.monotonic() >= self.calibrating_until
        lower_tank_service = self.lower_tank_service_state(online)
        source_sensor_ok = lower_tank_service == "ON"
        dry_run = "YES" if self.config.enable_source_tank and self.source_level is not None and self.source_level <= 2.0 else "NO"
        fill_eta_seconds = None
        if self.motor_on and self.fill_percent_per_hour() > 0:
            fill_eta_seconds = max(
                0.0,
                ((self.config.auto_stop_percent - self.level) / self.fill_percent_per_hour()) * 3600.0,
            )

        return {
            "device_source": self.config.device_source,
            "level": round(self.level, 2),
            "motor": "ON" if self.motor_on else "OFF",
            "mode": self.mode,
            "runtime": format_duration(self.total_runtime_s),
            "current_runtime": format_duration(self.current_runtime_s) if self.motor_on else "0s",
            "last_runtime": format_duration(self.last_runtime_s) if self.last_runtime_s > 0 else "0s",
            "fill_time": format_duration(fill_eta_seconds) if fill_eta_seconds is not None else "--",
            "leak": "NO",
            "pump_failure": "NO",
            "abnormal": "NO",
            "drip": "NO",
            "slow_leak": "NO",
            "pipe_leak": "NO",
            "ai_usage_rate": usage_rate,
            "tomorrow_prediction": tomorrow_prediction,
            "dry_run": dry_run,
            "wifi": "ONLINE" if online else "OFFLINE",
            "wifi_rssi": wifi_rssi,
            "sensor": "OK" if sensor_ok else "DISCONNECTED",
            "sensor_info": (
                "Virtual sensor is calibrating."
                if time.monotonic() < self.calibrating_until
                else ("Virtual sensor responding normally." if sensor_ok else "Virtual sensor offline with the relay link.")
            ),
            "sensor_distance_cm": sensor_distance,
            "tank_height_cm": round(self.config.tank_height_cm, 1),
            "tank_capacity_liters": round(self.config.tank_capacity_liters, 1),
            "auto_status": auto_status,
            "auto_status_tone": auto_tone,
            "auto_timer": auto_timer,
            "free_heap": self.free_heap,
            "uptime_s": int(self.uptime_s),
            "lower_tank_level": round(self.source_level, 2) if self.source_level is not None else None,
            "lower_sensor": "OK" if source_sensor_ok else ("DISABLED" if not self.config.enable_source_tank else "DISCONNECTED"),
            "lower_sensor_info": (
                "Source tank disabled"
                if not self.config.enable_source_tank
                else ("Virtual source sensor responding normally." if source_sensor_ok else "Virtual source sensor offline with the relay link.")
            ),
            "lower_sensor_distance_cm": lower_distance,
            "firmware_version": self.config.firmware_version,
            "reset_reason": self.reset_reason,
            "device_local_url": self.config.device_local_url,
            "channel_mode": self.config.channel_mode,
            "telemetry_service": self.service_state(online),
            "command_service": self.service_state(online),
            "ota_service": self.service_state(online),
            "lower_tank_service": lower_tank_service,
        }

    def report_backend_mode(self) -> None:
        try:
            response = self.session.get(self.status_url, timeout=(5, 10))
            payload = response.json() if response.ok else {}
        except requests.RequestException as exc:
            self.handle_request_exception("Unable to read backend status before start", exc)
            return
        except ValueError:
            self.warning("Backend status response was not valid JSON.")
            return

        backend_mode = str(payload.get("device_source_mode") or "").strip().lower()
        if backend_mode:
            self.info("Backend source mode is %s", backend_mode)
            if backend_mode != self.config.device_source:
                message = (
                    f"Backend source mode is {backend_mode}, but this emulator is publishing as "
                    f"{self.config.device_source}. Switch the backend mode before command polling tests."
                )
                if self.config.require_active_source:
                    raise SystemExit(message)
                self.warning("%s", message)
                self.log_source_mode_hint()

    def post_telemetry(self, online: bool) -> bool:
        payload = self.build_payload(online)
        try:
            response = self.session.post(
                self.status_url,
                json=payload,
                headers=self.headers(),
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            self.handle_request_exception("Telemetry post failed", exc)
            return False

        if response.status_code in {401, 403}:
            raise SystemExit(f"Device authentication failed: HTTP {response.status_code}")

        if response.status_code == 409:
            self.warning("Backend rejected telemetry because source mode is inactive: %s", response.text)
            self.log_source_mode_hint()
            return False

        if not response.ok:
            self.warning("Telemetry post returned HTTP %s: %s", response.status_code, response.text)
            return False

        service_state = payload["telemetry_service"]
        if service_state != self.last_posted_service_state:
            self.info("Service state changed to %s", service_state)
            self.last_posted_service_state = service_state
        self.info(
            "Telemetry saved | source=%s level=%.1f%% motor=%s mode=%s wifi=%s lower_tank_service=%s",
            self.config.device_source,
            payload["level"],
            payload["motor"],
            payload["mode"],
            payload["wifi"],
            payload["lower_tank_service"],
        )
        return True

    def acknowledge_command(self, command_id: Any, command_source: Any) -> None:
        ack_payload = {
            "command_id": command_id,
            "command_source": command_source,
            "device_source": self.config.device_source,
        }
        try:
            response = self.session.post(
                self.command_ack_url,
                json=ack_payload,
                headers=self.headers(),
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            self.handle_request_exception("Command ack failed", exc)
            return

        if response.status_code in {401, 403}:
            raise SystemExit(f"Device authentication failed during ack: HTTP {response.status_code}")

        if response.status_code == 409:
            self.warning("Backend rejected command ack because source mode is inactive: %s", response.text)
            self.log_source_mode_hint()
            return

        if not response.ok:
            self.warning("Command ack returned HTTP %s: %s", response.status_code, response.text)
            return

        self.info("Acknowledged command %s from %s", command_id, command_source)

    def poll_command(self) -> bool:
        try:
            response = self.session.get(
                self.command_url,
                headers=self.headers(),
                timeout=(5, 15),
            )
        except requests.RequestException as exc:
            self.handle_request_exception("Command poll failed", exc)
            return False

        if response.status_code in {401, 403}:
            raise SystemExit(f"Device authentication failed during command poll: HTTP {response.status_code}")

        if response.status_code == 409:
            self.warning("Command poll rejected because backend source mode is different: %s", response.text)
            self.log_source_mode_hint()
            return False

        if not response.ok:
            self.warning("Command poll returned HTTP %s: %s", response.status_code, response.text)
            return False

        try:
            payload = response.json()
        except ValueError:
            self.warning("Command poll returned invalid JSON.")
            return False

        command = str(payload.get("command") or "").strip().upper()
        command_id = payload.get("command_id")
        command_source = payload.get("command_source") or "queue"
        if not command:
            return False

        self.info("Received command %s (%s)", command, command_source)
        self.apply_command(command)
        self.acknowledge_command(command_id, command_source)
        return True

    def run(self, stop_event: threading.Event | None = None) -> None:
        self.report_backend_mode()
        force_telemetry = True

        while True:
            if stop_event and stop_event.is_set():
                return

            now = time.monotonic()
            elapsed = now - self.started_at
            dt = min(max(now - self.last_step_at, 0.0), 5.0)
            self.last_step_at = now
            online = self.connectivity_online(elapsed)

            if online != self.last_connectivity_state:
                self.last_connectivity_state = online
                state_label = "online" if online else "offline"
                self.set_note(f"Relay services changed to {state_label}.", tone="warn" if not online else "ok", ttl_seconds=15)
                force_telemetry = True

            self.update_simulation(dt, online)

            if online and (now - self.last_command_poll_at) >= self.config.command_poll_interval:
                if self.poll_command():
                    force_telemetry = True
                self.last_command_poll_at = now

            should_send = force_telemetry or (online and (now - self.last_telemetry_at) >= self.config.telemetry_interval)
            if should_send:
                if self.post_telemetry(online):
                    self.last_telemetry_at = now
                force_telemetry = False

            if self.config.run_seconds > 0 and elapsed >= self.config.run_seconds:
                self.info("Run duration reached. Stopping emulator.")
                return

            if stop_event:
                if stop_event.wait(self.config.loop_sleep):
                    return
            else:
                time.sleep(self.config.loop_sleep)


def build_parser(environ: Any = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a virtual Smart Water Tank device against the Flask backend.",
    )
    parser.add_argument(
        "--env-dir",
        default=env_text(
            "SWT_VIRTUAL_DEVICE_ENV_DIR",
            default=str(DEFAULT_VIRTUAL_DEVICE_ENV_DIR),
            environ=environ,
        ),
        help="Directory containing one .env file per virtual device.",
    )
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Run device configs from one or more explicit .env files. May be passed multiple times.",
    )
    parser.add_argument(
        "--device_count",
        "--device-count",
        dest="device_count",
        type=int,
        default=0,
        help="Auto-generate and run N virtual device env files in tests/virtual_devices/generated.",
    )
    parser.add_argument(
        "--base-url",
        default=env_text(
            "SWT_VIRTUAL_DEVICE_BASE_URL",
            "SWT_LOCAL_FLASK_BASE_URL",
            "SWT_CLOUD_BASE_URL",
            default="http://127.0.0.1:8000/",
            environ=environ,
        ),
        help="Flask backend base URL. Defaults to SWT_VIRTUAL_DEVICE_BASE_URL, then shared Flask URL settings.",
    )
    parser.add_argument(
        "--device-id",
        default=env_text("SWT_VIRTUAL_DEVICE_ID", "SWT_DEVICE_ID", default="swt-node-01", environ=environ),
        help="Device ID used in X-Device-Id and telemetry payloads.",
    )
    parser.add_argument(
        "--device-key",
        default=env_text("SWT_VIRTUAL_DEVICE_KEY", "SWT_DEVICE_API_KEY", default="", environ=environ),
        help="Device API key used in X-Device-Key.",
    )
    parser.add_argument(
        "--device-source",
        default=env_choice(
            "SWT_VIRTUAL_DEVICE_SOURCE",
            allowed={"real", "virtual"},
            default=DEVICE_SOURCE_VIRTUAL,
            environ=environ,
        ),
        choices=["real", "virtual"],
        help="Device source tag sent to the backend. Use virtual for emulator testing.",
    )
    parser.add_argument(
        "--device-local-url",
        default=env_text("SWT_VIRTUAL_DEVICE_LOCAL_URL", "SWT_LOCAL_DEVICE_URL", default="", environ=environ),
        help="Local device URL to publish with telemetry.",
    )
    parser.add_argument(
        "--firmware-version",
        default=env_text("SWT_VIRTUAL_DEVICE_FIRMWARE_VERSION", default="virtual-mcu-1.0.0", environ=environ),
    )
    parser.add_argument(
        "--tank-height-cm",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_TANK_HEIGHT_CM", default=180.0, environ=environ),
    )
    parser.add_argument(
        "--tank-capacity-liters",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_TANK_CAPACITY_LITERS", default=1000.0, environ=environ),
    )
    parser.add_argument(
        "--start-level-percent",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_START_LEVEL_PERCENT", default=62.0, environ=environ),
    )
    parser.add_argument(
        "--start-source-level-percent",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_START_SOURCE_LEVEL_PERCENT", default=74.0, environ=environ),
    )
    parser.add_argument(
        "--auto-start-percent",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_AUTO_START_PERCENT", default=28.0, environ=environ),
    )
    parser.add_argument(
        "--auto-stop-percent",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_AUTO_STOP_PERCENT", default=92.0, environ=environ),
    )
    parser.add_argument(
        "--source-min-run-percent",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_SOURCE_MIN_RUN_PERCENT", default=18.0, environ=environ),
    )
    parser.add_argument(
        "--usage-liters-per-hour",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_USAGE_LITERS_PER_HOUR", default=34.0, environ=environ),
    )
    parser.add_argument(
        "--fill-liters-per-hour",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_FILL_LITERS_PER_HOUR", default=180.0, environ=environ),
    )
    parser.add_argument(
        "--source-recovery-liters-per-hour",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_SOURCE_RECOVERY_LITERS_PER_HOUR", default=18.0, environ=environ),
    )
    parser.add_argument(
        "--telemetry-interval",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_TELEMETRY_INTERVAL", default=5.0, environ=environ),
    )
    parser.add_argument(
        "--command-poll-interval",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_COMMAND_POLL_INTERVAL", default=2.5, environ=environ),
    )
    parser.add_argument(
        "--loop-sleep",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_LOOP_SLEEP", default=0.5, environ=environ),
    )
    parser.add_argument(
        "--run-seconds",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_RUN_SECONDS", default=0.0, environ=environ),
        help="Run for N seconds. Use 0 to run forever.",
    )
    parser.add_argument(
        "--connected-seconds",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_CONNECTED_SECONDS", default=120.0, environ=environ),
    )
    parser.add_argument(
        "--disconnected-seconds",
        type=float,
        default=env_float("SWT_VIRTUAL_DEVICE_DISCONNECTED_SECONDS", default=30.0, environ=environ),
        help="When greater than 0, the emulator cycles through connected/offline periods.",
    )
    parser.add_argument(
        "--channel-mode",
        default=env_choice(
            "SWT_VIRTUAL_DEVICE_CHANNEL_MODE",
            "SWT_FLASK_CHANNEL_MODE",
            allowed={"both", "cloud", "local"},
            default="both",
            environ=environ,
        ),
        choices=["both", "cloud", "local"],
        help="Reported device channel mode.",
    )
    parser.add_argument("--seed", type=int, default=env_int("SWT_VIRTUAL_DEVICE_SEED", default=42, environ=environ))
    parser.add_argument(
        "--enable-source-tank",
        action=argparse.BooleanOptionalAction,
        default=env_flag("SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK", default=True, environ=environ),
        help="Enable lower/source tank telemetry and interlock behavior.",
    )
    parser.add_argument(
        "--require-active-source",
        action="store_true",
        default=env_flag("SWT_VIRTUAL_DEVICE_REQUIRE_ACTIVE_SOURCE", default=False, environ=environ),
        help="Exit early if backend /status shows a different device_source_mode.",
    )
    parser.add_argument(
        "--log-level",
        default=env_choice(
            "SWT_VIRTUAL_DEVICE_LOG_LEVEL",
            allowed={"debug", "info", "warning", "error"},
            default="info",
            environ=environ,
        ).upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def build_config(args: argparse.Namespace) -> Config:
    device_key = str(args.device_key or "").strip()
    if not device_key:
        raise SystemExit("device key is required. Pass --device-key or set SWT_DEVICE_API_KEY.")
    return Config(
        base_url=normalize_base_url(args.base_url),
        device_id=str(args.device_id or "").strip(),
        device_key=device_key,
        device_source=str(args.device_source or DEVICE_SOURCE_VIRTUAL).strip().lower(),
        device_local_url=str(args.device_local_url or "").strip() or None,
        firmware_version=str(args.firmware_version or "").strip() or "virtual-mcu-1.0.0",
        tank_height_cm=clamp(float(args.tank_height_cm), 30.0, 500.0),
        tank_capacity_liters=clamp(float(args.tank_capacity_liters), 50.0, 50000.0),
        telemetry_interval=max(1.0, float(args.telemetry_interval)),
        command_poll_interval=max(0.5, float(args.command_poll_interval)),
        loop_sleep=max(0.1, float(args.loop_sleep)),
        run_seconds=max(0.0, float(args.run_seconds)),
        connected_seconds=max(0.0, float(args.connected_seconds)),
        disconnected_seconds=max(0.0, float(args.disconnected_seconds)),
        auto_start_percent=clamp(float(args.auto_start_percent), 1.0, 99.0),
        auto_stop_percent=clamp(float(args.auto_stop_percent), 1.0, 99.0),
        source_min_run_percent=clamp(float(args.source_min_run_percent), 0.0, 100.0),
        start_level_percent=clamp(float(args.start_level_percent), 0.0, 100.0),
        start_source_level_percent=clamp(float(args.start_source_level_percent), 0.0, 100.0),
        usage_liters_per_hour=max(0.1, float(args.usage_liters_per_hour)),
        fill_liters_per_hour=max(0.1, float(args.fill_liters_per_hour)),
        source_recovery_liters_per_hour=max(0.0, float(args.source_recovery_liters_per_hour)),
        enable_source_tank=bool(args.enable_source_tank),
        channel_mode=str(args.channel_mode or "both").strip().lower(),
        seed=int(args.seed),
        require_active_source=bool(args.require_active_source),
    )


def build_config_from_env(env_values: dict[str, str]) -> Config:
    env_args = build_parser(environ=env_values).parse_args([])
    return build_config(env_args)


def generated_device_env_values(args: argparse.Namespace, index: int) -> dict[str, str]:
    return {
        "SWT_VIRTUAL_DEVICE_BASE_URL": normalize_base_url(args.base_url),
        "SWT_VIRTUAL_DEVICE_ID": format_sequenced_value(args.device_id, index),
        "SWT_VIRTUAL_DEVICE_KEY": str(args.device_key or "").strip(),
        "SWT_VIRTUAL_DEVICE_SOURCE": str(args.device_source or DEVICE_SOURCE_VIRTUAL).strip().lower(),
        "SWT_VIRTUAL_DEVICE_LOCAL_URL": str(args.device_local_url or "").strip(),
        "SWT_VIRTUAL_DEVICE_FIRMWARE_VERSION": str(args.firmware_version or "").strip() or "virtual-mcu-1.0.0",
        "SWT_VIRTUAL_DEVICE_TANK_HEIGHT_CM": format_env_scalar(args.tank_height_cm),
        "SWT_VIRTUAL_DEVICE_TANK_CAPACITY_LITERS": format_env_scalar(args.tank_capacity_liters),
        "SWT_VIRTUAL_DEVICE_START_LEVEL_PERCENT": format_env_scalar(args.start_level_percent),
        "SWT_VIRTUAL_DEVICE_START_SOURCE_LEVEL_PERCENT": format_env_scalar(args.start_source_level_percent),
        "SWT_VIRTUAL_DEVICE_AUTO_START_PERCENT": format_env_scalar(args.auto_start_percent),
        "SWT_VIRTUAL_DEVICE_AUTO_STOP_PERCENT": format_env_scalar(args.auto_stop_percent),
        "SWT_VIRTUAL_DEVICE_SOURCE_MIN_RUN_PERCENT": format_env_scalar(args.source_min_run_percent),
        "SWT_VIRTUAL_DEVICE_USAGE_LITERS_PER_HOUR": format_env_scalar(args.usage_liters_per_hour),
        "SWT_VIRTUAL_DEVICE_FILL_LITERS_PER_HOUR": format_env_scalar(args.fill_liters_per_hour),
        "SWT_VIRTUAL_DEVICE_SOURCE_RECOVERY_LITERS_PER_HOUR": format_env_scalar(args.source_recovery_liters_per_hour),
        "SWT_VIRTUAL_DEVICE_TELEMETRY_INTERVAL": format_env_scalar(args.telemetry_interval),
        "SWT_VIRTUAL_DEVICE_COMMAND_POLL_INTERVAL": format_env_scalar(args.command_poll_interval),
        "SWT_VIRTUAL_DEVICE_LOOP_SLEEP": format_env_scalar(args.loop_sleep),
        "SWT_VIRTUAL_DEVICE_RUN_SECONDS": format_env_scalar(args.run_seconds),
        "SWT_VIRTUAL_DEVICE_CONNECTED_SECONDS": format_env_scalar(args.connected_seconds),
        "SWT_VIRTUAL_DEVICE_DISCONNECTED_SECONDS": format_env_scalar(args.disconnected_seconds),
        "SWT_VIRTUAL_DEVICE_CHANNEL_MODE": str(args.channel_mode or "both").strip().lower(),
        "SWT_VIRTUAL_DEVICE_SEED": str(int(args.seed) + index - 1),
        "SWT_VIRTUAL_DEVICE_ENABLE_SOURCE_TANK": bool_to_env_text(bool(args.enable_source_tank)),
        "SWT_VIRTUAL_DEVICE_REQUIRE_ACTIVE_SOURCE": bool_to_env_text(bool(args.require_active_source)),
        "SWT_VIRTUAL_DEVICE_LOG_LEVEL": str(args.log_level or "INFO").upper(),
    }


def write_virtual_device_env_file(path: Path, env_values: dict[str, str]) -> None:
    lines = [
        "# Auto-generated by scripts/virtual_device.py",
        "# Edit carefully: running --device_count again may refresh these files.",
        "",
    ]
    for key, value in env_values.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_virtual_device_env_files(args: argparse.Namespace) -> list[Path]:
    if args.device_count <= 0:
        return []

    if args.env_file:
        raise SystemExit("--device_count cannot be combined with --env-file")

    if not str(args.device_key or "").strip():
        raise SystemExit("device key is required to generate virtual device env files.")

    env_root = resolve_runtime_path(args.env_dir)
    generated_dir = env_root / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)

    for existing in generated_dir.glob("device-*.env"):
        try:
            existing.unlink()
        except OSError:
            pass

    env_paths: list[Path] = []
    for index in range(1, int(args.device_count) + 1):
        env_path = generated_dir / f"device-{index:03d}.env"
        write_virtual_device_env_file(env_path, generated_device_env_values(args, index))
        env_paths.append(env_path)

    LOG.info(
        "Generated %s virtual device env files in %s",
        len(env_paths),
        generated_dir,
    )
    LOG.info(
        "Backend multi-device auth hint: SWT_DEVICE_KEYS=%s",
        wildcard_rule_for_generated_devices(args.device_id, str(args.device_key or "").strip()),
    )
    return env_paths


def has_explicit_device_cli_overrides(argv: list[str] | None = None) -> bool:
    tokens = list(sys.argv[1:] if argv is None else argv)
    override_prefixes = (
        "--base-url",
        "--device-id",
        "--device-key",
        "--device-source",
        "--device-local-url",
        "--firmware-version",
        "--tank-height-cm",
        "--tank-capacity-liters",
        "--start-level-percent",
        "--start-source-level-percent",
        "--auto-start-percent",
        "--auto-stop-percent",
        "--source-min-run-percent",
        "--usage-liters-per-hour",
        "--fill-liters-per-hour",
        "--source-recovery-liters-per-hour",
        "--telemetry-interval",
        "--command-poll-interval",
        "--loop-sleep",
        "--run-seconds",
        "--connected-seconds",
        "--disconnected-seconds",
        "--channel-mode",
        "--seed",
        "--enable-source-tank",
        "--no-enable-source-tank",
        "--require-active-source",
    )
    return any(any(token == prefix or token.startswith(f"{prefix}=") for prefix in override_prefixes) for token in tokens)


def resolve_virtual_device_env_paths(args: argparse.Namespace, cli_overrides_present: bool = False) -> list[Path]:
    if args.env_file:
        env_paths = [resolve_runtime_path(item) for item in args.env_file]
        missing = [str(path) for path in env_paths if not path.exists() or not path.is_file()]
        if missing:
            raise SystemExit(f"virtual device env file not found: {', '.join(missing)}")
        return env_paths

    if cli_overrides_present:
        return []

    env_dir = resolve_runtime_path(args.env_dir)
    discovered = discover_virtual_device_env_files(env_dir)
    if discovered:
        return discovered

    if LEGACY_VIRTUAL_DEVICE_ENV_PATH.exists():
        return [LEGACY_VIRTUAL_DEVICE_ENV_PATH]

    return []


def start_virtual_device(config: Config, stop_event: threading.Event | None = None) -> None:
    LOG.info(
        "Starting virtual device %s against %s as source=%s",
        config.device_id,
        config.base_url,
        config.device_source,
    )
    VirtualDevice(config).run(stop_event=stop_event)


def run_multi_device_configs(configs: list[Config], env_paths: list[Path]) -> int:
    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    errors: list[tuple[str, str]] = []
    error_lock = threading.Lock()

    def worker(config: Config, env_path: Path) -> None:
        try:
            start_virtual_device(config, stop_event=stop_event)
        except KeyboardInterrupt:
            stop_event.set()
        except SystemExit as exc:
            stop_event.set()
            with error_lock:
                errors.append((config.device_id, str(exc)))
            LOG.error("[%s] Emulator stopped: %s (env file: %s)", config.device_id, exc, env_path)
        except Exception:
            stop_event.set()
            with error_lock:
                errors.append((config.device_id, f"unexpected error in {env_path.name}"))
            LOG.exception("[%s] Emulator failed from env file %s", config.device_id, env_path)

    for config, env_path in zip(configs, env_paths):
        thread = threading.Thread(
            target=worker,
            args=(config, env_path),
            name=f"virtual-device-{config.device_id}",
            daemon=True,
        )
        threads.append(thread)
        thread.start()

    try:
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.2)
    except KeyboardInterrupt:
        LOG.info("Stopping %s virtual devices.", len(configs))
        stop_event.set()
        for thread in threads:
            thread.join(timeout=2.0)
        return 0

    return 1 if errors else 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.device_count < 0:
        raise SystemExit("--device_count must be 0 or greater")
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if args.device_count > 0:
        env_paths = generate_virtual_device_env_files(args)
    else:
        env_paths = resolve_virtual_device_env_paths(
            args,
            cli_overrides_present=has_explicit_device_cli_overrides(),
        )

    if env_paths:
        configs = [
            build_config_from_env(merge_env_files([env_path], base_environ=os.environ))
            for env_path in env_paths
        ]
        if len(configs) == 1:
            LOG.info("Using virtual device env file %s", env_paths[0])
            try:
                start_virtual_device(configs[0])
            except KeyboardInterrupt:
                LOG.info("Virtual device stopped by user.")
                return 0
            return 0

        LOG.info(
            "Using %s virtual device env files from %s",
            len(env_paths),
            env_paths[0].parent,
        )
        return run_multi_device_configs(configs, env_paths)

    config = build_config(args)
    try:
        start_virtual_device(config)
    except KeyboardInterrupt:
        LOG.info("Virtual device stopped by user.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
