import atexit
from datetime import datetime, timedelta, timezone
from functools import wraps
import base64
import csv
import hashlib
import ipaddress
import io
import json
import logging
import os
from pathlib import Path
import binascii
import secrets
import sqlite3
import subprocess
import time
import threading
import math

pd = None
PANDAS_IMPORT_ERROR = None
import requests
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None
from flask import Flask, abort, g, jsonify, redirect, render_template, render_template_string, request, send_from_directory, session, url_for
from flask import Response
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jinja2 import TemplateNotFound
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from flask_app.runtime_utils import (
    db_parent_is_writable,
    env_float,
    env_int,
    env_flag as runtime_env_flag,
    load_dotenv_values,
    normalize_db_path as runtime_normalize_db_path,
    normalize_http_base_url as runtime_normalize_http_base_url,
    parse_simple_dotenv,
)


def load_local_env_files():
    module_root = Path(__file__).resolve().parent
    project_root = module_root.parent
    load_dotenv_values(
        (project_root / ".env", module_root / ".env", project_root / "device.env"),
        environ=os.environ,
        preserve_existing=True,
    )


load_local_env_files()

DEFAULT_APP_SECRET_KEY = "change-me-before-production"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "change-me-admin-password"
DEFAULT_DEVICE_KEY = "change-me-device-key"
DEFAULT_DEVICE_KEYS = f"swt-node-01:{DEFAULT_DEVICE_KEY}"
DATE_ONLY_FORMAT = "%Y-%m-%d"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
MOBILE_TOKEN_SALT = "smart-water-tank-mobile"
APP_SECRET_KEY_SETTING = "app_secret_key"
DASHBOARD_PASSWORD_SETTING = "dashboard_password"
DEVICE_SOURCE_MODE_SETTING = "device_source_mode"
CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV = "CUSTOMER_ACCOUNTS_BOOTSTRAP_B64"
DASHBOARD_PASSWORD_HASH_ENV = "DASHBOARD_PASSWORD_HASH"
DEVICE_SOURCE_REAL = "real"
DEVICE_SOURCE_VIRTUAL = "virtual"
DEVICE_SOURCE_HEADER = "X-Device-Source"
IS_RENDER = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))
APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
STATIC_DIR = APP_ROOT / "static"
RENDER_PERSISTENT_DB_PATH = Path("/var/data/tank.db")


def normalize_db_path(raw_path):
    return runtime_normalize_db_path(raw_path, project_root=PROJECT_ROOT)


def normalize_http_base_url(value):
    return runtime_normalize_http_base_url(value)


def env_flag(name, default=False):
    return runtime_env_flag(name, default=default, environ=os.environ)


def strip_ip_address_fields(payload, keep_device_local_url=False):
    if payload is None:
        return None
    cleaned = dict(payload)
    cleaned["source_ip"] = None
    if not keep_device_local_url:
        cleaned["device_local_url"] = None
    return cleaned


def is_render_persistent_db_path(db_path):
    try:
        return db_path.resolve() == RENDER_PERSISTENT_DB_PATH.resolve()
    except OSError:
        return str(db_path).startswith(str(RENDER_PERSISTENT_DB_PATH.parent))


def render_persistent_db_active(db_path=None):
    if not IS_RENDER:
        return False
    candidate = normalize_db_path(str(db_path or DB_FILE))
    return is_render_persistent_db_path(candidate)


def resolve_db_path():
    configured_path = os.environ.get("DB_FILE", "").strip()
    candidates = []
    if IS_RENDER:
        persistent_path = str(RENDER_PERSISTENT_DB_PATH)
        if configured_path:
            normalized_configured_path = normalize_db_path(configured_path)
            if is_render_persistent_db_path(normalized_configured_path):
                candidates.append((configured_path, "env"))
            else:
                candidates.append((persistent_path, "render-persistent-preferred"))
                candidates.append((configured_path, "env"))
        else:
            candidates.append((persistent_path, "render-default"))
        candidates.append(("/tmp/smart-water-tank/tank.db", "render-fallback"))
    elif configured_path:
        candidates.append((configured_path, "env"))
    else:
        candidates.append(("data/tank.db", "project-default"))

    rejected = []
    for raw_path, source in candidates:
        db_path = normalize_db_path(raw_path)
        if db_parent_is_writable(db_path):
            return db_path, source, rejected
        rejected.append({"source": source, "path": str(db_path)})

    fallback_path = normalize_db_path("data/tank.db")
    return fallback_path, "forced-project-default", rejected


def validate_runtime_db_configuration():
    if not IS_RENDER or not REQUIRE_RENDER_PERSISTENT_DB:
        return
    if render_persistent_db_active():
        return

    rejected_text = ", ".join(
        f"{entry['path']} ({entry['source']})" for entry in DB_PATH_REJECTED
    ) or "none recorded"
    raise RuntimeError(
        "Render persistent storage is required, but the application resolved "
        f"DB_FILE={DB_FILE} ({DB_PATH_SOURCE}) instead of {RENDER_PERSISTENT_DB_PATH}. "
        "Attach a mounted persistent disk at /var/data and deploy on a Render instance type "
        "that supports disks before restarting this service. "
        f"Rejected database paths: {rejected_text}."
    )


def resolve_app_secret_key(db_path):
    configured_secret = os.environ.get("APP_SECRET_KEY", "").strip()
    if configured_secret:
        return configured_secret, "env"

    try:
        if db_path.exists():
            with sqlite3.connect(str(db_path)) as db:
                row = db.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    (APP_SECRET_KEY_SETTING,),
                ).fetchone()
            persisted_secret = str(row[0] or "").strip() if row else ""
            if persisted_secret:
                return persisted_secret, f"{db_path}:app_settings"
    except (sqlite3.DatabaseError, OSError, IndexError, TypeError):
        pass

    secret_file = db_path.parent / ".app_secret_key"
    try:
        if secret_file.exists():
            persisted_secret = secret_file.read_text(encoding="utf-8").strip()
            if persisted_secret:
                try:
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    with sqlite3.connect(str(db_path)) as db:
                        db.execute(
                            """
                            CREATE TABLE IF NOT EXISTS app_settings(
                                key TEXT PRIMARY KEY,
                                value TEXT,
                                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                            )
                            """
                        )
                        db.execute(
                            """
                            INSERT INTO app_settings(key, value, updated_at)
                            VALUES (?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(key) DO UPDATE SET
                                value=excluded.value,
                                updated_at=CURRENT_TIMESTAMP
                            """,
                            (APP_SECRET_KEY_SETTING, persisted_secret),
                        )
                except (sqlite3.DatabaseError, OSError):
                    pass
                return persisted_secret, str(secret_file)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        generated_secret = secrets.token_hex(32)
        secret_file.write_text(generated_secret, encoding="utf-8")
        try:
            with sqlite3.connect(str(db_path)) as db:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS app_settings(
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO app_settings(key, value, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET
                        value=excluded.value,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (APP_SECRET_KEY_SETTING, generated_secret),
                )
        except (sqlite3.DatabaseError, OSError):
            pass
        return generated_secret, str(secret_file)
    except OSError:
        return DEFAULT_APP_SECRET_KEY, "default"


def resolve_device_key_registry():
    shared_registry = os.environ.get("SWT_DEVICE_KEYS", "").strip()
    if shared_registry:
        return shared_registry, "SWT_DEVICE_KEYS"

    configured_registry = os.environ.get("DEVICE_KEYS", "").strip()
    if configured_registry:
        return configured_registry, "DEVICE_KEYS"

    shared_device_id = os.environ.get("SWT_DEVICE_ID", "").strip()
    shared_device_key = os.environ.get("SWT_DEVICE_API_KEY", "").strip()
    if shared_device_id and shared_device_key:
        return f"{shared_device_id}:{shared_device_key}", "SWT_DEVICE_ID/SWT_DEVICE_API_KEY"

    return DEFAULT_DEVICE_KEYS, "default"


DB_PATH, DB_PATH_SOURCE, DB_PATH_REJECTED = resolve_db_path()
DB_FILE = str(DB_PATH)
APP_SECRET_KEY, APP_SECRET_KEY_SOURCE = resolve_app_secret_key(DB_PATH)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
CORS(app, resources={
    r"/status": {"origins": "*"},
    r"/device/command": {"origins": "*"},
    r"/health": {"origins": "*"},
})
app.config["JSON_SORT_KEYS"] = False
app.secret_key = APP_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE",
    "true" if IS_RENDER else "false"
).lower() not in {"0", "false", "no"}
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=env_int("SESSION_LIFETIME_HOURS", 12))
app.config["SESSION_COOKIE_NAME"] = os.environ.get("SESSION_COOKIE_NAME", "smart_water_tank_session")


@app.route("/manifest.webmanifest")
def web_manifest():
    response = send_from_directory(str(STATIC_DIR), "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(str(STATIC_DIR), "service-worker.js")
    response.headers["Content-Type"] = "application/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response
@app.after_request
def apply_security_headers(response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.method == "GET" and not request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def detect_git_short_commit():
    env_commit = (
        os.environ.get("RENDER_GIT_COMMIT")
        or os.environ.get("GIT_COMMIT")
        or os.environ.get("COMMIT_SHA")
    )
    if env_commit:
        return env_commit[:8]

    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={PROJECT_ROOT}", "rev-parse", "--short=8", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        commit = result.stdout.strip()
        return commit or None
    except Exception:
        return None


def detect_git_branch():
    env_branch = (
        os.environ.get("RENDER_GIT_BRANCH")
        or os.environ.get("GIT_BRANCH")
        or os.environ.get("BRANCH_NAME")
    )
    if env_branch:
        return env_branch

    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={PROJECT_ROOT}", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        branch = result.stdout.strip()
        return branch or None
    except Exception:
        return None


def detect_version_sequence():
    for value in (
        os.environ.get("SWT_BUILD_NUMBER"),
        os.environ.get("RENDER_DEPLOY_ID"),
        os.environ.get("CI_PIPELINE_IID"),
        os.environ.get("CI_PIPELINE_ID"),
        os.environ.get("BUILD_NUMBER"),
    ):
        if not value:
            continue
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if digits:
            return str(int(digits))

    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={PROJECT_ROOT}", "rev-list", "--count", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        count = result.stdout.strip()
        if count.isdigit():
            return count
    except Exception:
        pass

    # Day-of-year keeps the fallback number ascending through the calendar year.
    return datetime.now(timezone.utc).strftime("%j")


def detect_version_revision(seed_text):
    for value in (
        os.environ.get("SWT_SEQUENCE_NUMBER"),
        os.environ.get("SWT_VERSION_SEQUENCE"),
        os.environ.get("CI_JOB_ID"),
        os.environ.get("CI_JOB_IID"),
        os.environ.get("RENDER_INSTANCE_ID"),
    ):
        if not value:
            continue
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        if digits:
            return str(int(digits))

    digest = hashlib.sha256(seed_text.encode("utf-8")).digest()
    derived = ((digest[0] << 8) | digest[1]) % 1000
    return str(derived + 1)


def compact_version_number(value, max_digits, fallback="1"):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not digits:
        return fallback
    if len(digits) > max_digits:
        return digits[-max_digits:]
    return digits


def build_swt_version():
    explicit_version = os.environ.get("SWT_VERSION")
    if explicit_version:
        return explicit_version

    now = datetime.now(timezone.utc)
    year_suffix = now.strftime("%y")
    build_number = detect_version_sequence()
    git_commit = detect_git_short_commit()
    git_branch = detect_git_branch()
    suffix_seed = "|".join(
        part for part in (year_suffix, build_number, git_commit, git_branch) if part
    ) or now.strftime(DATE_ONLY_FORMAT)
    build_number = compact_version_number(build_number, 2)
    revision = compact_version_number(detect_version_revision(suffix_seed), 3)
    return f"v.{year_suffix}.{build_number}.{revision}"


SWT_VERSION = build_swt_version()
API_VERSION = SWT_VERSION
DEVICE = os.environ.get("DEVICE_URL", "").strip()
DEFAULT_CUSTOMER_PASSWORD = os.environ.get("DEFAULT_CUSTOMER_PASSWORD", "").strip()
SEED_DEFAULT_CUSTOMERS = os.environ.get("SEED_DEFAULT_CUSTOMERS", "false").lower() in {"1", "true", "yes"}
DEFAULT_CUSTOMER_ACCOUNTS = (
    ("swt-node-01", "Tank Owner"),
    ("swt-node-customer", "Customer Demo"),
    ("swt-node-other", "Tank Owner"),
)
LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", DEFAULT_ADMIN_PASSWORD).strip() or DEFAULT_ADMIN_PASSWORD
RESET_ADMIN_PASSWORD_ON_BOOT = os.environ.get("RESET_ADMIN_PASSWORD_ON_BOOT", "false").lower() in {"1", "true", "yes", "on"}
DEVICE_KEYS, DEVICE_KEYS_SOURCE = resolve_device_key_registry()
TANK_CAPACITY_LITERS = env_float("TANK_CAPACITY_LITERS", 1000.0)
STALE_AFTER_SECONDS = env_int("DATA_STALE_AFTER_SECONDS", 180)
DATA_RETENTION_DAYS = max(1, env_int("DATA_RETENTION_DAYS", 7))
TELEMETRY_HISTORY_ENABLED = env_flag("TELEMETRY_HISTORY_ENABLED", default=True)
MAX_TELEMETRY_ROWS_PER_DEVICE = max(0, env_int("MAX_TELEMETRY_ROWS_PER_DEVICE", 65000))
DEVICE_COMMAND_RETENTION_DAYS = max(1, env_int("DEVICE_COMMAND_RETENTION_DAYS", 7))
OPS_ALERT_RETENTION_DAYS = max(1, env_int("OPS_ALERT_RETENTION_DAYS", 30))
OPS_AUDIT_RETENTION_DAYS = max(1, env_int("OPS_AUDIT_RETENTION_DAYS", 30))
DB_MAINTENANCE_ENABLED = env_flag("DB_MAINTENANCE_ENABLED", default=True)
DB_TARGET_SIZE_MB = max(0.0, env_float("DB_TARGET_SIZE_MB", 256.0 if IS_RENDER else 0.0))
DB_TARGET_SIZE_BYTES = int(DB_TARGET_SIZE_MB * 1024 * 1024)
DB_MAINTENANCE_MIN_INTERVAL_SECONDS = max(60, env_int("DB_MAINTENANCE_MIN_INTERVAL_SECONDS", 900 if IS_RENDER else 3600))
DB_WAL_AUTOCHECKPOINT_PAGES = max(100, env_int("DB_WAL_AUTOCHECKPOINT_PAGES", 1000))
DB_PRUNE_MIN_INTERVAL_SECONDS = max(0, env_int("DB_PRUNE_MIN_INTERVAL_SECONDS", 30 if IS_RENDER else 15))
TEMP_DB_SIZE_GUARD_ENABLED = env_flag("TEMP_DB_SIZE_GUARD_ENABLED", default=False)
TEMP_HARD_DB_CAP_ENABLED = env_flag("TEMP_HARD_DB_CAP_ENABLED", default=False)
TEMP_HARD_DB_CAP_BATCH_ROWS = max(100, env_int("TEMP_HARD_DB_CAP_BATCH_ROWS", 2000))
TEMP_HARD_DB_CAP_MAX_BATCHES = max(1, env_int("TEMP_HARD_DB_CAP_MAX_BATCHES", 24))
REQUIRE_RENDER_PERSISTENT_DB = env_flag("REQUIRE_RENDER_PERSISTENT_DB", default=False)
SEED_VIRTUAL_DEVICE_ENVS = env_flag("SEED_VIRTUAL_DEVICE_ENVS", default=not IS_RENDER)
PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT = env_flag("PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT", default=IS_RENDER)
RESET_DEVICE_SOURCE_MODE_ON_BOOT = env_flag("RESET_DEVICE_SOURCE_MODE_ON_BOOT", default=IS_RENDER)
CONTROL_POLICY = "AUTO_PROTECTED"
DEFAULT_DEVICE_SOURCE_MODE = (
    DEVICE_SOURCE_VIRTUAL
    if str(os.environ.get("SWT_DEVICE_SOURCE_MODE", DEVICE_SOURCE_REAL)).strip().lower() == DEVICE_SOURCE_VIRTUAL
    else DEVICE_SOURCE_REAL
)
MOBILE_TOKEN_MAX_AGE_SECONDS = max(3600, env_int("MOBILE_TOKEN_MAX_AGE_HOURS", 168) * 3600)
MOBILE_TOKEN_SERIALIZER = URLSafeTimedSerializer(app.secret_key, salt=MOBILE_TOKEN_SALT)
analytics_cache = {}
dashboard_snapshot_cache = {}
level_forecast_model_cache = {}
SNAPSHOT_CACHE_TTL_SECONDS = max(0.0, env_float("SNAPSHOT_CACHE_TTL_SECONDS", 2.0))
ANALYTICS_MAX_GAP_MINUTES = env_int("ANALYTICS_MAX_GAP_MINUTES", 20)
ANALYTICS_MAX_LEVEL_DELTA_PCT = env_float("ANALYTICS_MAX_LEVEL_DELTA_PCT", 25.0)
ANALYTICS_MIN_BASELINE_USAGE_PCT = env_float("ANALYTICS_MIN_BASELINE_USAGE_PCT", 1.0)
ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR = env_float("ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR", 0.05)
ANALYTICS_CACHE_TTL_SECONDS = max(0.0, env_float("ANALYTICS_CACHE_TTL_SECONDS", 30.0))
ANALYTICS_CACHE_MAX_ENTRIES = max(1, env_int("ANALYTICS_CACHE_MAX_ENTRIES", 8 if IS_RENDER else 24))
ANALYTICS_LEVEL_SERIES_MAX_POINTS = max(60, env_int("ANALYTICS_LEVEL_SERIES_MAX_POINTS", 720 if IS_RENDER else 1440))
ANALYTICS_MOTOR_SERIES_MAX_POINTS = max(40, env_int("ANALYTICS_MOTOR_SERIES_MAX_POINTS", 240 if IS_RENDER else 480))
LEVEL_FORECAST_MODEL_PATH_ENV = "LEVEL_FORECAST_MODEL_PATH"
DEFAULT_LEVEL_FORECAST_MODEL_PATH = PROJECT_ROOT / "artifacts" / "level_forecast_model.pkl"
DEFAULT_SHARED_CLOUD_BASE_URL = normalize_http_base_url(os.environ.get("SWT_CLOUD_BASE_URL")) or "https://salewell.co.in"
DEFAULT_RELAY_STATUS_URLS = "" if IS_RENDER else f"{DEFAULT_SHARED_CLOUD_BASE_URL}/status"
DEFAULT_RELAY_COMMAND_URLS = "" if IS_RENDER else f"{DEFAULT_SHARED_CLOUD_BASE_URL}/device/command"
RELAY_STATUS_URLS = os.environ.get("RELAY_STATUS_URLS", DEFAULT_RELAY_STATUS_URLS)
RELAY_COMMAND_URLS = os.environ.get("RELAY_COMMAND_URLS", DEFAULT_RELAY_COMMAND_URLS)
RELAY_TIMEOUT_SEC = env_float("RELAY_TIMEOUT_SEC", 25.0)
RELAY_CONNECT_TIMEOUT_SEC = env_float("RELAY_CONNECT_TIMEOUT_SEC", 5.0)
RELAY_VERIFY_TLS = os.environ.get("RELAY_VERIFY_TLS", "true").lower() not in {"0", "false", "no"}
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WHATSAPP_WEBHOOK_URL = os.environ.get("WHATSAPP_WEBHOOK_URL", "").strip()
MQTT_ENABLED = os.environ.get("MQTT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
MQTT_BROKER_HOST = os.environ.get("MQTT_BROKER_HOST", "").strip()
MQTT_BROKER_PORT = env_int("MQTT_BROKER_PORT", 1883)
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "").strip()
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "swt").strip().strip("/")
MQTT_KEEPALIVE_SEC = max(15, env_int("MQTT_KEEPALIVE_SEC", 30))
MQTT_QOS = max(0, min(2, env_int("MQTT_QOS", 1)))
MQTT_COMMAND_RETAIN = os.environ.get("MQTT_COMMAND_RETAIN", "true").lower() in {"1", "true", "yes", "on"}
REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS = max(0, env_int("REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS", 30))
ALERT_TOUCH_INTERVAL_SECONDS = max(0, env_int("ALERT_TOUCH_INTERVAL_SECONDS", 30))


APP_LOG_LEVEL_NAME = (os.environ.get("APP_LOG_LEVEL") or os.environ.get("LOG_LEVEL") or ("warning" if IS_RENDER else "info")).strip().upper()
APP_LOG_LEVEL = getattr(logging, APP_LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(level=APP_LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")

logger = logging.getLogger("tank_server")
relay_lock = threading.Lock()
level_forecast_model_lock = threading.Lock()
db_maintenance_lock = threading.Lock()
db_prune_lock = threading.Lock()
relay_state = {
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_status_code": None,
}
mqtt_lock = threading.Lock()
mqtt_client = None
mqtt_started = False
registered_device_touch_lock = threading.Lock()
registered_device_touch_cache = {}
alert_touch_lock = threading.Lock()
alert_touch_cache = {}
mqtt_state = {
    "enabled": MQTT_ENABLED and bool(MQTT_BROKER_HOST),
    "connected": False,
    "last_connect_at": None,
    "last_message_at": None,
    "last_error": None,
}
db_maintenance_state = {
    "last_run_at": 0.0,
    "last_reason": None,
    "last_error": None,
    "last_total_bytes": 0,
    "last_skip_at": 0.0,
    "last_skip_reason": None,
}
db_prune_state = {
    "last_run_at": 0.0,
    "last_pruned_rows": 0,
    "last_error": None,
    "last_size_cap_rows": 0,
    "last_size_cap_batches": 0,
    "last_size_cap_remaining_pressure": False,
}


def parse_url_list(value):
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def mqtt_feature_enabled():
    return mqtt_state["enabled"] and mqtt is not None


def mqtt_topic(*parts):
    base_parts = [part for part in (MQTT_TOPIC_PREFIX,) if part]
    extra_parts = [str(part).strip("/") for part in parts if str(part or "").strip("/")]
    return "/".join(base_parts + extra_parts)


def mqtt_command_topic(device_id):
    return mqtt_topic(device_id, "command")


def mqtt_telemetry_topic(device_id):
    return mqtt_topic(device_id, "telemetry")


def mqtt_ack_topic(device_id):
    return mqtt_topic(device_id, "command_ack")


def mqtt_extract_device_id(topic, leaf_name):
    topic_text = str(topic or "").strip().strip("/")
    if not topic_text:
        return None
    expected_prefix = f"{MQTT_TOPIC_PREFIX}/" if MQTT_TOPIC_PREFIX else ""
    if expected_prefix and not topic_text.startswith(expected_prefix):
        return None
    suffix = f"/{leaf_name}"
    if not topic_text.endswith(suffix):
        return None
    middle = topic_text[len(expected_prefix): -len(suffix)]
    return normalize_device_id(middle)


RELAY_STATUS_URL_LIST = parse_url_list(RELAY_STATUS_URLS)
RELAY_COMMAND_URL_LIST = parse_url_list(RELAY_COMMAND_URLS)


def parse_device_key_registry(value):
    registry = {}
    for item in parse_url_list(value):
        if ":" not in item:
            continue
        device_id, key = item.split(":", 1)
        device_id = device_id.strip()
        key = key.strip()
        if not device_id or not key or "*" in device_id:
            continue
        registry[device_id] = key
    return registry


def parse_device_key_wildcard_rules(value):
    rules = []
    for item in parse_url_list(value):
        if ":" not in item:
            continue
        device_id, key = item.split(":", 1)
        device_id = device_id.strip()
        key = key.strip()
        if not device_id or not key or "*" not in device_id:
            continue
        if device_id.count("*") != 1 or not device_id.endswith("*"):
            continue
        prefix = device_id[:-1].strip()
        if not prefix:
            continue
        rules.append(
            {
                "pattern": device_id,
                "prefix": prefix,
                "key": key,
            }
        )
    return rules


def configured_device_auth_enabled():
    if DEVICE_KEY_MAP or DEVICE_KEY_WILDCARD_RULES or LOCAL_VIRTUAL_DEVICE_AUTH_MAP:
        return True
    if SEED_VIRTUAL_DEVICE_ENVS and refresh_configured_virtual_device_auth():
        return True
    return False


def find_matching_device_key_rule(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    expected = DEVICE_KEY_MAP.get(normalized_device_id)
    if expected:
        return {
            "kind": "exact",
            "pattern": normalized_device_id,
            "prefix": normalized_device_id,
            "key": expected,
        }

    for rule in DEVICE_KEY_WILDCARD_RULES:
        if normalized_device_id.startswith(rule["prefix"]):
            return {
                "kind": "wildcard",
                "pattern": rule["pattern"],
                "prefix": rule["prefix"],
                "key": rule["key"],
            }

    expected = LOCAL_VIRTUAL_DEVICE_AUTH_MAP.get(normalized_device_id)
    if not expected and SEED_VIRTUAL_DEVICE_ENVS:
        refresh_configured_virtual_device_auth()
        expected = LOCAL_VIRTUAL_DEVICE_AUTH_MAP.get(normalized_device_id)
    if expected:
        return {
            "kind": "virtual_env",
            "pattern": LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES.get(normalized_device_id, normalized_device_id),
            "prefix": normalized_device_id,
            "key": expected,
        }
    return None


def configured_device_key_for_id(device_id):
    matched_rule = find_matching_device_key_rule(device_id)
    return matched_rule.get("key") if matched_rule else None


def list_registered_device_ids(limit=200):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT device_id
            FROM registered_devices
            ORDER BY last_seen_at DESC, device_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [normalize_device_id(row["device_id"]) for row in rows if normalize_device_id(row["device_id"])]


def list_ignored_device_ids(limit=None):
    query = """
        SELECT device_id
        FROM ignored_devices
        ORDER BY updated_at DESC, device_id ASC
    """
    params = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return {
        normalize_device_id(row["device_id"])
        for row in rows
        if normalize_device_id(row["device_id"])
    }


def device_is_ignored(device_id, ignored_device_ids=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return False
    if ignored_device_ids is None:
        ignored_device_ids = list_ignored_device_ids()
    return normalized_device_id in ignored_device_ids


def remember_ignored_device(device_id, note=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    with get_db() as db:
        db.execute(
            """
            INSERT INTO ignored_devices(device_id, note, created_at, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                note=excluded.note,
                updated_at=CURRENT_TIMESTAMP
            """,
            (normalized_device_id, str(note or "").strip() or None),
        )
    forget_registered_device_touch(normalized_device_id)
    forget_alert_touches_for_device(normalized_device_id)


def forget_ignored_device(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (normalized_device_id,))


def should_skip_registered_device_touch(device_id, registration_source, key_rule):
    if REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS <= 0:
        return False
    now = time.monotonic()
    with registered_device_touch_lock:
        cached = registered_device_touch_cache.get(device_id)
        if not cached:
            return False
        if cached.get("registration_source") != registration_source:
            return False
        if cached.get("key_rule") != key_rule:
            return False
        return (now - float(cached.get("touched_at") or 0.0)) < REGISTERED_DEVICE_TOUCH_INTERVAL_SECONDS


def note_registered_device_touch(device_id, registration_source, key_rule):
    with registered_device_touch_lock:
        registered_device_touch_cache[device_id] = {
            "registration_source": registration_source,
            "key_rule": key_rule,
            "touched_at": time.monotonic(),
        }


def forget_registered_device_touch(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    with registered_device_touch_lock:
        registered_device_touch_cache.pop(normalized_device_id, None)


def database_is_locked_error(exc):
    message = str(exc or "").strip().lower()
    return "database is locked" in message or "database table is locked" in message or "database is busy" in message


def alert_touch_key(kind, device_id=None):
    normalized_kind = str(kind or "").strip().lower()
    normalized_device_id = normalize_device_id(device_id) or ""
    return normalized_kind, normalized_device_id


def should_skip_alert_touch(kind, severity, message, device_id=None, active=True):
    if ALERT_TOUCH_INTERVAL_SECONDS <= 0:
        return False
    cache_key = alert_touch_key(kind, device_id)
    now = time.monotonic()
    with alert_touch_lock:
        cached = alert_touch_cache.get(cache_key)
        if not cached:
            return False
        if cached.get("severity") != severity:
            return False
        if cached.get("message") != message:
            return False
        if bool(cached.get("active")) != bool(active):
            return False
        return (now - float(cached.get("touched_at") or 0.0)) < ALERT_TOUCH_INTERVAL_SECONDS


def note_alert_touch(kind, severity, message, device_id=None, active=True):
    cache_key = alert_touch_key(kind, device_id)
    with alert_touch_lock:
        alert_touch_cache[cache_key] = {
            "severity": severity,
            "message": message,
            "active": bool(active),
            "touched_at": time.monotonic(),
        }


def forget_alert_touch(kind, device_id=None):
    cache_key = alert_touch_key(kind, device_id)
    with alert_touch_lock:
        alert_touch_cache.pop(cache_key, None)


def forget_alert_touches_for_device(device_id=None):
    if device_id is None:
        with alert_touch_lock:
            alert_touch_cache.clear()
        return
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    with alert_touch_lock:
        doomed_keys = [key for key in alert_touch_cache if key[1] == normalized_device_id]
        for key in doomed_keys:
            alert_touch_cache.pop(key, None)


def remember_registered_device(device_id, registration_source, key_rule=None, best_effort=False):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id or device_is_ignored(normalized_device_id):
        return
    if should_skip_registered_device_touch(normalized_device_id, registration_source, key_rule):
        return
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO registered_devices(device_id, registration_source, key_rule, first_seen_at, last_seen_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(device_id) DO UPDATE SET
                    registration_source=excluded.registration_source,
                    key_rule=excluded.key_rule,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (normalized_device_id, registration_source, key_rule),
            )
    except sqlite3.OperationalError as exc:
        if best_effort and database_is_locked_error(exc):
            note_registered_device_touch(normalized_device_id, registration_source, key_rule)
            logger.warning(
                "Skipping registered device bookkeeping for %s because the SQLite database is busy.",
                normalized_device_id,
            )
            return
        raise
    note_registered_device_touch(normalized_device_id, registration_source, key_rule)


def iter_virtual_device_env_paths():
    tests_root = PROJECT_ROOT / "tests"
    seen = set()
    candidates = [
        tests_root / "virtual_device.env",
    ]
    virtual_devices_dir = tests_root / "virtual_devices"
    if virtual_devices_dir.exists():
        candidates.extend(sorted(path for path in virtual_devices_dir.rglob("*.env") if path.is_file()))

    for path in candidates:
        resolved = str(path.resolve()) if path.exists() else str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        yield path


def relative_to_project_or_str(path):
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def configured_virtual_device_ids():
    seen_device_ids = set()
    for env_path in iter_virtual_device_env_paths():
        env_values = parse_simple_dotenv(env_path)
        normalized_device_id = normalize_device_id(env_values.get("SWT_VIRTUAL_DEVICE_ID"))
        if not normalized_device_id or normalized_device_id in seen_device_ids:
            continue
        seen_device_ids.add(normalized_device_id)
        yield normalized_device_id


def configured_virtual_device_auth_entries():
    if not SEED_VIRTUAL_DEVICE_ENVS:
        return {}

    auth_entries = {}
    for env_path in iter_virtual_device_env_paths():
        env_values = parse_simple_dotenv(env_path)
        normalized_device_id = str(env_values.get("SWT_VIRTUAL_DEVICE_ID") or "").strip()
        device_key = str(
            env_values.get("SWT_VIRTUAL_DEVICE_KEY")
            or env_values.get("SWT_DEVICE_API_KEY")
            or ""
        ).strip()
        if not normalized_device_id or not device_key or normalized_device_id in auth_entries:
            continue
        auth_entries[normalized_device_id] = {
            "key": device_key,
            "key_rule": relative_to_project_or_str(env_path) if env_path.exists() else str(env_path),
        }
    return auth_entries


def seed_registered_devices_from_configuration():
    seeded_device_ids = set()
    ignored_device_ids = list_ignored_device_ids()

    for device_id in sorted(DEVICE_KEY_MAP.keys()):
        normalized_device_id = normalize_device_id(device_id)
        if (
            not normalized_device_id
            or normalized_device_id in seeded_device_ids
            or normalized_device_id in ignored_device_ids
        ):
            continue
        remember_registered_device(
            normalized_device_id,
            registration_source="configured_device_keys",
            key_rule=normalized_device_id,
        )
        seeded_device_ids.add(normalized_device_id)

    if SEED_VIRTUAL_DEVICE_ENVS:
        for env_path in iter_virtual_device_env_paths():
            env_values = parse_simple_dotenv(env_path)
            normalized_device_id = normalize_device_id(env_values.get("SWT_VIRTUAL_DEVICE_ID"))
            if (
                not normalized_device_id
                or normalized_device_id in seeded_device_ids
                or normalized_device_id in ignored_device_ids
            ):
                continue
            remember_registered_device(
                normalized_device_id,
                registration_source="virtual_device_env",
                key_rule=relative_to_project_or_str(env_path) if env_path.exists() else str(env_path),
            )
            seeded_device_ids.add(normalized_device_id)


def purge_configured_virtual_device_records():
    if not PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT:
        return {"device_ids": 0}

    virtual_device_ids = list(configured_virtual_device_ids())
    if not virtual_device_ids:
        return {"device_ids": 0}

    deleted_counts = {
        "device_ids": len(virtual_device_ids),
        "customer_accounts": 0,
        "registered_devices": 0,
        "tank_data": 0,
        "device_command_queue": 0,
        "ops_alerts": 0,
        "ops_audit_log": 0,
        "ignored_devices": 0,
    }

    with get_db() as db:
        for normalized_device_id in virtual_device_ids:
            deleted_counts["customer_accounts"] += int(
                db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["registered_devices"] += int(
                db.execute("DELETE FROM registered_devices WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["tank_data"] += int(
                db.execute("DELETE FROM tank_data WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["device_command_queue"] += int(
                db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["ops_alerts"] += int(
                db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["ops_audit_log"] += int(
                db.execute("DELETE FROM ops_audit_log WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )
            deleted_counts["ignored_devices"] += int(
                db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
            )

    for normalized_device_id in virtual_device_ids:
        forget_registered_device_touch(normalized_device_id)
        clear_runtime_caches(normalized_device_id)

    return deleted_counts


def delete_known_device(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    deleted_counts = {
        "customer_accounts": 0,
        "registered_devices": 0,
        "tank_data": 0,
        "device_command_queue": 0,
        "ops_alerts": 0,
        "ops_audit_log": 0,
        "ignored_devices": 0,
    }

    with get_db() as db:
        deleted_counts["customer_accounts"] = int(
            db.execute("DELETE FROM customer_accounts WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["registered_devices"] = int(
            db.execute("DELETE FROM registered_devices WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["tank_data"] = int(
            db.execute("DELETE FROM tank_data WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["device_command_queue"] = int(
            db.execute("DELETE FROM device_command_queue WHERE target_device = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["ops_alerts"] = int(
            db.execute("DELETE FROM ops_alerts WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["ops_audit_log"] = int(
            db.execute("DELETE FROM ops_audit_log WHERE device_id = ?", (normalized_device_id,)).rowcount or 0
        )
        deleted_counts["ignored_devices"] = int(
            db.execute(
                """
                INSERT INTO ignored_devices(device_id, note, created_at, updated_at)
                VALUES (?, 'admin_delete', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(device_id) DO UPDATE SET
                    note=excluded.note,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (normalized_device_id,),
            ).rowcount or 0
        )

    forget_registered_device_touch(normalized_device_id)
    clear_runtime_caches(normalized_device_id)

    return deleted_counts


DEVICE_KEY_MAP = parse_device_key_registry(DEVICE_KEYS)
DEVICE_KEY_WILDCARD_RULES = parse_device_key_wildcard_rules(DEVICE_KEYS)
LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES = configured_virtual_device_auth_entries()
LOCAL_VIRTUAL_DEVICE_AUTH_MAP = {
    device_id: entry["key"]
    for device_id, entry in LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES.items()
}
LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES = {
    device_id: entry["key_rule"]
    for device_id, entry in LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES.items()
}


def refresh_configured_virtual_device_auth():
    global LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES, LOCAL_VIRTUAL_DEVICE_AUTH_MAP, LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES
    LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES = configured_virtual_device_auth_entries()
    LOCAL_VIRTUAL_DEVICE_AUTH_MAP = {
        device_id: entry["key"]
        for device_id, entry in LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES.items()
    }
    LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES = {
        device_id: entry["key_rule"]
        for device_id, entry in LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES.items()
    }
    return len(LOCAL_VIRTUAL_DEVICE_AUTH_MAP)


def device_keys_look_default():
    configured_keys = list(DEVICE_KEY_MAP.values()) + [rule["key"] for rule in DEVICE_KEY_WILDCARD_RULES]
    if not configured_keys:
        return True
    return any("change-me" in str(value).lower() for value in configured_keys)


def normalize_device_id(value):
    return str(value or "").strip()


def normalize_device_source(value, default=DEVICE_SOURCE_REAL):
    raw = str(value or "").strip().lower()
    if raw in {DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL}:
        return raw
    return default


def clear_runtime_caches(device_id=None):
    analytics_cache.clear()
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        dashboard_snapshot_cache.clear()
        forget_alert_touches_for_device()
        return
    for mode in (DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL):
        dashboard_snapshot_cache.pop(f"{mode}:{normalized_device_id}", None)
    dashboard_snapshot_cache.pop(f"{DEVICE_SOURCE_REAL}:__latest__", None)
    dashboard_snapshot_cache.pop(f"{DEVICE_SOURCE_VIRTUAL}:__latest__", None)
    forget_alert_touches_for_device(normalized_device_id)


def current_user_role():
    if not is_logged_in():
        return None
    return session.get("role") or "admin"


def is_admin_user():
    return current_user_role() == "admin"


def current_customer_device_id():
    if current_user_role() != "customer":
        return None
    return normalize_device_id(session.get("device_id"))


def current_customer_account():
    device_id = current_customer_device_id()
    if not device_id:
        return None
    return fetch_customer_account(device_id)


def current_customer_service_config():
    if current_user_role() != "customer":
        return None
    device_id = current_customer_device_id()
    if not device_id:
        return None
    return fetch_device_service_config(device_id, account=current_customer_account())


def current_customer_cloud_feed_enabled():
    if current_user_role() != "customer":
        return True
    service_config = current_customer_service_config()
    if not service_config:
        return False
    return bool(service_config.get("cloud_feed_enabled", False))


def current_customer_ai_analysis_enabled():
    if current_user_role() != "customer":
        return True
    service_config = current_customer_service_config()
    if not service_config:
        return False
    return bool(service_config.get("effective_ai_analysis_enabled", False))


def customer_cloud_feed_error_message():
    return "Cloud feed is disabled for this customer account. Contact the admin to enable it."


def customer_ai_analysis_error_message():
    return "AI analysis is disabled for this device. Ask the admin to switch Cloud Feed to Full Features."


def customer_cloud_feed_block_response():
    if current_user_role() != "customer" or current_customer_cloud_feed_enabled():
        return None
    return jsonify(
        {
            "error": customer_cloud_feed_error_message(),
            "cloud_feed_enabled": False,
        }
    ), 403


def customer_ai_analysis_block_response():
    if current_user_role() != "customer" or current_customer_ai_analysis_enabled():
        return None
    return jsonify(
        {
            "error": customer_ai_analysis_error_message(),
            "ai_analysis_enabled": False,
        }
    ), 403


def customer_cloud_feed_abort_if_disabled():
    if current_user_role() == "customer" and not current_customer_cloud_feed_enabled():
        abort(403, description=customer_cloud_feed_error_message())


def current_actor_username():
    return session.get("username", "unknown")


def current_scope_device_id(requested_device_id=None):
    normalized_requested = normalize_device_id(requested_device_id)
    if is_admin_user():
        return normalized_requested or None
    scoped_device_id = current_customer_device_id()
    if normalized_requested and scoped_device_id and normalized_requested != scoped_device_id:
        abort(403)
    return scoped_device_id or normalized_requested or None


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def validate_csrf_or_abort():
    session_token = session.get("csrf_token")
    request_token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    if not session_token or not request_token or not secrets.compare_digest(session_token, request_token):
        abort(400, description="CSRF token missing or invalid")


def csrf_protect(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            validate_csrf_or_abort()
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def inject_template_globals():
    return {"csrf_token": get_csrf_token()}

LOGIN_TEMPLATE_FALLBACK = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Smart Water Tank Login</title>
<style>
:root{--bg:#08111f;--panel:#0f1c2f;--line:rgba(148,163,184,.18);--text:#e2e8f0;--muted:#94a3b8;--primary:#38bdf8;--danger:#ef4444}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at top left,rgba(56,189,248,.16),transparent 24%),linear-gradient(180deg,#091120 0%,#07101d 100%)}
.card{width:min(420px,calc(100% - 24px));padding:28px;border-radius:20px;background:rgba(15,28,47,.92);border:1px solid var(--line);box-shadow:0 18px 40px rgba(2,8,23,.35)}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--primary);margin:0 0 10px}.title{font-size:34px;font-weight:700;margin:0 0 8px}.muted{margin:0 0 18px;color:var(--muted);line-height:1.5}
form{display:grid;gap:14px}.group{display:grid;gap:6px}.group label{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid rgba(148,163,184,.22);background:rgba(8,17,31,.78);color:var(--text)}
button{border:none;border-radius:12px;padding:10px 16px;min-height:40px;font-weight:700;cursor:pointer;background:linear-gradient(135deg,#60a5fa,#2563eb);color:#fff;justify-self:start}
a{display:inline-flex;align-items:center;justify-content:center;border-radius:12px;padding:10px 16px;min-height:40px;font-weight:700;text-decoration:none;border:1px solid rgba(148,163,184,.24);color:var(--text)}
.error{padding:12px 14px;border-radius:12px;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.24);color:#fecaca}
.hint{margin-top:14px;font-size:13px;color:var(--muted)}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
@media (max-width:560px){button,a{width:100%;justify-content:center}.row{display:grid}.row a{width:100%}}
</style>
</head>
<body>
<div class="card">
    <div class="eyebrow">Smart Water Tank</div>
    <h1 class="title">{{ login_title }}</h1>
    <p class="muted">{{ login_description }}</p>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <form method="post" action="{{ login_action }}">
        <input type="hidden" name="next" value="{{ next_url }}">
        <div class="group">
            <label for="username">Username</label>
            <input id="username" name="username" type="text" autocomplete="username" required>
        </div>
        <div class="group">
            <label for="password">Password</label>
            <input id="password" name="password" type="password" autocomplete="current-password" required>
        </div>
        <button type="submit">Sign In</button>
    </form>
    <div class="row">
        <a href="{{ switch_href }}">{{ switch_label }}</a>
    </div>
    <div class="hint">{% if login_mode == "admin" %}Use the admin dashboard account here. Customers should sign in from the customer page with their exact device ID.{% else %}Customer usernames must exactly match the registered device_id, for example swt-000-000-000-001.{% endif %}</div>
</div>
</body>
</html>"""


def is_logged_in():
    return bool(session.get("logged_in"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_logged_in():
            return view(*args, **kwargs)
        return redirect(url_for("customer_login", next=request.path))

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("admin_login", next=request.path))
        if not is_admin_user():
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("customer_login", next=request.path))
        if current_user_role() != "customer":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def is_safe_next_url(target):
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")


def dashboard_home_endpoint():
    return "admin_customers" if is_admin_user() else "customer_dashboard"


def dashboard_home_url(role=None):
    resolved_role = role or current_user_role() or "customer"
    return url_for("admin_customers" if resolved_role == "admin" else "customer_dashboard")


def auth_persistence_warnings():
    warnings = []
    render_persistent_db = render_persistent_db_active()
    if app.config.get("SESSION_COOKIE_SECURE") and not request.is_secure:
        warnings.append(
            "SESSION_COOKIE_SECURE is enabled but this page is being served over HTTP. "
            "Browsers will refuse to keep the login cookie, so users can appear to be logged out immediately. "
            "Use HTTPS or set SESSION_COOKIE_SECURE=false for local/LAN HTTP deployments."
        )
    if IS_RENDER and not render_persistent_db:
        warnings.append(
            f"This Render deployment is using DB_FILE={DB_FILE} instead of /var/data/tank.db on a mounted disk. "
            "Customer passwords and dashboard data can disappear after redeploys or restarts."
        )
    if APP_SECRET_KEY_SOURCE == "default":
        warnings.append(
            "APP_SECRET_KEY is using the default fallback value. Browser and mobile sessions can be invalidated after restarts."
        )
    elif IS_RENDER and not render_persistent_db and APP_SECRET_KEY_SOURCE != "env":
        warnings.append(
            "APP_SECRET_KEY is not explicitly set in Render environment variables. "
            "If the app falls back to ephemeral storage, everyone can be logged out after redeploys."
        )
    return warnings


def resolve_next_url(default_url):
    next_url = request.values.get("next") or default_url
    if not is_safe_next_url(next_url):
        return default_url
    return next_url


def can_access_next_url(role, next_url):
    if not is_safe_next_url(next_url):
        return False
    if role == "admin":
        return not next_url.startswith("/customer/dashboard")
    return not next_url.startswith("/admin/")


def render_login_page(mode="customer", error=None, next_url="/"):
    is_admin_mode = mode == "admin"
    login_action = url_for("admin_login" if is_admin_mode else "customer_login")
    switch_href = url_for("customer_login" if is_admin_mode else "admin_login")
    switch_label = "Customer Login" if is_admin_mode else "Admin Login"
    persistence_warnings = auth_persistence_warnings()
    try:
        return render_template(
            "login.html",
            error=error,
            next_url=next_url,
            login_mode=mode,
            login_title="Admin Login" if is_admin_mode else "Customer Login",
            login_description=(
                "Admin signs in here to register customers, manage credentials, and control devices."
                if is_admin_mode else
                "Customers sign in here with their tank device ID and password to view only their own tank dashboard."
            ),
            login_action=login_action,
            switch_href=switch_href,
            switch_label=switch_label,
            persistence_warnings=persistence_warnings,
        )
    except TemplateNotFound:
        logger.warning("login.html template not found, using inline fallback")
        return render_template_string(
            LOGIN_TEMPLATE_FALLBACK,
            error=error,
            next_url=next_url,
            login_mode=mode,
            login_title="Admin Login" if is_admin_mode else "Customer Login",
            login_description=(
                "Admin signs in here to register customers, manage credentials, and control devices."
                if is_admin_mode else
                "Customers sign in here with their tank device ID and password to view only their own tank dashboard."
            ),
            login_action=login_action,
            switch_href=switch_href,
            switch_label=switch_label,
            persistence_warnings=persistence_warnings,
        )
def handle_role_login(mode):
    if is_logged_in():
        return redirect(dashboard_home_url())

    default_url = dashboard_home_url("admin" if mode == "admin" else "customer")
    next_url = resolve_next_url(default_url)
    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        authenticated_user = authenticate_dashboard_user(username, password)
        expected_role = "admin" if mode == "admin" else "customer"
        if authenticated_user and authenticated_user["role"] == expected_role:
            session["logged_in"] = True
            session["username"] = authenticated_user["username"]
            session["role"] = authenticated_user["role"]
            session["device_id"] = authenticated_user.get("device_id")
            session.permanent = True
            destination = next_url if can_access_next_url(authenticated_user["role"], next_url) else dashboard_home_url(authenticated_user["role"])
            logger.info(
                "Dashboard login successful for user %s with role %s",
                authenticated_user["username"],
                authenticated_user["role"],
            )
            return redirect(destination)
        if authenticated_user:
            wrong_page = "Admin Login" if expected_role == "customer" else "Customer Login"
            error = f"Use the {wrong_page} page for this account."
        else:
            error = "Invalid username or password."
        time.sleep(0.5)
        logger.warning("Dashboard login failed for user %s on %s mode", username, mode)

    return render_login_page(mode=mode, error=error, next_url=next_url)


def render_firmware_update_unavailable(device_id=None):
    return render_template_string(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Firmware Update Unavailable</title>
<style>
:root{--bg:#08111f;--panel:#0f1c2f;--line:rgba(148,163,184,.18);--text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at top left,rgba(56,189,248,.16),transparent 24%),linear-gradient(180deg,#091120 0%,#07101d 100%)}
.card{width:min(520px,calc(100% - 24px));padding:28px;border-radius:20px;background:rgba(15,28,47,.92);border:1px solid var(--line);box-shadow:0 18px 40px rgba(2,8,23,.35)}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);margin:0 0 10px}
h1{margin:0 0 10px}p{margin:0 0 12px;color:var(--muted);line-height:1.55}.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
a{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border-radius:12px;border:1px solid rgba(148,163,184,.24);color:var(--text);text-decoration:none}
</style>
</head>
<body>
<div class="card">
    <div class="eyebrow">Firmware Update</div>
    <h1>Device OTA page is not available yet</h1>
    <p>{% if device_id %}No recent network address is available for device <strong>{{ device_id }}</strong>.{% else %}No recent device network address is available yet.{% endif %}</p>
    <p>Let the ESP connect and send telemetry first, then try the OTA button again from the dashboard.</p>
    <div class="actions">
        <a href="/">Back to Dashboard</a>
        {% if device_id %}<a href="/devices/{{ device_id }}">Back to Device</a>{% endif %}
    </div>
</div>
</body>
</html>""",
        device_id=device_id,
    ), 404


def render_dashboard_password_page(error=None, success=None):
    return render_template_string(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reset Dashboard Password</title>
<style>
:root{--bg:#08111f;--panel:#0f1c2f;--line:rgba(148,163,184,.18);--text:#e2e8f0;--muted:#94a3b8;--primary:#38bdf8;--danger:#ef4444;--ok:#22c55e}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:"Segoe UI",sans-serif;color:var(--text);background:radial-gradient(circle at top left,rgba(56,189,248,.16),transparent 24%),linear-gradient(180deg,#091120 0%,#07101d 100%)}
.card{width:min(460px,calc(100% - 24px));padding:28px;border-radius:20px;background:rgba(15,28,47,.92);border:1px solid var(--line);box-shadow:0 18px 40px rgba(2,8,23,.35)}
.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--primary);margin:0 0 10px}.title{font-size:30px;font-weight:700;margin:0 0 8px}.muted{margin:0 0 18px;color:var(--muted);line-height:1.5}
form{display:grid;gap:14px}.group{display:grid;gap:6px}.group label{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
input{width:100%;padding:12px 14px;border-radius:12px;border:1px solid rgba(148,163,184,.22);background:rgba(8,17,31,.78);color:var(--text)}
button,a{display:inline-flex;align-items:center;justify-content:center;border-radius:12px;padding:12px 16px;font-weight:700;text-decoration:none}
button{border:none;cursor:pointer;background:linear-gradient(135deg,#60a5fa,#2563eb);color:#fff}
a{border:1px solid rgba(148,163,184,.24);color:var(--text)}
.row{display:flex;gap:10px;flex-wrap:wrap}.error{padding:12px 14px;border-radius:12px;background:rgba(239,68,68,.14);border:1px solid rgba(239,68,68,.24);color:#fecaca}.success{padding:12px 14px;border-radius:12px;background:rgba(34,197,94,.14);border:1px solid rgba(34,197,94,.24);color:#bbf7d0}
</style>
</head>
<body>
<div class="card">
    <div class="eyebrow">Smart Water Tank</div>
    <h1 class="title">Reset Dashboard Password</h1>
    <p class="muted">Update the Flask dashboard login password for user <strong>{{ username }}</strong>.</p>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    {% if success %}
    <div class="success">{{ success }}</div>
    {% endif %}
    <form method="post" action="/account/password">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
        <div class="group">
            <label for="current_password">Current Password</label>
            <input id="current_password" name="current_password" type="password" minlength="6" required>
        </div>
        <div class="group">
            <label for="new_password">New Password</label>
            <input id="new_password" name="new_password" type="password" minlength="6" required>
        </div>
        <div class="group">
            <label for="confirm_password">Confirm New Password</label>
            <input id="confirm_password" name="confirm_password" type="password" minlength="6" required>
        </div>
        <div class="row">
            <button type="submit">Save Password</button>
            <a href="/">Back to Dashboard</a>
        </div>
    </form>
</div>
</body>
</html>""",
        username=LOGIN_USERNAME,
        error=error,
        success=success,
    )


def fetch_active_alert_device_ids():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT DISTINCT device_id
            FROM ops_alerts
            WHERE active = 1
              AND COALESCE(device_id, '') != ''
            """
        ).fetchall()
    return {
        normalize_device_id(row["device_id"])
        for row in rows
        if normalize_device_id(row["device_id"])
    }


def admin_device_is_online(device):
    return str(device.get("telemetry_status") or "").strip().lower() in {"live", "recent"}


def admin_telemetry_status_label(value):
    normalized = str(value or "").strip().lower()
    if normalized == "live":
        return "Live"
    if normalized == "recent":
        return "Recent"
    if normalized == "stale":
        return "Stale"
    if normalized == "no-data":
        return "No data"
    return normalized.replace("-", " ").title() if normalized else "--"


def alert_severity_rank(value):
    normalized = str(value or "").strip().lower()
    if normalized == "danger":
        return 3
    if normalized == "warning":
        return 2
    if normalized == "success":
        return 1
    return 0


def fetch_active_alert_summaries(device_ids=None):
    normalized_device_ids = [
        item
        for item in (normalize_device_id(value) for value in (device_ids or []))
        if item
    ]
    query = """
        SELECT device_id, severity, message, updated_at, id
        FROM ops_alerts
        WHERE active = 1
          AND COALESCE(device_id, '') != ''
    """
    params = []
    if normalized_device_ids:
        placeholders = ",".join("?" for _ in normalized_device_ids)
        query += f" AND device_id IN ({placeholders})"
        params.extend(normalized_device_ids)
    query += " ORDER BY updated_at DESC, id DESC"
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    summaries = {}
    for row in rows:
        device_id = normalize_device_id(row["device_id"])
        if not device_id:
            continue
        entry = summaries.setdefault(
            device_id,
            {
                "active_alert_count": 0,
                "latest_alert_message": None,
                "latest_alert_severity": "info",
                "latest_alert_updated_at": None,
                "highest_alert_severity": "info",
            },
        )
        entry["active_alert_count"] += 1
        severity = str(row["severity"] or "info").strip().lower() or "info"
        if entry["latest_alert_message"] is None:
            entry["latest_alert_message"] = row["message"]
            entry["latest_alert_severity"] = severity
            entry["latest_alert_updated_at"] = row["updated_at"]
        if alert_severity_rank(severity) > alert_severity_rank(entry["highest_alert_severity"]):
            entry["highest_alert_severity"] = severity
    return summaries


def build_admin_device_entry(device_id, snapshot=None):
    normalized_device_id = normalize_device_id(device_id or (snapshot or {}).get("device_id"))
    payload = snapshot if snapshot is not None else build_empty_snapshot_payload(normalized_device_id)
    return {
        "device_id": normalized_device_id,
        "level": payload.get("level"),
        "firmware_version": payload.get("firmware_version"),
        "reset_reason": payload.get("reset_reason"),
        "last_sync_at": payload.get("last_sync_at"),
        "telemetry_status": payload.get("telemetry_status"),
        "channel_mode": payload.get("channel_mode"),
        "telemetry_service": payload.get("telemetry_service"),
        "command_service": payload.get("command_service"),
        "ota_service": payload.get("ota_service"),
        "lower_tank_service": payload.get("lower_tank_service"),
        "buzzer_service": payload.get("buzzer_service"),
        "led_display_service": payload.get("led_display_service"),
        "wifi": payload.get("wifi"),
        "wifi_rssi": payload.get("wifi_rssi"),
        "sensor": payload.get("sensor"),
        "motor": payload.get("motor"),
        "mode": payload.get("mode"),
        "registered_account": False,
        "account_active": False,
        "cloud_feed_enabled": False,
    }


def build_admin_device_summary(available_devices):
    alert_device_ids = fetch_active_alert_device_ids()
    seen_device_ids = set()
    online_devices = 0

    for device in available_devices:
        normalized_device_id = normalize_device_id(device.get("device_id"))
        if not normalized_device_id or normalized_device_id in seen_device_ids:
            continue
        seen_device_ids.add(normalized_device_id)
        if admin_device_is_online(device):
            online_devices += 1

    total_registered_devices = len(seen_device_ids)
    warning_alert_devices = len(seen_device_ids.intersection(alert_device_ids))
    offline_devices = max(0, total_registered_devices - online_devices)

    return {
        "total_registered_devices": total_registered_devices,
        "online_devices": online_devices,
        "offline_devices": offline_devices,
        "warning_alert_devices": warning_alert_devices,
    }


def render_customer_admin_page(accounts, available_devices, error=None, success=None, search_query="", device_summary=None):
    return render_template(
        "admin_customers.html",
        accounts=accounts,
        available_devices=available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary or build_admin_device_summary(available_devices),
        global_alerts=fetch_filtered_alerts(limit=10),
        persistence_warnings=auth_persistence_warnings(),
    )


def build_admin_known_devices(accounts, available_devices):
    seed_registered_devices_from_configuration()
    ignored_device_ids = list_ignored_device_ids()
    merged = {}
    accounts_by_device = {}

    for device in available_devices:
        normalized_device_id = normalize_device_id(device.get("device_id"))
        if not normalized_device_id or normalized_device_id in ignored_device_ids:
            continue
        merged[normalized_device_id] = build_admin_device_entry(normalized_device_id, snapshot=dict(device))

    for account in accounts:
        normalized_device_id = normalize_device_id(account.get("device_id"))
        if not normalized_device_id or normalized_device_id in ignored_device_ids:
            continue

        entry = merged.get(normalized_device_id)
        if entry is None:
            entry = build_admin_device_entry(normalized_device_id)
            merged[normalized_device_id] = entry

        entry["display_name"] = account.get("display_name") or entry.get("display_name")
        entry["registered_account"] = True
        entry["account_active"] = int(account.get("active", 1) or 0) == 1
        entry["cloud_feed_enabled"] = int(account.get("cloud_feed_enabled", 1) or 0) == 1
        accounts_by_device[normalized_device_id] = account

    for device_id in list_registered_device_ids(limit=200):
        normalized_device_id = normalize_device_id(device_id)
        if not normalized_device_id or normalized_device_id in ignored_device_ids:
            continue

        entry = merged.get(normalized_device_id)
        if entry is None:
            entry = build_admin_device_entry(normalized_device_id)
            merged[normalized_device_id] = entry

        entry["server_registered"] = True

    for device_id in sorted(DEVICE_KEY_MAP.keys()):
        normalized_device_id = normalize_device_id(device_id)
        if not normalized_device_id or normalized_device_id in ignored_device_ids:
            continue

        entry = merged.get(normalized_device_id)
        if entry is None:
            entry = build_admin_device_entry(normalized_device_id)
            merged[normalized_device_id] = entry

        entry["server_registered"] = True

    service_configs = list_device_service_configs(merged.keys(), accounts_by_device=accounts_by_device)

    alert_summaries = fetch_active_alert_summaries(merged.keys())
    for device_id, entry in merged.items():
        online = admin_device_is_online(entry)
        telemetry_status = str(entry.get("telemetry_status") or "").strip().lower()
        alert_summary = alert_summaries.get(device_id, {})
        service_config = service_configs.get(device_id) or default_device_service_config(
            device_id,
            account=accounts_by_device.get(device_id),
        )
        active_alert_count = int(alert_summary.get("active_alert_count") or 0)
        entry["admin_status"] = "online" if online else "offline"
        entry["admin_status_label"] = "Online" if online else "Offline"
        entry["telemetry_status_label"] = admin_telemetry_status_label(telemetry_status)
        entry["status_sort_value"] = 0 if online else 1
        entry["active_alert_count"] = active_alert_count
        entry["warning_alert_label"] = "Clear" if active_alert_count == 0 else f"{active_alert_count} active"
        entry["latest_alert_message"] = alert_summary.get("latest_alert_message") or "No active alerts."
        entry["latest_alert_severity"] = (
            alert_summary.get("highest_alert_severity")
            or alert_summary.get("latest_alert_severity")
            or "info"
        )
        entry.update(service_config)
        entry["service_profile_tone"] = (
            "info"
            if service_config.get("cloud_feed_mode") == DEVICE_SERVICE_CLOUD_FEED_FULL
            else "warning"
            if service_config.get("cloud_feed_mode") == DEVICE_SERVICE_CLOUD_FEED_BASIC
            else "clear"
        )

    return sorted(
        merged.values(),
        key=lambda item: str(item.get("device_id") or "").lower(),
    )


def filter_admin_search_results(items, search_query):
    normalized_query = " ".join(str(search_query or "").strip().lower().split())
    if not normalized_query:
        return list(items)

    filtered = []
    for item in items:
        haystack = " ".join(
            str(value or "").strip().lower()
            for value in (item.get("device_id"), item.get("display_name"))
        )
        if normalized_query in haystack:
            filtered.append(item)
    return filtered


def authenticate_device_request(payload=None):
    payload = payload or {}
    header_device_id = request.headers.get("X-Device-Id")
    payload_device_id = payload.get("device_id")
    normalized_header_device_id = normalize_device_id(header_device_id)
    normalized_payload_device_id = normalize_device_id(payload_device_id)
    if normalized_header_device_id and normalized_payload_device_id and normalized_header_device_id != normalized_payload_device_id:
        return False, jsonify({"error": "device_id does not match X-Device-Id header"}), 403

    device_id = normalized_header_device_id or normalized_payload_device_id
    device_key = request.headers.get("X-Device-Key") or payload.get("device_key")
    auth_ok, normalized_device_id, error_message, status_code = authenticate_device_identity(
        device_id,
        device_key=device_key,
        remote_addr=request.remote_addr,
    )
    if not auth_ok:
        return False, jsonify({"error": error_message}), status_code
    return True, normalized_device_id, None


def authenticate_device_identity(device_id, device_key=None, remote_addr=None, require_key=None):
    normalized_device_id = normalize_device_id(device_id)
    if not configured_device_auth_enabled():
        return True, normalized_device_id, None, None

    if require_key is None:
        require_key = True

    if not normalized_device_id or (require_key and not device_key):
        return False, None, "device credentials required", 401

    matched_rule = find_matching_device_key_rule(normalized_device_id)
    if not matched_rule:
        logger.warning("Rejected device auth for %s", normalized_device_id or "<missing>")
        return False, None, "invalid device credentials", 403

    if require_key and device_key != matched_rule["key"]:
        logger.warning("Rejected device auth for %s", normalized_device_id)
        return False, None, "invalid device credentials", 403

    remember_registered_device(
        normalized_device_id,
        registration_source=f"device_keys_{matched_rule['kind']}",
        key_rule=matched_rule.get("pattern"),
        best_effort=True,
    )
    return True, normalized_device_id, None, None


def relay_headers_for_device(device_id):
    headers = {"Content-Type": "application/json"}
    resolved_key = configured_device_key_for_id(device_id)
    if device_id and resolved_key:
        headers["X-Device-Id"] = device_id
        headers["X-Device-Key"] = resolved_key
    return headers


def latest_snapshot_with_metrics(db):
    row = fetch_latest_row(db)
    if not row:
        return None
    motor_cycles, leak_events = recent_counts(db)
    return enrich_snapshot(dict(row), motor_cycles, leak_events)


def refresh_operational_alerts(snapshot=None):
    if snapshot is None:
        with get_db() as db:
            snapshot = latest_snapshot_with_metrics(db)
    evaluate_snapshot_alerts(snapshot)
    return snapshot


def get_pandas():
    global pd, PANDAS_IMPORT_ERROR
    if pd is not None:
        return pd
    try:
        import pandas as pd_local
        pd = pd_local
        PANDAS_IMPORT_ERROR = None
        return pd
    except Exception as exc:
        PANDAS_IMPORT_ERROR = str(exc)
        logger.warning("Pandas unavailable: %s", exc)
        return None


def resolve_level_forecast_artifact_path():
    configured_path = os.environ.get(LEVEL_FORECAST_MODEL_PATH_ENV, "").strip()
    if configured_path:
        artifact_path = Path(configured_path).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = PROJECT_ROOT / artifact_path
        return artifact_path
    return DEFAULT_LEVEL_FORECAST_MODEL_PATH


def load_level_forecast_artifact(force_reload=False):
    artifact_path = resolve_level_forecast_artifact_path()
    if not artifact_path.exists():
        raise FileNotFoundError(f"Level forecast model artifact not found: {artifact_path}")

    try:
        modified_at = artifact_path.stat().st_mtime
    except OSError as exc:
        raise RuntimeError(f"Unable to read level forecast model metadata: {exc}") from exc

    with level_forecast_model_lock:
        cached_artifact = level_forecast_model_cache.get("artifact")
        if (
            not force_reload
            and cached_artifact is not None
            and level_forecast_model_cache.get("path") == str(artifact_path)
            and level_forecast_model_cache.get("modified_at") == modified_at
        ):
            cached_timestamp = datetime.fromtimestamp(modified_at, tz=timezone.utc).replace(tzinfo=None)
            return cached_artifact, artifact_path, cached_timestamp

        try:
            from flask_app.ml_forecasting import load_forecast_artifact

            artifact = load_forecast_artifact(artifact_path)
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "ML dependencies are unavailable. Install them with: pip install -r requirements-ml.txt",
            ) from exc
        except ImportError as exc:
            raise RuntimeError(f"ML forecasting helpers could not be imported: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Unable to load the level forecast model artifact: {exc}") from exc

        level_forecast_model_cache.clear()
        level_forecast_model_cache.update(
            {
                "artifact": artifact,
                "path": str(artifact_path),
                "modified_at": modified_at,
            }
        )

    loaded_at = datetime.fromtimestamp(modified_at, tz=timezone.utc).replace(tzinfo=None)
    return artifact, artifact_path, loaded_at


def build_level_forecast_payload(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")
    if not fetch_device_snapshot(normalized_device_id):
        raise LookupError("device not found")

    artifact, artifact_path, artifact_updated_at = load_level_forecast_artifact()
    try:
        from flask_app.ml_forecasting import predict_latest_level, query_device_forecast_rows
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "ML dependencies are unavailable. Install them with: pip install -r requirements-ml.txt",
        ) from exc
    except ImportError as exc:
        raise RuntimeError(f"ML forecasting helpers could not be imported: {exc}") from exc

    with get_db() as db:
        raw = query_device_forecast_rows(db, normalized_device_id, device_source=get_device_source_mode())
    prediction = predict_latest_level(raw, artifact)

    metrics = {}
    for key, value in dict(artifact.get("metrics") or {}).items():
        try:
            metrics[key] = round(float(value), 4)
        except (TypeError, ValueError):
            metrics[key] = value

    metadata = dict(artifact.get("metadata") or {})
    try:
        artifact_label = str(artifact_path.relative_to(PROJECT_ROOT))
    except ValueError:
        artifact_label = str(artifact_path)
    artifact_label = artifact_label.replace("\\", "/")

    current_level_percent = prediction.get("current_level_percent")
    predicted_level_percent = prediction.get("predicted_level_percent")
    predicted_delta_percent = prediction.get("predicted_delta_percent")
    predicted_remaining_liters = prediction.get("predicted_remaining_liters")
    raw_predicted_level_percent = prediction.get("raw_predicted_level_percent")

    return {
        "device_id": prediction.get("device_id") or normalized_device_id,
        "observed_at": format_timestamp(prediction.get("observed_at")),
        "forecast_for": format_timestamp(prediction.get("forecast_for")),
        "current_level_percent": None
        if current_level_percent is None
        else round(float(current_level_percent), 4),
        "predicted_level_percent": None
        if predicted_level_percent is None
        else round(float(predicted_level_percent), 4),
        "predicted_delta_percent": None
        if predicted_delta_percent is None
        else round(float(predicted_delta_percent), 4),
        "predicted_remaining_liters": None
        if predicted_remaining_liters is None
        else round(float(predicted_remaining_liters), 2),
        "raw_predicted_level_percent": None
        if raw_predicted_level_percent is None
        else round(float(raw_predicted_level_percent), 4),
        "prediction_clamped": bool(prediction.get("prediction_clamped")),
        "rows_considered": int(prediction.get("rows_considered") or 0),
        "model": {
            "artifact_path": artifact_label,
            "artifact_updated_at": format_timestamp(artifact_updated_at),
            "family": metadata.get("model_family"),
            "target": metadata.get("target"),
            "horizon_hours": metadata.get("horizon_hours"),
            "resample_minutes": metadata.get("resample_minutes"),
            "device_id_filter": metadata.get("device_id_filter"),
            "train_rows": artifact.get("train_rows"),
            "test_rows": artifact.get("test_rows"),
            "metrics": metrics,
        },
    }


def sanitize_payload(payload):
    if payload is None:
        return payload
    cleaned = {}
    for key, value in payload.items():
        if isinstance(value, bool) or value is None:
            cleaned[key] = value
            continue
        if isinstance(value, (int, str)):
            cleaned[key] = value
            continue
        if isinstance(value, float):
            cleaned[key] = value if math.isfinite(value) else None
            continue
        # Fallback to string for unknown types
        cleaned[key] = str(value)
    return cleaned


SOURCE_TANK_ALIAS_FIELDS = {
    "lower_tank_level": ("source_tank_level", "source_level"),
    "lower_sensor": ("source_tank_sensor", "source_sensor"),
    "lower_sensor_info": ("source_tank_sensor_info", "source_sensor_info"),
    "lower_sensor_distance_cm": ("source_tank_sensor_distance_cm", "source_sensor_distance_cm"),
    "lower_tank_service": ("source_tank_service", "source_service"),
}


def apply_source_tank_aliases(payload, include_aliases=False):
    if payload is None:
        return payload

    for canonical_key, alias_keys in SOURCE_TANK_ALIAS_FIELDS.items():
        canonical_value = payload.get(canonical_key)
        if canonical_value in (None, "", "null"):
            for alias_key in alias_keys:
                alias_value = payload.get(alias_key)
                if alias_value not in (None, "", "null"):
                    payload[canonical_key] = alias_value
                    canonical_value = alias_value
                    break

        if include_aliases:
            if canonical_key in payload or any(alias_key in payload for alias_key in alias_keys):
                for alias_key in alias_keys:
                    payload[alias_key] = canonical_value

    return payload


def collect_database_file_sizes(db_path=None):
    base_path = normalize_db_path(str(db_path or DB_FILE))
    paths = {
        "main_bytes": base_path,
        "wal_bytes": Path(f"{base_path}-wal"),
        "shm_bytes": Path(f"{base_path}-shm"),
    }
    sizes = {}
    total_bytes = 0
    for key, path in paths.items():
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        sizes[key] = int(size_bytes)
        total_bytes += int(size_bytes)
    sizes["total_bytes"] = total_bytes
    return sizes


def database_size_pressure(file_sizes=None):
    sizes = file_sizes or collect_database_file_sizes()
    return DB_TARGET_SIZE_BYTES > 0 and int(sizes.get("total_bytes") or 0) >= DB_TARGET_SIZE_BYTES


def retention_maintenance_skip_reason(pruned_rows=0, force=False):
    if force or not TEMP_DB_SIZE_GUARD_ENABLED or pruned_rows > 0:
        return None
    if DB_TARGET_SIZE_BYTES <= 0:
        return "retention-noop-no-target-size"
    if not database_size_pressure():
        return "retention-noop-under-target-size"
    return None


def prune_telemetry_batch_for_size_cap(cursor, batch_rows=None):
    cursor.execute(
        """
        DELETE FROM tank_data
        WHERE id IN (
            SELECT id FROM (
                SELECT id
                FROM tank_data
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM tank_data
                    GROUP BY COALESCE(device_id, '')
                )
                ORDER BY created_at ASC, id ASC
                LIMIT ?
            )
        )
        """,
        (max(1, int(batch_rows or TEMP_HARD_DB_CAP_BATCH_ROWS)),),
    )
    return max(0, int(cursor.rowcount or 0))


def maybe_prune_telemetry_size_cap(force=False):
    if not TEMP_HARD_DB_CAP_ENABLED and not force:
        return {"rows": 0, "batches": 0, "remaining_pressure": False}
    if DB_TARGET_SIZE_BYTES <= 0:
        return {"rows": 0, "batches": 0, "remaining_pressure": False}

    current_sizes = collect_database_file_sizes()
    remaining_pressure = database_size_pressure(current_sizes)
    if not remaining_pressure and not force:
        return {"rows": 0, "batches": 0, "remaining_pressure": False}

    total_rows = 0
    batches = 0
    while remaining_pressure and batches < TEMP_HARD_DB_CAP_MAX_BATCHES:
        with get_db() as db:
            deleted_rows = prune_telemetry_batch_for_size_cap(
                db.cursor(),
                batch_rows=TEMP_HARD_DB_CAP_BATCH_ROWS,
            )
        if deleted_rows <= 0:
            break

        total_rows += deleted_rows
        batches += 1
        maybe_maintain_database(reason="telemetry-size-cap", pruned_rows=deleted_rows, force=True)
        current_sizes = collect_database_file_sizes()
        remaining_pressure = database_size_pressure(current_sizes)

    if remaining_pressure:
        logger.warning(
            "Telemetry size cap did not fully reach target: pruned_rows=%s batches=%s total_bytes=%s target_bytes=%s",
            total_rows,
            batches,
            current_sizes["total_bytes"],
            DB_TARGET_SIZE_BYTES,
        )

    return {
        "rows": total_rows,
        "batches": batches,
        "remaining_pressure": remaining_pressure,
    }


def prune_retained_rows(cursor, device_id=None, latest_row_id=None):
    pruned = {}

    cursor.execute(
        """
        DELETE FROM tank_data
        WHERE created_at < datetime('now', ?)
        """,
        (f"-{DATA_RETENTION_DAYS} day",),
    )
    pruned["tank_data_retention"] = max(0, int(cursor.rowcount or 0))

    normalized_device_id = normalize_device_id(device_id) or ""
    if latest_row_id is not None and not TELEMETRY_HISTORY_ENABLED:
        if normalized_device_id:
            cursor.execute(
                """
                DELETE FROM tank_data
                WHERE COALESCE(device_id, '') = ?
                  AND id != ?
                """,
                (normalized_device_id, latest_row_id),
            )
        else:
            cursor.execute("DELETE FROM tank_data WHERE id != ?", (latest_row_id,))
        pruned["tank_data_snapshot_only"] = max(0, int(cursor.rowcount or 0))
    elif MAX_TELEMETRY_ROWS_PER_DEVICE > 0:
        cursor.execute(
            """
            DELETE FROM tank_data
            WHERE id IN (
                SELECT id FROM (
                    SELECT id
                    FROM tank_data
                    WHERE COALESCE(device_id, '') = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
            )
            """,
            (normalized_device_id, MAX_TELEMETRY_ROWS_PER_DEVICE),
        )
        pruned["tank_data_device_cap"] = max(0, int(cursor.rowcount or 0))

    cursor.execute(
        """
        DELETE FROM device_command_queue
        WHERE delivered_at IS NOT NULL
          AND delivered_at < datetime('now', ?)
        """,
        (f"-{DEVICE_COMMAND_RETENTION_DAYS} day",),
    )
    pruned["device_command_queue_retention"] = max(0, int(cursor.rowcount or 0))

    cursor.execute(
        """
        DELETE FROM ops_audit_log
        WHERE created_at < datetime('now', ?)
        """,
        (f"-{OPS_AUDIT_RETENTION_DAYS} day",),
    )
    pruned["ops_audit_log_retention"] = max(0, int(cursor.rowcount or 0))

    cursor.execute(
        """
        DELETE FROM ops_alerts
        WHERE active = 0
          AND COALESCE(resolved_at, updated_at, created_at) < datetime('now', ?)
        """,
        (f"-{OPS_ALERT_RETENTION_DAYS} day",),
    )
    pruned["ops_alerts_retention"] = max(0, int(cursor.rowcount or 0))
    return pruned


def maybe_prune_retained_rows(device_id=None, latest_row_id=None, force=False):
    now = time.time()
    if (
        not force
        and DB_PRUNE_MIN_INTERVAL_SECONDS > 0
        and (now - float(db_prune_state.get("last_run_at") or 0.0)) < DB_PRUNE_MIN_INTERVAL_SECONDS
    ):
        return {}

    if not db_prune_lock.acquire(blocking=False):
        return {}

    try:
        now = time.time()
        if (
            not force
            and DB_PRUNE_MIN_INTERVAL_SECONDS > 0
            and (now - float(db_prune_state.get("last_run_at") or 0.0)) < DB_PRUNE_MIN_INTERVAL_SECONDS
        ):
            return {}

        with get_db() as db:
            cursor = db.cursor()
            pruned = prune_retained_rows(
                cursor,
                device_id=device_id,
                latest_row_id=latest_row_id,
            )

        size_cap_result = maybe_prune_telemetry_size_cap(force=force)
        size_cap_rows = int(size_cap_result.get("rows") or 0)
        if size_cap_rows > 0:
            pruned["tank_data_size_cap"] = size_cap_rows

        pruned_rows = sum(int(value or 0) for value in pruned.values())
        db_prune_state.update(
            {
                "last_run_at": now,
                "last_pruned_rows": pruned_rows,
                "last_error": None,
                "last_size_cap_rows": size_cap_rows,
                "last_size_cap_batches": int(size_cap_result.get("batches") or 0),
                "last_size_cap_remaining_pressure": bool(size_cap_result.get("remaining_pressure")),
            }
        )
        if int(size_cap_result.get("batches") or 0) > 0:
            return pruned
        skip_reason = retention_maintenance_skip_reason(pruned_rows=pruned_rows, force=force)
        if skip_reason:
            db_maintenance_state.update(
                {
                    "last_skip_at": now,
                    "last_skip_reason": skip_reason,
                }
            )
            return pruned
        maybe_maintain_database(reason="telemetry-retention", pruned_rows=pruned_rows)
        return pruned
    except Exception as exc:
        db_prune_state["last_error"] = str(exc)
        logger.warning("Retention prune failed: %s", exc)
        return {}
    finally:
        db_prune_lock.release()


def maybe_maintain_database(reason="periodic", pruned_rows=0, force=False):
    if not DB_MAINTENANCE_ENABLED and not force:
        return False

    now = time.time()
    before_sizes = collect_database_file_sizes()
    size_pressure = database_size_pressure(before_sizes)
    if (
        not force
        and pruned_rows <= 0
        and not size_pressure
        and (now - float(db_maintenance_state.get("last_run_at") or 0.0)) < DB_MAINTENANCE_MIN_INTERVAL_SECONDS
    ):
        return False

    if not db_maintenance_lock.acquire(blocking=False):
        return False

    try:
        now = time.time()
        before_sizes = collect_database_file_sizes()
        size_pressure = database_size_pressure(before_sizes)
        if (
            not force
            and pruned_rows <= 0
            and not size_pressure
            and (now - float(db_maintenance_state.get("last_run_at") or 0.0)) < DB_MAINTENANCE_MIN_INTERVAL_SECONDS
        ):
            return False

        with sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False) as db:
            db.isolation_level = None
            db.execute("PRAGMA busy_timeout=30000")
            db.execute(f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT_PAGES}")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            auto_vacuum_row = db.execute("PRAGMA auto_vacuum").fetchone()
            auto_vacuum_mode = int(auto_vacuum_row[0] or 0) if auto_vacuum_row else 0

            if auto_vacuum_mode == 2 and (pruned_rows > 0 or size_pressure):
                page_size_row = db.execute("PRAGMA page_size").fetchone()
                page_size = int(page_size_row[0] or 4096) if page_size_row else 4096
                pages_to_free = max(128, min(8192, max(1, int(pruned_rows or 1)) * 2))
                if size_pressure and DB_TARGET_SIZE_BYTES > 0 and before_sizes["total_bytes"] > DB_TARGET_SIZE_BYTES:
                    extra_pages = int(
                        math.ceil((before_sizes["total_bytes"] - DB_TARGET_SIZE_BYTES) / max(page_size, 1))
                    )
                    pages_to_free = max(pages_to_free, min(16384, max(256, extra_pages)))
                db.execute(f"PRAGMA incremental_vacuum({int(pages_to_free)})")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            elif size_pressure:
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            db.execute("PRAGMA optimize")

        after_sizes = collect_database_file_sizes()
        db_maintenance_state.update(
            {
                "last_run_at": now,
                "last_reason": reason,
                "last_error": None,
                "last_total_bytes": after_sizes["total_bytes"],
                "last_skip_at": 0.0,
                "last_skip_reason": None,
            }
        )
        if pruned_rows > 0 or size_pressure or after_sizes["total_bytes"] != before_sizes["total_bytes"]:
            log_fn = logger.warning if size_pressure else logger.info
            log_fn(
                "Database maintenance (%s): pruned_rows=%s total_bytes=%s->%s target_bytes=%s",
                reason,
                pruned_rows,
                before_sizes["total_bytes"],
                after_sizes["total_bytes"],
                DB_TARGET_SIZE_BYTES,
            )
        return True
    except (sqlite3.DatabaseError, OSError) as exc:
        db_maintenance_state.update(
            {
                "last_run_at": time.time(),
                "last_reason": reason,
                "last_error": str(exc),
                "last_skip_at": 0.0,
                "last_skip_reason": None,
            }
        )
        logger.warning("Database maintenance failed (%s): %s", reason, exc)
        return False
    finally:
        db_maintenance_lock.release()


def process_telemetry_payload(data, source_ip=None, transport="http"):
    cleaned = sanitize_payload(dict(data or {}))
    cleaned.pop("device_key", None)
    cleaned.pop("simulator", None)
    cleaned.pop("source_tank_simulator", None)
    apply_source_tank_aliases(cleaned)
    cleaned["device_source"] = normalize_device_source(cleaned.get("device_source"), default=DEVICE_SOURCE_REAL)

    mode = str(cleaned.get("mode", "AUTO")).upper()
    if mode not in {"AUTO", "MANUAL"}:
        mode = "AUTO"

    latest_row_id = None
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            """
            INSERT INTO tank_data (
                level, motor, mode,
                runtime, current_runtime, last_runtime,
                fill_time,
                leak, pump_failure, abnormal,
                drip, slow_leak, pipe_leak,
                ai_usage_rate, tomorrow_prediction,
                dry_run,
                wifi, wifi_rssi, sensor,
                device_source,
                sensor_info, sensor_distance_cm,
                tank_height_cm, tank_capacity_liters,
                auto_status, auto_status_tone, auto_timer,
                tank_health, free_heap, uptime_s,
                lower_tank_level, lower_sensor, lower_sensor_info, lower_sensor_distance_cm,
                device_id, firmware_version, reset_reason, source_ip, device_local_url,
                channel_mode, telemetry_service, command_service, ota_service, lower_tank_service,
                buzzer_service, led_display_service
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cleaned.get("level"),
                cleaned.get("motor"),
                mode,
                cleaned.get("runtime"),
                cleaned.get("current_runtime"),
                cleaned.get("last_runtime"),
                cleaned.get("fill_time"),
                cleaned.get("leak"),
                cleaned.get("pump_failure"),
                cleaned.get("abnormal"),
                cleaned.get("drip"),
                cleaned.get("slow_leak"),
                cleaned.get("pipe_leak"),
                cleaned.get("ai_usage_rate"),
                cleaned.get("tomorrow_prediction"),
                cleaned.get("dry_run"),
                cleaned.get("wifi"),
                cleaned.get("wifi_rssi"),
                cleaned.get("sensor"),
                cleaned.get("device_source"),
                cleaned.get("sensor_info"),
                cleaned.get("sensor_distance_cm"),
                cleaned.get("tank_height_cm"),
                cleaned.get("tank_capacity_liters"),
                cleaned.get("auto_status"),
                cleaned.get("auto_status_tone"),
                cleaned.get("auto_timer"),
                cleaned.get("tank_health"),
                cleaned.get("free_heap"),
                cleaned.get("uptime_s"),
                cleaned.get("lower_tank_level"),
                cleaned.get("lower_sensor"),
                cleaned.get("lower_sensor_info"),
                cleaned.get("lower_sensor_distance_cm"),
                cleaned.get("device_id"),
                cleaned.get("firmware_version"),
                cleaned.get("reset_reason"),
                source_ip or transport,
                cleaned.get("device_local_url"),
                cleaned.get("channel_mode"),
                cleaned.get("telemetry_service"),
                cleaned.get("command_service"),
                cleaned.get("ota_service"),
                cleaned.get("lower_tank_service"),
                cleaned.get("buzzer_service"),
                cleaned.get("led_display_service"),
            ),
        )
        latest_row_id = cursor.lastrowid

    maybe_prune_retained_rows(
        device_id=cleaned.get("device_id"),
        latest_row_id=latest_row_id,
    )
    clear_runtime_caches(cleaned.get("device_id"))
    logger.info(
        "Saved tank level via %s: %s | Motor: %s | Mode: %s | Device: %s",
        transport,
        cleaned.get("level"),
        cleaned.get("motor"),
        mode,
        cleaned.get("device_id"),
    )

    if cleaned.get("device_source") == get_device_source_mode():
        alert_snapshot = dict(cleaned)
        alert_snapshot["telemetry_status"] = "fresh"
        evaluate_snapshot_alerts(alert_snapshot)
        if cleaned.get("device_source") == DEVICE_SOURCE_REAL:
            relay_status_async(cleaned)
    return cleaned


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=20000")
    conn.execute(f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT_PAGES}")
    return conn


def ensure_tank_data_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(tank_data)").fetchall()}
    required = {
        "device_source": "TEXT",
        "sensor_info": "TEXT",
        "sensor_distance_cm": "REAL",
        "tank_height_cm": "REAL",
        "tank_capacity_liters": "REAL",
        "auto_status": "TEXT",
        "auto_status_tone": "TEXT",
        "auto_timer": "TEXT",
        "tank_health": "REAL",
        "free_heap": "INTEGER",
        "uptime_s": "INTEGER",
        "lower_tank_level": "REAL",
        "lower_sensor": "TEXT",
        "lower_sensor_info": "TEXT",
        "lower_sensor_distance_cm": "REAL",
        "device_id": "TEXT",
        "firmware_version": "TEXT",
        "reset_reason": "TEXT",
        "source_ip": "TEXT",
        "device_local_url": "TEXT",
        "channel_mode": "TEXT",
        "telemetry_service": "TEXT",
        "command_service": "TEXT",
        "ota_service": "TEXT",
        "lower_tank_service": "TEXT",
        "buzzer_service": "TEXT",
        "led_display_service": "TEXT",
    }

    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE tank_data ADD COLUMN {column} {definition}")


def rebuild_tank_data_without_simulator_columns(cursor):
    existing = [row[1] for row in cursor.execute("PRAGMA table_info(tank_data)").fetchall()]
    obsolete = {"simulator", "source_tank_simulator"}
    if not set(existing).intersection(obsolete):
        return

    preserved_columns = [column for column in existing if column not in obsolete]
    if not preserved_columns:
        return

    preserved_sql = ", ".join(preserved_columns)
    logger.info("Removing obsolete simulator telemetry columns from tank_data")
    cursor.execute("ALTER TABLE tank_data RENAME TO tank_data_legacy")
    cursor.execute(
        """
        CREATE TABLE tank_data(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level REAL,
            motor TEXT,
            mode TEXT,
            runtime REAL,
            current_runtime REAL,
            last_runtime REAL,
            fill_time REAL,
            leak TEXT,
            pump_failure TEXT,
            abnormal TEXT,
            drip TEXT,
            slow_leak TEXT,
            pipe_leak TEXT,
            ai_usage_rate REAL,
            tomorrow_prediction REAL,
            dry_run TEXT,
            wifi TEXT,
            wifi_rssi INTEGER,
            sensor TEXT,
            device_source TEXT,
            sensor_info TEXT,
            sensor_distance_cm REAL,
            tank_height_cm REAL,
            tank_capacity_liters REAL,
            auto_status TEXT,
            auto_status_tone TEXT,
            auto_timer TEXT,
            tank_health REAL,
            free_heap INTEGER,
            uptime_s INTEGER,
            lower_tank_level REAL,
            lower_sensor TEXT,
            lower_sensor_info TEXT,
            lower_sensor_distance_cm REAL,
            device_id TEXT,
            firmware_version TEXT,
            reset_reason TEXT,
            source_ip TEXT,
            device_local_url TEXT,
            channel_mode TEXT,
            telemetry_service TEXT,
            command_service TEXT,
            ota_service TEXT,
            lower_tank_service TEXT,
            buzzer_service TEXT,
            led_display_service TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        f"""
        INSERT INTO tank_data ({preserved_sql})
        SELECT {preserved_sql}
        FROM tank_data_legacy
        """
    )
    cursor.execute("DROP TABLE tank_data_legacy")


def ensure_relay_queue_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS relay_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payload TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT,
            last_error TEXT,
            last_status_code INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_device_command_queue_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS device_command_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_device TEXT NOT NULL,
            command TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            delivered_at TEXT
        )
        """
    )


def ensure_alerts_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT
        )
        """
    )


def ensure_audit_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            device_id TEXT,
            details TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_app_settings_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings(
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_customer_accounts_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_accounts(
            device_id TEXT PRIMARY KEY,
            display_name TEXT,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            cloud_feed_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_customer_accounts_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(customer_accounts)").fetchall()}
    required = {
        "display_name": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "cloud_feed_enabled": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE customer_accounts ADD COLUMN {column} {definition}")


def ensure_device_service_configs_table(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS device_service_configs(
            device_id TEXT PRIMARY KEY,
            source_tank_monitoring_enabled INTEGER NOT NULL DEFAULT 1,
            ai_analysis_enabled INTEGER NOT NULL DEFAULT 1,
            cloud_feed_mode TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}',
            buzzer_enabled INTEGER NOT NULL DEFAULT 1,
            led_display_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_device_service_configs_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(device_service_configs)").fetchall()}
    required = {
        "source_tank_monitoring_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_analysis_enabled": "INTEGER NOT NULL DEFAULT 1",
        "cloud_feed_mode": f"TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}'",
        "buzzer_enabled": "INTEGER NOT NULL DEFAULT 1",
        "led_display_enabled": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE device_service_configs ADD COLUMN {column} {definition}")


def ensure_registered_devices_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS registered_devices(
            device_id TEXT PRIMARY KEY,
            registration_source TEXT NOT NULL,
            key_rule TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_ignored_devices_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ignored_devices(
            device_id TEXT PRIMARY KEY,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def load_base64_json_env(name):
    raw_value = os.environ.get(name, "").strip()
    if not raw_value:
        return None
    try:
        decoded = base64.b64decode(raw_value).decode("utf-8")
        return json.loads(decoded)
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        logger.warning("%s is set but could not be decoded as base64 JSON.", name)
        return None


def seed_bootstrap_dashboard_password(cursor):
    password_hash = os.environ.get(DASHBOARD_PASSWORD_HASH_ENV, "").strip()
    if not password_hash:
        return
    if not password_looks_hashed(password_hash):
        logger.warning("%s is set but does not look like a supported password hash.", DASHBOARD_PASSWORD_HASH_ENV)
        return
    row = cursor.execute(
        "SELECT value FROM app_settings WHERE key = ?",
        (DASHBOARD_PASSWORD_SETTING,),
    ).fetchone()
    existing_value = str(row[0] or "").strip() if row else ""
    if existing_value:
        return
    cursor.execute(
        """
        INSERT INTO app_settings(key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO NOTHING
        """,
        (DASHBOARD_PASSWORD_SETTING, password_hash),
    )


def seed_bootstrap_customer_accounts(cursor):
    payload = load_base64_json_env(CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV)
    if not payload:
        return
    existing_row = cursor.execute("SELECT 1 FROM customer_accounts LIMIT 1").fetchone()
    if existing_row:
        return
    accounts = payload if isinstance(payload, list) else payload.get("accounts", [])
    if not isinstance(accounts, list):
        logger.warning("%s must decode to a list or an object with an 'accounts' list.", CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV)
        return
    seeded_count = 0
    for item in accounts:
        if not isinstance(item, dict):
            continue
        normalized_device_id = normalize_device_id(item.get("device_id"))
        password_hash = str(item.get("password_hash") or "").strip()
        display_name = str(item.get("display_name") or "").strip() or None
        active = 1 if int(item.get("active", 1) or 0) == 1 else 0
        cloud_feed_enabled = 1 if int(item.get("cloud_feed_enabled", 1) or 0) == 1 else 0
        if not normalized_device_id or not password_hash:
            continue
        cursor.execute(
            """
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, cloud_feed_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO NOTHING
            """,
            (normalized_device_id, display_name, password_hash, active, cloud_feed_enabled),
        )
        seeded_count += 1
    if seeded_count:
        logger.warning(
            "Seeded %s customer account(s) from %s because the live database was empty.",
            seeded_count,
            CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV,
        )


def seed_default_customer_accounts(cursor):
    if not SEED_DEFAULT_CUSTOMERS:
        return
    if not DEFAULT_CUSTOMER_PASSWORD or len(DEFAULT_CUSTOMER_PASSWORD) < 6:
        logger.warning("Skipping default customer account seed because DEFAULT_CUSTOMER_PASSWORD is too short.")
        return
    password_hash = generate_password_hash(DEFAULT_CUSTOMER_PASSWORD)
    for device_id, display_name in DEFAULT_CUSTOMER_ACCOUNTS:
        cursor.execute(
            """
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, cloud_feed_enabled, updated_at)
            VALUES (?, ?, ?, 1, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO NOTHING
            """,
            (device_id, display_name, password_hash),
        )


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db_exists = os.path.exists(DB_FILE)

    if db_exists:
        logger.info("Database file already exists: %s", DB_FILE)
    else:
        logger.info("Creating new database file: %s", DB_FILE)

    with sqlite3.connect(DB_FILE) as db:
        cursor = db.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA wal_autocheckpoint={DB_WAL_AUTOCHECKPOINT_PAGES}")
        auto_vacuum_row = cursor.execute("PRAGMA auto_vacuum").fetchone()
        auto_vacuum_mode = int(auto_vacuum_row[0] or 0) if auto_vacuum_row else 0
        if auto_vacuum_mode != 2:
            cursor.execute("PRAGMA auto_vacuum=INCREMENTAL")
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tank_data(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level REAL,
                motor TEXT,
                mode TEXT,
                runtime REAL,
                current_runtime REAL,
                last_runtime REAL,
                fill_time REAL,
                leak TEXT,
                pump_failure TEXT,
                abnormal TEXT,
                drip TEXT,
                slow_leak TEXT,
                pipe_leak TEXT,
                ai_usage_rate REAL,
                tomorrow_prediction REAL,
                dry_run TEXT,
                wifi TEXT,
                wifi_rssi INTEGER,
                sensor TEXT,
                device_source TEXT,
                sensor_info TEXT,
                sensor_distance_cm REAL,
                tank_height_cm REAL,
                tank_capacity_liters REAL,
                auto_status TEXT,
                auto_status_tone TEXT,
                auto_timer TEXT,
                tank_health REAL,
                free_heap INTEGER,
                uptime_s INTEGER,
                lower_tank_level REAL,
                lower_sensor TEXT,
                lower_sensor_info TEXT,
                lower_sensor_distance_cm REAL,
                device_id TEXT,
                firmware_version TEXT,
                reset_reason TEXT,
                source_ip TEXT,
                device_local_url TEXT,
                channel_mode TEXT,
                telemetry_service TEXT,
                command_service TEXT,
                ota_service TEXT,
                lower_tank_service TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        ensure_tank_data_columns(cursor)
        rebuild_tank_data_without_simulator_columns(cursor)
        ensure_relay_queue_table(cursor)
        ensure_device_command_queue_table(cursor)
        ensure_alerts_table(cursor)
        ensure_audit_table(cursor)
        ensure_app_settings_table(cursor)
        seed_bootstrap_dashboard_password(cursor)
        ensure_customer_accounts_table(cursor)
        ensure_customer_accounts_columns(cursor)
        ensure_device_service_configs_table(cursor)
        ensure_device_service_configs_columns(cursor)
        ensure_registered_devices_table(cursor)
        ensure_ignored_devices_table(cursor)
        seed_bootstrap_customer_accounts(cursor)
        seed_default_customer_accounts(cursor)
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_created_at
            ON tank_data(created_at)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tank_data_device_created
            ON tank_data(device_id, created_at DESC, id DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alerts_active
            ON ops_alerts(active, kind, device_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_device_command_queue_target_pending
            ON device_command_queue(target_device, delivered_at, id DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_device_command_queue_target_pending
            ON device_command_queue(target_device, delivered_at, id DESC)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_device_created
            ON ops_audit_log(device_id, created_at)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_registered_devices_last_seen
            ON registered_devices(last_seen_at, device_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_device_service_configs_updated
            ON device_service_configs(updated_at, device_id)
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_ignored_devices_updated
            ON ignored_devices(updated_at, device_id)
            """
        )

    maybe_reset_device_source_mode_on_boot()
    deleted_counts = purge_configured_virtual_device_records()
    if deleted_counts.get("device_ids"):
        logger.info(
            "Purged %s configured virtual devices from startup database state.",
            deleted_counts["device_ids"],
        )
    seed_registered_devices_from_configuration()
    logger.info("Database initialization complete")


def get_app_setting(key, default=None):
    with get_db() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    value = row["value"]
    return default if value in (None, "") else value


def set_app_setting(key, value):
    with get_db() as db:
        db.execute(
            """
            INSERT INTO app_settings(key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=CURRENT_TIMESTAMP
            """,
            (key, value),
        )


def delete_app_setting(key):
    with get_db() as db:
        db.execute("DELETE FROM app_settings WHERE key = ?", (key,))


def maybe_reset_device_source_mode_on_boot():
    if not RESET_DEVICE_SOURCE_MODE_ON_BOOT:
        return
    current_mode = get_app_setting(DEVICE_SOURCE_MODE_SETTING)
    if normalize_device_source(current_mode, default=DEFAULT_DEVICE_SOURCE_MODE) == DEFAULT_DEVICE_SOURCE_MODE:
        return
    set_app_setting(DEVICE_SOURCE_MODE_SETTING, DEFAULT_DEVICE_SOURCE_MODE)
    clear_runtime_caches()
    logger.info(
        "Device source mode reset to %s from startup configuration.",
        DEFAULT_DEVICE_SOURCE_MODE,
    )


def get_device_source_mode():
    configured = get_app_setting(DEVICE_SOURCE_MODE_SETTING, DEFAULT_DEVICE_SOURCE_MODE)
    return normalize_device_source(configured, default=DEFAULT_DEVICE_SOURCE_MODE)


def set_device_source_mode(mode):
    normalized_mode = normalize_device_source(mode, default=None)
    if normalized_mode not in {DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL}:
        raise ValueError("device source mode must be 'real' or 'virtual'")
    set_app_setting(DEVICE_SOURCE_MODE_SETTING, normalized_mode)
    clear_runtime_caches()
    return normalized_mode


def parse_explicit_device_source(value):
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in {DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL}:
        return raw
    raise ValueError("device_source must be 'real' or 'virtual'")


def resolve_request_device_source(payload=None):
    payload = payload or {}
    header_source = parse_explicit_device_source(request.headers.get(DEVICE_SOURCE_HEADER))
    payload_source = parse_explicit_device_source(payload.get("device_source"))
    if header_source and payload_source and header_source != payload_source:
        raise ValueError("device_source does not match X-Device-Source header")
    return header_source or payload_source or DEVICE_SOURCE_REAL


def active_device_source_conflict_response(request_source):
    active_mode = get_device_source_mode()
    return (
        jsonify(
            {
                "error": f"{request_source} device source is inactive while backend mode is {active_mode}",
                "device_source": request_source,
                "device_source_mode": active_mode,
            }
        ),
        409,
    )


def device_source_where_clause(column="device_source", mode=None):
    normalized_mode = normalize_device_source(mode, default=get_device_source_mode())
    return f"COALESCE({column}, '{DEVICE_SOURCE_REAL}') = ?", [normalized_mode]


def ensure_app_secret_key_persisted():
    persisted_secret = str(get_app_setting(APP_SECRET_KEY_SETTING, "") or "").strip()
    if persisted_secret:
        if APP_SECRET_KEY_SOURCE == "env" and persisted_secret != app.secret_key:
            logger.warning(
                "APP_SECRET_KEY from environment differs from the database-persisted secret. "
                "Existing browser and mobile sessions from previous deploys may be invalidated."
            )
        return
    set_app_setting(APP_SECRET_KEY_SETTING, app.secret_key)


def password_looks_hashed(value):
    text = str(value or "")
    return text.startswith("scrypt:") or text.startswith("pbkdf2:")


def get_dashboard_password():
    stored_value = get_app_setting("dashboard_password")
    if stored_value is None:
        return LOGIN_PASSWORD
    if password_looks_hashed(stored_value):
        return None
    return stored_value


def verify_dashboard_password(password):
    candidate = password or ""
    stored_value = get_app_setting("dashboard_password")
    if stored_value is None:
        return candidate == LOGIN_PASSWORD
    if password_looks_hashed(stored_value):
        return check_password_hash(stored_value, candidate)
    if stored_value == candidate:
        set_dashboard_password(stored_value)
        return True
    return False


def set_dashboard_password(password):
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    set_app_setting("dashboard_password", generate_password_hash(password))


def is_default_dashboard_password():
    if LOGIN_USERNAME != DEFAULT_ADMIN_USERNAME:
        return False
    stored_value = get_app_setting("dashboard_password")
    if stored_value is None:
        return LOGIN_PASSWORD == DEFAULT_ADMIN_PASSWORD
    if password_looks_hashed(stored_value):
        return check_password_hash(stored_value, DEFAULT_ADMIN_PASSWORD)
    return stored_value == DEFAULT_ADMIN_PASSWORD


def maybe_reset_admin_password_on_boot():
    if not RESET_ADMIN_PASSWORD_ON_BOOT:
        return
    normalized_password = (LOGIN_PASSWORD or "").strip()
    if len(normalized_password) < 6:
        logger.warning("RESET_ADMIN_PASSWORD_ON_BOOT is enabled, but LOGIN_PASSWORD is shorter than 6 characters. Skipping admin password reset.")
        return
    set_dashboard_password(normalized_password)
    logger.warning("Admin dashboard password was reset from LOGIN_PASSWORD during startup because RESET_ADMIN_PASSWORD_ON_BOOT is enabled.")


def fetch_customer_account(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, display_name, password_hash, active, cloud_feed_enabled, created_at, updated_at
            FROM customer_accounts
            WHERE device_id = ?
            """,
            (normalized_device_id,),
        ).fetchone()
    return dict(row) if row else None


def list_customer_accounts(limit=100):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT device_id, display_name, active, cloud_feed_enabled, created_at, updated_at
            FROM customer_accounts
            ORDER BY updated_at DESC, device_id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def customer_cloud_feed_enabled(device_id):
    account = fetch_customer_account(device_id)
    if not account:
        return False
    return int(account.get("cloud_feed_enabled", 1) or 0) == 1


def upsert_customer_account(device_id, password, display_name=None, active=None, cloud_feed_enabled=None):
    normalized_device_id = normalize_device_id(device_id)
    normalized_display_name = str(display_name or "").strip()
    if not normalized_device_id:
        raise ValueError("device_id is required")
    if not password or len(password) < 6:
        raise ValueError("password must be at least 6 characters")
    existing = fetch_customer_account(normalized_device_id)
    default_service_config = fetch_device_service_config(normalized_device_id, account=existing)
    password_hash = generate_password_hash(password)
    resolved_active = 1 if active is None and not existing else (1 if int(active if active is not None else existing.get("active", 1) or 0) == 1 else 0)
    resolved_cloud_feed_enabled = (
        (1 if default_service_config.get("cloud_feed_mode") != DEVICE_SERVICE_CLOUD_FEED_OFF else 0)
        if cloud_feed_enabled is None and not existing
        else (1 if int(cloud_feed_enabled if cloud_feed_enabled is not None else existing.get("cloud_feed_enabled", 1) or 0) == 1 else 0)
    )
    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (normalized_device_id,))
        db.execute(
            """
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, cloud_feed_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                display_name=excluded.display_name,
                password_hash=excluded.password_hash,
                active=excluded.active,
                cloud_feed_enabled=excluded.cloud_feed_enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                normalized_device_id,
                normalized_display_name or None,
                password_hash,
                resolved_active,
                resolved_cloud_feed_enabled,
            ),
        )
    return fetch_customer_account(normalized_device_id)


def update_customer_password(device_id, password):
    account = fetch_customer_account(device_id)
    if not account:
        raise ValueError("customer account not found")
    return upsert_customer_account(
        account["device_id"],
        password,
        display_name=account.get("display_name"),
        active=account.get("active", 1),
        cloud_feed_enabled=account.get("cloud_feed_enabled", 1),
    )


def update_customer_account_profile(device_id, display_name=None, active=None, cloud_feed_enabled=None):
    account = fetch_customer_account(device_id)
    if not account:
        raise ValueError("customer account not found")

    resolved_display_name = account.get("display_name") if display_name is None else (str(display_name).strip() or None)
    resolved_active = 1 if int(active if active is not None else account.get("active", 1) or 0) == 1 else 0
    resolved_cloud_feed_enabled = (
        1
        if int(cloud_feed_enabled if cloud_feed_enabled is not None else account.get("cloud_feed_enabled", 1) or 0) == 1
        else 0
    )

    with get_db() as db:
        db.execute(
            """
            UPDATE customer_accounts
            SET display_name = ?,
                active = ?,
                cloud_feed_enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE device_id = ?
            """,
            (
                resolved_display_name,
                resolved_active,
                resolved_cloud_feed_enabled,
                account["device_id"],
            ),
        )

    return fetch_customer_account(account["device_id"])


DEVICE_SERVICE_CLOUD_FEED_OFF = "off"
DEVICE_SERVICE_CLOUD_FEED_BASIC = "basic"
DEVICE_SERVICE_CLOUD_FEED_FULL = "full"
DEVICE_SERVICE_CLOUD_MODE_LABELS = {
    DEVICE_SERVICE_CLOUD_FEED_OFF: "Disabled",
    DEVICE_SERVICE_CLOUD_FEED_BASIC: "Without AI",
    DEVICE_SERVICE_CLOUD_FEED_FULL: "With Full Features",
}


def normalize_device_service_cloud_mode(value, default=DEVICE_SERVICE_CLOUD_FEED_FULL):
    raw = str(value or "").strip().lower()
    if raw in DEVICE_SERVICE_CLOUD_MODE_LABELS:
        return raw
    return default


def boolish_enabled(value, default=True):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on", "enable", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disable", "disabled"}:
        return False
    return bool(default)


def serialize_device_service_config(device_id, payload=None, account=None):
    payload = payload or {}
    normalized_device_id = normalize_device_id(device_id or payload.get("device_id"))
    account_cloud_feed_enabled = True
    if account is not None:
        account_cloud_feed_enabled = int(account.get("cloud_feed_enabled", 1) or 0) == 1

    cloud_feed_mode = normalize_device_service_cloud_mode(
        payload.get("cloud_feed_mode"),
        default=DEVICE_SERVICE_CLOUD_FEED_FULL if account_cloud_feed_enabled else DEVICE_SERVICE_CLOUD_FEED_OFF,
    )
    source_tank_monitoring_enabled = boolish_enabled(payload.get("source_tank_monitoring_enabled"), default=True)
    ai_analysis_enabled = boolish_enabled(payload.get("ai_analysis_enabled"), default=True)
    buzzer_enabled = boolish_enabled(payload.get("buzzer_enabled"), default=True)
    led_display_enabled = boolish_enabled(payload.get("led_display_enabled"), default=True)
    effective_cloud_feed_enabled = cloud_feed_mode != DEVICE_SERVICE_CLOUD_FEED_OFF and account_cloud_feed_enabled
    effective_ai_analysis_enabled = (
        ai_analysis_enabled
        and cloud_feed_mode == DEVICE_SERVICE_CLOUD_FEED_FULL
        and effective_cloud_feed_enabled
    )
    hardware_enabled_count = int(buzzer_enabled) + int(led_display_enabled)
    cloud_note = "Customer cloud access starts after an account is created."
    if account is not None:
        cloud_note = (
            "Remote customer access is enabled."
            if effective_cloud_feed_enabled
            else "Customer cloud access is disabled for this device."
        )
    ai_label = "On" if effective_ai_analysis_enabled else ("Saved" if ai_analysis_enabled else "Off")
    return {
        "device_id": normalized_device_id,
        "source_tank_monitoring_enabled": source_tank_monitoring_enabled,
        "ai_analysis_enabled": ai_analysis_enabled,
        "effective_ai_analysis_enabled": effective_ai_analysis_enabled,
        "cloud_feed_mode": cloud_feed_mode,
        "cloud_feed_mode_label": DEVICE_SERVICE_CLOUD_MODE_LABELS.get(cloud_feed_mode, "Unknown"),
        "cloud_feed_enabled": effective_cloud_feed_enabled,
        "cloud_note": cloud_note,
        "buzzer_enabled": buzzer_enabled,
        "led_display_enabled": led_display_enabled,
        "hardware_enabled_count": hardware_enabled_count,
        "hardware_enabled_label": f"{hardware_enabled_count}/2 hardware controls active",
        "service_profile_hint": (
            f"Source {'On' if source_tank_monitoring_enabled else 'Off'}"
            f" • AI {ai_label}"
            f" • Buzzer {'On' if buzzer_enabled else 'Off'}"
            f" • LED {'On' if led_display_enabled else 'Off'}"
        ),
    }


def default_device_service_config(device_id=None, account=None):
    cloud_feed_enabled = True
    if account is not None:
        cloud_feed_enabled = int(account.get("cloud_feed_enabled", 1) or 0) == 1
    return serialize_device_service_config(
        device_id,
        {
            "source_tank_monitoring_enabled": True,
            "ai_analysis_enabled": True,
            "cloud_feed_mode": (
                DEVICE_SERVICE_CLOUD_FEED_FULL if cloud_feed_enabled else DEVICE_SERVICE_CLOUD_FEED_OFF
            ),
            "buzzer_enabled": True,
            "led_display_enabled": True,
        },
        account=account,
    )


def fetch_device_service_config(device_id, account=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return default_device_service_config(device_id, account=account)
    resolved_account = account if account is not None else fetch_customer_account(normalized_device_id)
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, source_tank_monitoring_enabled, ai_analysis_enabled,
                   cloud_feed_mode, buzzer_enabled, led_display_enabled,
                   created_at, updated_at
            FROM device_service_configs
            WHERE device_id = ?
            """,
            (normalized_device_id,),
        ).fetchone()
    if not row:
        return default_device_service_config(normalized_device_id, account=resolved_account)
    return serialize_device_service_config(normalized_device_id, dict(row), account=resolved_account)


def list_device_service_configs(device_ids=None, accounts_by_device=None):
    normalized_device_ids = [
        item for item in (normalize_device_id(value) for value in (device_ids or [])) if item
    ]
    accounts_by_device = accounts_by_device or {}
    query = (
        """
        SELECT device_id, source_tank_monitoring_enabled, ai_analysis_enabled,
               cloud_feed_mode, buzzer_enabled, led_display_enabled,
               created_at, updated_at
        FROM device_service_configs
        """
    )
    params = []
    if normalized_device_ids:
        placeholders = ",".join("?" for _ in normalized_device_ids)
        query += f" WHERE device_id IN ({placeholders})"
        params.extend(normalized_device_ids)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()
    configs = {
        normalize_device_id(row["device_id"]): serialize_device_service_config(
            row["device_id"],
            dict(row),
            account=accounts_by_device.get(normalize_device_id(row["device_id"])),
        )
        for row in rows
    }
    if normalized_device_ids:
        for normalized_device_id in normalized_device_ids:
            configs.setdefault(
                normalized_device_id,
                default_device_service_config(
                    normalized_device_id,
                    account=accounts_by_device.get(normalized_device_id),
                ),
            )
    return configs


def upsert_device_service_config(
    device_id,
    source_tank_monitoring_enabled=None,
    ai_analysis_enabled=None,
    cloud_feed_mode=None,
    buzzer_enabled=None,
    led_display_enabled=None,
):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    account = fetch_customer_account(normalized_device_id)
    existing = fetch_device_service_config(normalized_device_id, account=account)
    resolved_source_tank_monitoring_enabled = boolish_enabled(
        source_tank_monitoring_enabled,
        default=existing.get("source_tank_monitoring_enabled", True),
    )
    resolved_ai_analysis_enabled = boolish_enabled(
        ai_analysis_enabled,
        default=existing.get("ai_analysis_enabled", True),
    )
    resolved_buzzer_enabled = boolish_enabled(
        buzzer_enabled,
        default=existing.get("buzzer_enabled", True),
    )
    resolved_led_display_enabled = boolish_enabled(
        led_display_enabled,
        default=existing.get("led_display_enabled", True),
    )
    resolved_cloud_feed_mode = normalize_device_service_cloud_mode(
        cloud_feed_mode,
        default=existing.get("cloud_feed_mode", DEVICE_SERVICE_CLOUD_FEED_FULL),
    )

    with get_db() as db:
        db.execute(
            """
            INSERT INTO device_service_configs(
                device_id, source_tank_monitoring_enabled, ai_analysis_enabled,
                cloud_feed_mode, buzzer_enabled, led_display_enabled,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                source_tank_monitoring_enabled=excluded.source_tank_monitoring_enabled,
                ai_analysis_enabled=excluded.ai_analysis_enabled,
                cloud_feed_mode=excluded.cloud_feed_mode,
                buzzer_enabled=excluded.buzzer_enabled,
                led_display_enabled=excluded.led_display_enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                normalized_device_id,
                1 if resolved_source_tank_monitoring_enabled else 0,
                1 if resolved_ai_analysis_enabled else 0,
                resolved_cloud_feed_mode,
                1 if resolved_buzzer_enabled else 0,
                1 if resolved_led_display_enabled else 0,
            ),
        )

    if account:
        desired_cloud_feed_enabled = 1 if resolved_cloud_feed_mode != DEVICE_SERVICE_CLOUD_FEED_OFF else 0
        if int(account.get("cloud_feed_enabled", 1) or 0) != desired_cloud_feed_enabled:
            update_customer_account_profile(
                normalized_device_id,
                cloud_feed_enabled=desired_cloud_feed_enabled,
            )

    return fetch_device_service_config(normalized_device_id)


def build_device_service_command(service_config):
    config = service_config or {}
    return "SERVICECFG:{source}:{buzzer}:{led}".format(
        source=1 if bool(config.get("source_tank_monitoring_enabled")) else 0,
        buzzer=1 if bool(config.get("buzzer_enabled")) else 0,
        led=1 if bool(config.get("led_display_enabled")) else 0,
    )


def authenticate_dashboard_user(username, password):
    normalized_username = str(username or "").strip()
    if normalized_username == LOGIN_USERNAME and verify_dashboard_password(password):
        return {
            "role": "admin",
            "username": normalized_username,
            "device_id": None,
            "display_name": "Administrator",
        }
    customer = fetch_customer_account(normalized_username)
    if not customer or int(customer.get("active", 0)) != 1:
        return None
    if not check_password_hash(customer.get("password_hash", ""), password or ""):
        return None
    service_config = fetch_device_service_config(normalized_username, account=customer)
    return {
        "role": "customer",
        "username": normalized_username,
        "device_id": normalized_username,
        "display_name": customer.get("display_name") or normalized_username,
        "cloud_feed_enabled": service_config.get("cloud_feed_enabled", True),
        "cloud_feed_mode": service_config.get("cloud_feed_mode"),
        "ai_analysis_enabled": service_config.get("effective_ai_analysis_enabled", True),
    }



def issue_mobile_token(user):
    payload = {
        "role": user.get("role"),
        "username": user.get("username"),
        "device_id": normalize_device_id(user.get("device_id")),
    }
    return MOBILE_TOKEN_SERIALIZER.dumps(payload)


def resolve_mobile_user():
    cached = getattr(g, "mobile_user", None)
    if cached is not None:
        return cached

    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token:
        token = request.headers.get("X-Mobile-Token", "").strip()
    if not token:
        g.mobile_user = None
        return None

    try:
        payload = MOBILE_TOKEN_SERIALIZER.loads(token, max_age=MOBILE_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        g.mobile_user = None
        return None

    role = payload.get("role")
    username = str(payload.get("username") or "").strip()
    device_id = normalize_device_id(payload.get("device_id"))

    if role == "admin" and username == LOGIN_USERNAME:
        user = {
            "role": "admin",
            "username": username,
            "device_id": None,
            "display_name": "Administrator",
            "cloud_feed_enabled": True,
            "cloud_feed_mode": DEVICE_SERVICE_CLOUD_FEED_FULL,
            "ai_analysis_enabled": True,
        }
    elif role == "customer" and device_id:
        customer = fetch_customer_account(device_id)
        if not customer or int(customer.get("active", 0)) != 1:
            g.mobile_user = None
            return None
        service_config = fetch_device_service_config(device_id, account=customer)
        user = {
            "role": "customer",
            "username": device_id,
            "device_id": device_id,
            "display_name": customer.get("display_name") or device_id,
            "cloud_feed_enabled": service_config.get("cloud_feed_enabled", True),
            "cloud_feed_mode": service_config.get("cloud_feed_mode"),
            "ai_analysis_enabled": service_config.get("effective_ai_analysis_enabled", True),
        }
    else:
        g.mobile_user = None
        return None

    g.mobile_user = user
    return user


def mobile_auth_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = resolve_mobile_user()
        if not user:
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)

    return wrapped


def current_mobile_scope_device_id(requested_device_id=None):
    mobile_user = resolve_mobile_user()
    normalized_requested = normalize_device_id(requested_device_id)
    if not mobile_user or mobile_user.get("role") == "admin":
        return normalized_requested or None
    scoped_device_id = normalize_device_id(mobile_user.get("device_id"))
    if normalized_requested and scoped_device_id and normalized_requested != scoped_device_id:
        abort(403)
    return scoped_device_id or normalized_requested or None


def mobile_customer_cloud_feed_block_response():
    user = resolve_mobile_user()
    if not user or user.get("role") != "customer" or user.get("cloud_feed_enabled", True):
        return None
    return jsonify(
        {
            "error": customer_cloud_feed_error_message(),
            "cloud_feed_enabled": False,
        }
    ), 403


def mobile_customer_ai_analysis_block_response():
    user = resolve_mobile_user()
    if not user or user.get("role") != "customer" or user.get("ai_analysis_enabled", True):
        return None
    return jsonify(
        {
            "error": customer_ai_analysis_error_message(),
            "ai_analysis_enabled": False,
        }
    ), 403

def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_timestamp(value):
    if not value:
        return None

    if isinstance(value, datetime):
        return value

    text = str(value).replace("T", " ")

    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def format_timestamp(value):
    parsed = parse_timestamp(value)
    return parsed.strftime(TIMESTAMP_FORMAT) if parsed else None


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bool_flag(value):
    return str(value).upper() == "YES"


def append_reason(reasons, message):
    if message not in reasons:
        reasons.append(message)


def signal_quality(rssi):
    if rssi is None:
        return "Unknown"

    try:
        value = int(rssi)
    except (TypeError, ValueError):
        return "Unknown"

    if value >= -60:
        return "Excellent"
    if value >= -70:
        return "Good"
    if value >= -80:
        return "Fair"
    return "Poor"


def telemetry_status(seconds_since_sync):
    if seconds_since_sync is None:
        return "no-data"
    if seconds_since_sync <= 30:
        return "live"
    if seconds_since_sync <= STALE_AFTER_SECONDS:
        return "recent"
    return "stale"


def normalize_channel_mode(value):
    raw = str(value or "").strip().lower()
    if raw in {"both", "cloud", "local"}:
        return raw
    return "unknown"


def normalize_service_state(value):
    raw = str(value or "").strip().upper()
    if raw in {"ON", "OFF"}:
        return raw
    return "UNKNOWN"


def format_compact_uptime(seconds):
    value = safe_float(seconds, -1)
    if value < 0:
        return "--"
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def calculate_health(snapshot=None, leak_events=0, motor_cycles=0, consumption_rate=0):
    score = 100
    reasons = []

    if snapshot:
        if bool_flag(snapshot.get("pipe_leak")):
            score -= 25
            append_reason(reasons, "Pipe leak warning is active.")
        if bool_flag(snapshot.get("slow_leak")):
            score -= 12
            append_reason(reasons, "Slow leak pattern detected.")
        if bool_flag(snapshot.get("drip")):
            score -= 8
            append_reason(reasons, "Drip alert detected.")
        if bool_flag(snapshot.get("abnormal")):
            score -= 10
            append_reason(reasons, "Abnormal water usage was detected.")
        if bool_flag(snapshot.get("pump_failure")):
            score -= 20
            append_reason(reasons, "Pump failure warning is active.")
        if bool_flag(snapshot.get("dry_run")):
            score -= 18
            append_reason(reasons, "Dry-run protection was triggered.")
        if str(snapshot.get("sensor", "")).upper() != "OK":
            score -= 20
            append_reason(reasons, "Sensor needs attention.")
        wifi_status = str(snapshot.get("wifi", "")).upper()
        if wifi_status and wifi_status not in {"ONLINE", "OK", "CONNECTED"}:
            score -= 10
            append_reason(reasons, "Device connectivity is unstable.")
        rssi = snapshot.get("wifi_rssi")
        if rssi is not None and safe_float(rssi, -100) < -75:
            score -= 8
            append_reason(reasons, "Wi-Fi signal is weak.")
        seconds_since_sync = snapshot.get("seconds_since_sync")
        if seconds_since_sync is not None and seconds_since_sync > STALE_AFTER_SECONDS:
            score -= 15
            append_reason(reasons, "Telemetry is stale. Device has not updated recently.")
        level = safe_float(snapshot.get("level"), 0)
        if level <= 20:
            score -= 6
            append_reason(reasons, "Tank is below the low-level threshold.")

    if leak_events > 0:
        score -= min(20, leak_events * 4)
        append_reason(reasons, "Leak events were recorded in recent history.")

    if motor_cycles > 12:
        score -= min(12, motor_cycles - 12)
        append_reason(reasons, "Motor is cycling more often than normal.")

    if consumption_rate > 15:
        score -= 10
        append_reason(reasons, "Water consumption is higher than the expected range.")

    score = max(score, 0)

    if not reasons:
        reasons.append("All monitored systems are operating normally.")

    if score >= 85:
        status = "Healthy"
    elif score >= 65:
        status = "Watch"
    else:
        status = "Attention"

    return {
        "score": round(score, 1),
        "status": status,
        "reasons": reasons[:4]
    }


def fetch_latest_row(db):
    clause, params = device_source_where_clause()
    return db.execute(
        f"""
        SELECT *
        FROM tank_data
        WHERE {clause}
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def recent_counts(db, limit=200):
    clause, params = device_source_where_clause()
    motor_cycles = db.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT motor,
                   LAG(motor) OVER (ORDER BY id) AS prev_motor
            FROM (
                SELECT id, motor
                FROM tank_data
                WHERE {clause}
                ORDER BY created_at DESC, id DESC
                LIMIT {limit}
            )
            ORDER BY id
        )
        WHERE motor='ON' AND COALESCE(prev_motor,'OFF')!='ON'
        """,
        tuple(params),
    ).fetchone()[0]

    leak_events = db.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT pipe_leak,
                   LAG(pipe_leak) OVER (ORDER BY id) AS prev_pipe_leak
            FROM (
                SELECT id, pipe_leak
                FROM tank_data
                WHERE {clause}
                ORDER BY created_at DESC, id DESC
                LIMIT {limit}
            )
            ORDER BY id
        )
        WHERE pipe_leak='YES' AND COALESCE(prev_pipe_leak,'NO')!='YES'
        """,
        tuple(params),
    ).fetchone()[0]

    return motor_cycles, leak_events


def enrich_snapshot(data, motor_cycles=0, leak_events=0):
    created_at = parse_timestamp(data.get("created_at"))
    seconds_since_sync = None
    if created_at:
        seconds_since_sync = int((now_utc() - created_at).total_seconds())

    level = max(0.0, min(100.0, safe_float(data.get("level"), 0)))
    capacity_liters = safe_float(data.get("tank_capacity_liters"), TANK_CAPACITY_LITERS)
    if capacity_liters <= 0:
        capacity_liters = TANK_CAPACITY_LITERS
    liters = round((level / 100) * capacity_liters, 1)

    data["level"] = round(level, 2)
    mode = str(data.get("mode", "AUTO")).upper()
    data["mode"] = mode if mode in {"AUTO", "MANUAL"} else "AUTO"
    data.pop("simulator", None)
    data.pop("source_tank_simulator", None)
    data["device_source"] = normalize_device_source(data.get("device_source"), default=DEVICE_SOURCE_REAL)
    data["device_source_mode"] = get_device_source_mode()
    data["control_policy"] = CONTROL_POLICY
    data["capacity_liters"] = round(capacity_liters, 1)
    data["tank_capacity_liters"] = round(capacity_liters, 1)
    data["tank_height_cm"] = round(safe_float(data.get("tank_height_cm"), 0), 1)
    data["remaining_liters"] = liters
    data["water_available_label"] = f"{liters:.1f} L / {capacity_liters:.1f} L"
    lower_level_raw = data.get("lower_tank_level")
    lower_level = None
    if lower_level_raw not in (None, "", "null"):
        lower_level = max(0.0, min(100.0, safe_float(lower_level_raw, -1)))
        if lower_level < 0:
            lower_level = None
    data["lower_tank_level"] = round(lower_level, 2) if lower_level is not None else None
    lower_sensor = str(data.get("lower_sensor") or "").upper()
    if not lower_sensor:
        lower_sensor = "DISABLED" if lower_level is None else "UNKNOWN"
    data["lower_sensor"] = lower_sensor
    data["lower_sensor_info"] = data.get("lower_sensor_info") or (
        "Lower sensor disabled" if lower_sensor == "DISABLED" else "Lower sensor status unavailable"
    )
    lower_distance_raw = data.get("lower_sensor_distance_cm")
    data["lower_sensor_distance_cm"] = (
        round(safe_float(lower_distance_raw, 0), 1)
        if lower_distance_raw not in (None, "", "null") else None
    )
    if data["lower_tank_level"] is not None:
        lower_liters = round((data["lower_tank_level"] / 100) * capacity_liters, 1)
        data["lower_water_available_label"] = f"{lower_liters:.1f} L / {capacity_liters:.1f} L"
    else:
        data["lower_water_available_label"] = "--"
    data["last_sync_at"] = format_timestamp(created_at)
    data["seconds_since_sync"] = seconds_since_sync
    data["telemetry_status"] = telemetry_status(seconds_since_sync)
    data["signal_quality"] = signal_quality(data.get("wifi_rssi"))
    data["motor_cycles"] = motor_cycles
    data["leak_events"] = leak_events
    data["device_local_url"] = normalize_device_base_url(data.get("device_local_url"))
    data["channel_mode"] = normalize_channel_mode(data.get("channel_mode"))
    data["telemetry_service"] = normalize_service_state(data.get("telemetry_service"))
    data["command_service"] = normalize_service_state(data.get("command_service"))
    data["ota_service"] = normalize_service_state(data.get("ota_service"))
    data["lower_tank_service"] = normalize_service_state(data.get("lower_tank_service"))
    data["buzzer_service"] = normalize_service_state(data.get("buzzer_service"))
    data["led_display_service"] = normalize_service_state(data.get("led_display_service"))
    apply_source_tank_aliases(data, include_aliases=True)
    data["uptime_label"] = format_compact_uptime(data.get("uptime_s"))
    free_heap = data.get("free_heap")
    try:
        free_heap_value = int(free_heap) if free_heap is not None else None
    except (TypeError, ValueError):
        free_heap_value = None
    data["free_heap"] = free_heap_value
    data["free_heap_label"] = f"{free_heap_value} B" if free_heap_value is not None else "--"
    auto_status = str(data.get("auto_status") or "").strip()
    auto_status_tone = str(data.get("auto_status_tone") or "").strip().lower()
    auto_timer = str(data.get("auto_timer") or "").strip()
    if data["telemetry_status"] == "stale":
        synced_at = data["last_sync_at"] or "an earlier sync"
        auto_status = f"Telemetry is stale. Showing last synced state from {synced_at}."
        auto_status_tone = "warn"
        auto_timer = "Waiting for fresh telemetry."
    elif not auto_status:
        if data["mode"] == "MANUAL":
            auto_status = "Manual override is active on the device."
            auto_status_tone = "warn"
        elif str(data.get("motor", "")).upper() == "ON":
            auto_status = "Auto fill is running on the device."
            auto_status_tone = "ok"
        else:
            auto_status = "Auto is waiting for the next start condition."
            auto_status_tone = "info"

    if auto_status_tone not in {"ok", "warn", "bad", "info"}:
        auto_status_tone = "info"
    data["auto_status"] = auto_status
    data["auto_status_tone"] = auto_status_tone
    data["auto_timer"] = auto_timer

    health = calculate_health(
        snapshot=data,
        leak_events=leak_events,
        motor_cycles=motor_cycles
    )
    data["tank_health"] = health["score"]
    data["tank_health_status"] = health["status"]
    data["tank_health_reasons"] = health["reasons"]
    return data


def resolve_date_window():
    start_value = request.args.get("start_date")
    end_value = request.args.get("end_date")
    days = request.args.get("days", type=int)

    if start_value or end_value:
        try:
            start_dt = datetime.strptime(start_value, DATE_ONLY_FORMAT) if start_value else None
            end_dt = datetime.strptime(end_value, DATE_ONLY_FORMAT) if end_value else None
        except ValueError:
            raise ValueError("Dates must use YYYY-MM-DD format.")

        if start_dt is None and end_dt is not None:
            fallback_days = max(1, min(days or 7, 365))
            start_dt = end_dt - timedelta(days=fallback_days - 1)

        if end_dt is None and start_dt is not None:
            end_dt = now_utc()

        if start_dt is None or end_dt is None:
            raise ValueError("Both start and end dates are required.")

        if start_dt.date() > end_dt.date():
            raise ValueError("Start date must be before end date.")

        end_exclusive = end_dt + timedelta(days=1)
        label = f"{start_dt.date()} to {end_dt.date()}"
        return start_dt, end_exclusive, label

    days = max(1, min(days or 7, 365))
    end_exclusive = now_utc() + timedelta(seconds=1)
    start_dt = now_utc() - timedelta(days=days)
    label = f"Last {days} days"
    return start_dt, end_exclusive, label


def build_analytics_query(start_dt, end_exclusive, device_id=None):
    source_clause, source_params = device_source_where_clause()
    query = """
        SELECT level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
               pump_failure, dry_run, wifi, wifi_rssi, sensor, lower_tank_level,
               ai_usage_rate, tomorrow_prediction, created_at
        FROM tank_data
        WHERE created_at >= ? AND created_at < ?
          AND """
    query += source_clause
    params = [start_dt.strftime(TIMESTAMP_FORMAT), end_exclusive.strftime(TIMESTAMP_FORMAT), *source_params]
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        query += " AND device_id = ?"
        params.append(normalized_device_id)
    query += " ORDER BY created_at ASC, id ASC"
    return query, tuple(params)


def evenly_spaced_indices(item_count, sample_count):
    if item_count <= 0 or sample_count <= 0:
        return []
    if sample_count >= item_count:
        return list(range(item_count))
    if sample_count == 1:
        return [0]

    step = (item_count - 1) / float(sample_count - 1)
    selected = []
    last_index = -1
    for offset in range(sample_count):
        index = int(round(offset * step))
        if index <= last_index:
            index = min(item_count - 1, last_index + 1)
        selected.append(index)
        last_index = index
    return selected


def pick_series_indices(candidates, sample_count):
    if sample_count <= 0:
        return []
    if sample_count >= len(candidates):
        return list(candidates)
    return [candidates[index] for index in evenly_spaced_indices(len(candidates), sample_count)]


def downsample_series(time_values, value_values, max_points, preserve_nulls=False):
    safe_times = list(time_values or [])
    safe_values = list(value_values or [])
    size = min(len(safe_times), len(safe_values))
    if max_points <= 0 or size <= max_points:
        return safe_times[:size], safe_values[:size]

    mandatory = {0, size - 1}
    if preserve_nulls:
        mandatory.update(index for index, value in enumerate(safe_values[:size]) if value is None)

    if len(mandatory) >= max_points:
        final_indices = pick_series_indices(sorted(mandatory), max_points)
    else:
        remaining = [index for index in range(size) if index not in mandatory]
        final_indices = sorted(mandatory.union(pick_series_indices(remaining, max_points - len(mandatory))))

    return [safe_times[index] for index in final_indices], [safe_values[index] for index in final_indices]


def compact_motor_series(time_values, value_values):
    safe_times = list(time_values or [])
    safe_values = list(value_values or [])
    size = min(len(safe_times), len(safe_values))
    if size <= 0:
        return [], []

    compact_times = []
    compact_values = []
    last_state = None
    for index in range(size):
        raw_value = safe_values[index]
        if raw_value is None or raw_value == "":
            compact_times.append(safe_times[index])
            compact_values.append(None)
            last_state = None
            continue

        current_state = 1 if int(raw_value) == 1 else 0
        if last_state is None or current_state != last_state:
            compact_times.append(safe_times[index])
            compact_values.append(current_state)
            last_state = current_state

    tail_value = safe_values[size - 1]
    if tail_value is None or tail_value == "":
        normalized_tail = None
    else:
        normalized_tail = 1 if int(tail_value) == 1 else 0

    if compact_times[-1] != safe_times[size - 1]:
        compact_times.append(safe_times[size - 1])
        compact_values.append(normalized_tail)

    return compact_times, compact_values


def read_cached_analytics(cache_key, now_ts=None):
    if ANALYTICS_CACHE_TTL_SECONDS <= 0:
        return None

    current_time = time.time() if now_ts is None else now_ts
    expired_keys = [
        key
        for key, cached in analytics_cache.items()
        if current_time - float(cached.get("created_at") or 0.0) >= ANALYTICS_CACHE_TTL_SECONDS
    ]
    for key in expired_keys:
        analytics_cache.pop(key, None)

    cached = analytics_cache.get(cache_key)
    if not cached:
        return None
    if current_time - float(cached.get("created_at") or 0.0) >= ANALYTICS_CACHE_TTL_SECONDS:
        analytics_cache.pop(cache_key, None)
        return None
    return cached.get("payload")


def store_cached_analytics(cache_key, payload, now_ts=None):
    if ANALYTICS_CACHE_TTL_SECONDS <= 0:
        return payload

    current_time = time.time() if now_ts is None else now_ts
    analytics_cache.pop(cache_key, None)
    analytics_cache[cache_key] = {"created_at": current_time, "payload": payload}

    if len(analytics_cache) > ANALYTICS_CACHE_MAX_ENTRIES:
        overflow = len(analytics_cache) - ANALYTICS_CACHE_MAX_ENTRIES
        for key in list(analytics_cache)[:overflow]:
            analytics_cache.pop(key, None)
    return payload


def build_empty_analytics(start_dt, end_exclusive, label, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        snapshot = fetch_device_snapshot(normalized_device_id)
        motor_cycles = int(snapshot.get("motor_cycles", 0)) if snapshot else 0
        leak_events = int(snapshot.get("leak_events", 0)) if snapshot else 0
    else:
        with get_db() as db:
            row = fetch_latest_row(db)
            motor_cycles, leak_events = recent_counts(db)
        snapshot = enrich_snapshot(dict(row), motor_cycles, leak_events) if row else None

    snapshot = snapshot or {
        "capacity_liters": round(TANK_CAPACITY_LITERS, 1),
        "remaining_liters": 0,
        "tank_health": 100,
        "tank_health_status": "Healthy",
        "tank_health_reasons": ["No data has been received yet."],
        "device_id": normalized_device_id or None,
    }

    return {
        "range": {
            "label": label,
            "start_date": start_dt.strftime(DATE_ONLY_FORMAT),
            "end_date": (end_exclusive - timedelta(days=1)).strftime(DATE_ONLY_FORMAT),
        },
        "insights": {
            "avg_level": round(safe_float(snapshot.get("level"), 0), 2),
            "empty_prediction": None,
            "health": snapshot.get("tank_health", 100),
            "max_level": round(safe_float(snapshot.get("level"), 0), 2),
            "min_level": round(safe_float(snapshot.get("level"), 0), 2),
            "motor_cycles": int(motor_cycles),
            "consumption_rate": 0,
            "leak_events": int(leak_events),
            "avg_daily_usage": 0,
            "peak_usage_day": "--",
            "peak_usage_value": 0,
            "lowest_usage_day": "--",
            "lowest_usage_value": 0,
            "latest_day_usage": 0,
            "previous_day_usage": 0,
            "usage_change_pct": None
        },
        "health": {
            "score": snapshot.get("tank_health", 100),
            "status": snapshot.get("tank_health_status", "Healthy"),
            "reasons": snapshot.get("tank_health_reasons", ["No data has been received yet."])
        },
        "daily": {"dates": [], "values": []},
        "pattern": {"hours": list(range(24)), "values": [0] * 24},
        "levels": {"time": [], "values": []},
        "motor": {"time": [], "values": []},
        "comparison": {
            "latest_day": "--",
            "latest_day_usage": 0,
            "previous_day": "--",
            "previous_day_usage": 0,
            "change_pct": None
        },
        "prediction": {"tomorrow_usage": 0},
        "alerts": [f"No telemetry available for device {normalized_device_id} in the selected range."] if normalized_device_id else ["No telemetry available for the selected range."]
    }


def build_empty_snapshot_payload(device_id=None):
    payload = {
        "level": 0,
        "mode": "AUTO",
        "device_source": get_device_source_mode(),
        "device_source_mode": get_device_source_mode(),
        "control_policy": CONTROL_POLICY,
        "capacity_liters": round(TANK_CAPACITY_LITERS, 1),
        "remaining_liters": 0,
        "auto_status": "Waiting for device telemetry.",
        "auto_status_tone": "warn",
        "auto_timer": "Waiting for device telemetry.",
        "tank_health": 100,
        "tank_health_status": "Healthy",
        "tank_health_reasons": ["No telemetry received yet."],
        "telemetry_status": "no-data",
        "device_id": normalize_device_id(device_id) or None,
        "firmware_version": None,
        "reset_reason": None,
        "source_ip": None,
        "device_local_url": None,
        "channel_mode": "unknown",
        "telemetry_service": "UNKNOWN",
        "command_service": "UNKNOWN",
        "ota_service": "UNKNOWN",
        "lower_tank_service": "UNKNOWN",
        "buzzer_service": "UNKNOWN",
        "led_display_service": "UNKNOWN",
        "uptime_label": "--",
        "free_heap_label": "--",
        "lower_tank_level": None,
        "lower_sensor": "DISABLED",
        "lower_sensor_info": "Lower sensor disabled",
        "lower_sensor_distance_cm": None,
        "lower_water_available_label": "--",
    }
    return apply_source_tank_aliases(payload, include_aliases=True)


def load_dashboard_snapshot(device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    active_mode = get_device_source_mode()
    cache_key = f"{active_mode}:{normalized_device_id or '__latest__'}"
    if SNAPSHOT_CACHE_TTL_SECONDS > 0:
        cached = dashboard_snapshot_cache.get(cache_key)
        if cached and (time.time() - cached["created_at"] < SNAPSHOT_CACHE_TTL_SECONDS):
            return dict(cached["payload"])
    if normalized_device_id:
        snapshot = fetch_device_snapshot(normalized_device_id)
        payload = snapshot or build_empty_snapshot_payload(normalized_device_id)
    else:
        with get_db() as db:
            snapshot = latest_snapshot_with_metrics(db)
        payload = snapshot or build_empty_snapshot_payload()
    if SNAPSHOT_CACHE_TTL_SECONDS > 0:
        dashboard_snapshot_cache[cache_key] = {
            "created_at": time.time(),
            "payload": dict(payload),
        }
    return dict(payload)


def snapshot_has_live_device_data(snapshot):
    return bool(snapshot and snapshot.get("device_id"))


def build_system_status_payload(snapshot, device_id=None):
    device_state = device_status_from_snapshot(snapshot if snapshot_has_live_device_data(snapshot) else None)
    active_alerts = fetch_active_alerts(limit=6, device_id=device_id)

    return {
        "server": "online",
        "database": "online",
        "device": device_state["device"],
        "device_status_code": device_state["status_code"],
        "api_version": API_VERSION,
        "swt_version": SWT_VERSION,
        "control_policy": CONTROL_POLICY,
        "capacity_liters": round(TANK_CAPACITY_LITERS, 1),
        "last_sync_at": snapshot.get("last_sync_at") if snapshot else None,
        "seconds_since_sync": snapshot.get("seconds_since_sync") if snapshot else None,
        "telemetry_status": snapshot.get("telemetry_status") if snapshot else "no-data",
        "device_source": snapshot.get("device_source") if snapshot else get_device_source_mode(),
        "device_source_mode": get_device_source_mode(),
        "signal_quality": snapshot.get("signal_quality") if snapshot else "Unknown",
        "device_id": snapshot.get("device_id") if snapshot else None,
        "firmware_version": snapshot.get("firmware_version") if snapshot else None,
        "reset_reason": snapshot.get("reset_reason") if snapshot else None,
        "channel_mode": snapshot.get("channel_mode") if snapshot else "unknown",
        "telemetry_service": snapshot.get("telemetry_service") if snapshot else "UNKNOWN",
        "command_service": snapshot.get("command_service") if snapshot else "UNKNOWN",
        "ota_service": snapshot.get("ota_service") if snapshot else "UNKNOWN",
        "lower_tank_service": snapshot.get("lower_tank_service") if snapshot else "UNKNOWN",
        "buzzer_service": snapshot.get("buzzer_service") if snapshot else "UNKNOWN",
        "led_display_service": snapshot.get("led_display_service") if snapshot else "UNKNOWN",
        "uptime_label": snapshot.get("uptime_label") if snapshot else "--",
        "free_heap_label": snapshot.get("free_heap_label") if snapshot else "--",
        "active_alert_count": len(active_alerts),
        "active_alerts": active_alerts,
    }


def build_monitoring_summary_payload(snapshot, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    with get_db() as db:
        pending = db.execute("SELECT COUNT(*) FROM relay_queue").fetchone()[0]

    active_alerts = fetch_active_alerts(limit=20, device_id=normalized_device_id)
    registered_device_ids = (
        [normalized_device_id]
        if normalized_device_id
        else sorted(set(sorted(DEVICE_KEY_MAP.keys()) + list_registered_device_ids(limit=200)))
    )
    return {
        "api_version": API_VERSION,
        "swt_version": SWT_VERSION,
        "control_policy": CONTROL_POLICY,
        "device_auth_enabled": configured_device_auth_enabled(),
        "registered_devices": registered_device_ids,
        "latest_snapshot": {
            "device_id": snapshot.get("device_id") if snapshot else None,
            "device_source": snapshot.get("device_source") if snapshot else get_device_source_mode(),
            "device_source_mode": get_device_source_mode(),
            "firmware_version": snapshot.get("firmware_version") if snapshot else None,
            "reset_reason": snapshot.get("reset_reason") if snapshot else None,
            "last_sync_at": snapshot.get("last_sync_at") if snapshot else None,
            "seconds_since_sync": snapshot.get("seconds_since_sync") if snapshot else None,
            "telemetry_status": snapshot.get("telemetry_status") if snapshot else "no-data",
            "channel_mode": snapshot.get("channel_mode") if snapshot else "unknown",
            "telemetry_service": snapshot.get("telemetry_service") if snapshot else "UNKNOWN",
            "command_service": snapshot.get("command_service") if snapshot else "UNKNOWN",
            "ota_service": snapshot.get("ota_service") if snapshot else "UNKNOWN",
            "lower_tank_service": snapshot.get("lower_tank_service") if snapshot else "UNKNOWN",
            "buzzer_service": snapshot.get("buzzer_service") if snapshot else "UNKNOWN",
            "led_display_service": snapshot.get("led_display_service") if snapshot else "UNKNOWN",
            "wifi": snapshot.get("wifi") if snapshot else None,
            "wifi_rssi": snapshot.get("wifi_rssi") if snapshot else None,
            "sensor": snapshot.get("sensor") if snapshot else None,
            "motor": snapshot.get("motor") if snapshot else None,
            "mode": snapshot.get("mode") if snapshot else None,
        },
        "relay": {
            "enabled": bool(RELAY_STATUS_URL_LIST),
            "targets": [],
            "pending_count": pending,
            "last_success_at": relay_state.get("last_success_at"),
            "last_error_at": relay_state.get("last_error_at"),
            "last_error": relay_state.get("last_error"),
            "last_status_code": relay_state.get("last_status_code"),
        },
        "alerts": active_alerts,
        "devices": fetch_device_inventory(limit=20, device_ids=[normalized_device_id] if normalized_device_id else None),
    }


def build_ops_dashboard_payload(snapshot, device_id=None, alert_limit=8, audit_limit=6):
    normalized_device_id = normalize_device_id(device_id)
    return {
        "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=normalized_device_id),
        "alerts": fetch_filtered_alerts(limit=max(1, min(alert_limit, 50)), device_id=normalized_device_id),
        "audit": fetch_audit_events(limit=max(1, min(audit_limit, 30)), device_id=normalized_device_id),
        "generated_at": now_utc().strftime(TIMESTAMP_FORMAT),
    }


def build_db_summary_payload():
    file_sizes = collect_database_file_sizes()
    active_mode = get_device_source_mode()
    with get_db() as db:
        telemetry_row = db.execute(
            """
            SELECT COUNT(*) AS row_count, MAX(created_at) AS latest_created_at
            FROM tank_data
            """
        ).fetchone()
        command_row = db.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN delivered_at IS NULL THEN 1 ELSE 0 END) AS pending_row_count,
                MAX(created_at) AS latest_created_at
            FROM device_command_queue
            """
        ).fetchone()
        accounts_row = db.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                SUM(CASE WHEN active = 1 THEN 1 ELSE 0 END) AS active_row_count,
                MAX(updated_at) AS latest_updated_at
            FROM customer_accounts
            """
        ).fetchone()

    telemetry_rows = int((telemetry_row["row_count"] if telemetry_row else 0) or 0)
    graph_reason = None
    if not TELEMETRY_HISTORY_ENABLED:
        graph_reason = "Telemetry history is disabled on this deployment."
    elif telemetry_rows < 2:
        graph_reason = "At least two telemetry rows are needed before trend graphs can render."
    elif IS_RENDER and REQUIRE_RENDER_PERSISTENT_DB and not render_persistent_db_active():
        graph_reason = (
            "Render is not using /var/data/tank.db, so graph history can disappear after restarts."
        )

    return {
        "database": {
            "path": DB_FILE,
            "path_source": DB_PATH_SOURCE,
            "is_render": IS_RENDER,
            "persistent_db_expected": bool(IS_RENDER and REQUIRE_RENDER_PERSISTENT_DB),
            "persistent_db_active": render_persistent_db_active(),
            "rejected_paths": DB_PATH_REJECTED,
            "file_sizes_bytes": file_sizes,
        },
        "analytics": {
            "history_enabled": TELEMETRY_HISTORY_ENABLED,
            "device_source_mode": active_mode,
            "retention_days": DATA_RETENTION_DAYS,
            "max_rows_per_device": MAX_TELEMETRY_ROWS_PER_DEVICE,
            "graphs_ready": TELEMETRY_HISTORY_ENABLED and telemetry_rows >= 2,
            "reason": graph_reason,
        },
        "tables": {
            "tank_data": {
                "rows": telemetry_rows,
                "latest_created_at": telemetry_row["latest_created_at"] if telemetry_row else None,
            },
            "device_command_queue": {
                "rows": int((command_row["row_count"] if command_row else 0) or 0),
                "pending_rows": int((command_row["pending_row_count"] if command_row else 0) or 0),
                "latest_created_at": command_row["latest_created_at"] if command_row else None,
            },
            "customer_accounts": {
                "rows": int((accounts_row["row_count"] if accounts_row else 0) or 0),
                "active_rows": int((accounts_row["active_row_count"] if accounts_row else 0) or 0),
                "latest_updated_at": accounts_row["latest_updated_at"] if accounts_row else None,
            },
        },
        "maintenance": {
            "enabled": DB_MAINTENANCE_ENABLED,
            "target_size_mb": DB_TARGET_SIZE_MB,
            "target_size_bytes": DB_TARGET_SIZE_BYTES,
            "min_interval_seconds": DB_MAINTENANCE_MIN_INTERVAL_SECONDS,
            "wal_autocheckpoint_pages": DB_WAL_AUTOCHECKPOINT_PAGES,
            "temporary_size_guard_enabled": TEMP_DB_SIZE_GUARD_ENABLED,
            "temporary_hard_size_cap_enabled": TEMP_HARD_DB_CAP_ENABLED,
            "hard_size_cap_batch_rows": TEMP_HARD_DB_CAP_BATCH_ROWS,
            "hard_size_cap_max_batches": TEMP_HARD_DB_CAP_MAX_BATCHES,
            "device_command_retention_days": DEVICE_COMMAND_RETENTION_DAYS,
            "ops_alert_retention_days": OPS_ALERT_RETENTION_DAYS,
            "ops_audit_retention_days": OPS_AUDIT_RETENTION_DAYS,
            "last_run_at_epoch": db_maintenance_state.get("last_run_at"),
            "last_reason": db_maintenance_state.get("last_reason"),
            "last_error": db_maintenance_state.get("last_error"),
            "last_total_bytes": db_maintenance_state.get("last_total_bytes"),
            "last_skip_at_epoch": db_maintenance_state.get("last_skip_at"),
            "last_skip_reason": db_maintenance_state.get("last_skip_reason"),
            "last_size_cap_pruned_rows": db_prune_state.get("last_size_cap_rows"),
            "last_size_cap_batches": db_prune_state.get("last_size_cap_batches"),
            "last_size_cap_remaining_pressure": db_prune_state.get("last_size_cap_remaining_pressure"),
        },
    }


def build_analytics(start_dt, end_exclusive, label, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    active_mode = get_device_source_mode()
    cache_key = (
        start_dt.strftime(DATE_ONLY_FORMAT),
        end_exclusive.strftime(DATE_ONLY_FORMAT),
        normalized_device_id or "*",
        active_mode,
    )
    now_ts = time.time()
    cached_payload = read_cached_analytics(cache_key, now_ts=now_ts)
    if cached_payload is not None:
        return cached_payload

    if not TELEMETRY_HISTORY_ENABLED:
        payload = build_empty_analytics(start_dt, end_exclusive, label, normalized_device_id)
        payload["alerts"] = ["Analytics history is disabled on this deployment."]
        return store_cached_analytics(cache_key, payload, now_ts=now_ts)

    query, params = build_analytics_query(start_dt, end_exclusive, normalized_device_id)
    gap_threshold_hours = ANALYTICS_MAX_GAP_MINUTES / 60.0
    daily_usage = {}
    hourly_usage = [0.0] * 24
    level_times = []
    level_values = []
    motor_times = []
    motor_values = []
    row_count = 0
    level_total = 0.0
    level_min = None
    level_max = None
    motor_cycles = 0
    leak_events = 0
    total_usage = 0.0
    valid_hours = 0.0
    prev_created_at = None
    prev_level = None
    prev_motor = "OFF"
    latest_row = None

    with get_db() as db:
        for row in db.execute(query, params):
            created_at = parse_timestamp(row["created_at"])
            if created_at is None:
                continue

            row_count += 1
            timestamp_label = created_at.strftime(TIMESTAMP_FORMAT)
            level = safe_float(row["level"], 0.0)
            motor = str(row["motor"] or "").upper()
            pipe_leak = str(row["pipe_leak"] or "").upper()

            level_total += level
            level_min = level if level_min is None else min(level_min, level)
            level_max = level if level_max is None else max(level_max, level)

            delta_hours = 0.0
            gap_break = False
            if prev_created_at is not None:
                delta_hours = max(0.0, (created_at - prev_created_at).total_seconds() / 3600.0)
                gap_break = delta_hours > gap_threshold_hours

            drop = 0.0 if prev_level is None else level - prev_level
            valid_drop = (
                (not gap_break)
                and (drop < -0.05)
                and (abs(drop) <= ANALYTICS_MAX_LEVEL_DELTA_PCT)
            )
            usage = abs(drop) if valid_drop else 0.0
            total_usage += usage
            if not gap_break:
                valid_hours += delta_hours

            date_key = created_at.date().isoformat()
            daily_usage[date_key] = daily_usage.get(date_key, 0.0) + usage
            hourly_usage[created_at.hour] += usage

            level_times.append(timestamp_label)
            level_values.append(None if gap_break else level)
            motor_times.append(timestamp_label)
            motor_values.append(None if gap_break else (1 if motor == "ON" else 0))

            if motor == "ON" and prev_motor != "ON":
                motor_cycles += 1
            if pipe_leak == "YES":
                leak_events += 1

            latest_row = dict(row)
            latest_row["created_at"] = created_at
            latest_row["level"] = level
            latest_row["motor"] = motor
            latest_row["pipe_leak"] = pipe_leak

            prev_created_at = created_at
            prev_level = level
            prev_motor = motor

    if row_count < 2 or latest_row is None or prev_level is None:
        payload = build_empty_analytics(start_dt, end_exclusive, label, normalized_device_id)
        return store_cached_analytics(cache_key, payload, now_ts=now_ts)

    consumption_rate = total_usage / valid_hours if valid_hours > 0 else 0.0
    if consumption_rate < ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR:
        consumption_rate = 0.0
    current_level = float(prev_level)
    empty_prediction = current_level / consumption_rate if consumption_rate > 0 else None
    daily_dates = list(daily_usage.keys())
    daily_values = list(daily_usage.values())

    peak_day = max(daily_usage, key=daily_usage.get) if daily_usage else "--"
    lowest_day = min(daily_usage, key=daily_usage.get) if daily_usage else "--"
    peak_value = float(daily_usage.get(peak_day, 0.0)) if daily_usage else 0.0
    lowest_value = float(daily_usage.get(lowest_day, 0.0)) if daily_usage else 0.0
    avg_daily_usage = float(sum(daily_values) / len(daily_values)) if daily_values else 0.0

    latest_day = str(daily_dates[-1]) if daily_dates else "--"
    previous_day = str(daily_dates[-2]) if len(daily_dates) >= 2 else "--"
    latest_day_usage = float(daily_values[-1]) if daily_values else 0.0
    previous_day_usage = float(daily_values[-2]) if len(daily_values) >= 2 else 0.0
    if previous_day_usage >= ANALYTICS_MIN_BASELINE_USAGE_PCT:
        usage_change_pct = ((latest_day_usage - previous_day_usage) / previous_day_usage) * 100
    else:
        usage_change_pct = None

    latest_row["seconds_since_sync"] = max(0, int((now_utc() - latest_row["created_at"]).total_seconds()))
    health = calculate_health(
        snapshot=latest_row,
        leak_events=leak_events,
        motor_cycles=motor_cycles,
        consumption_rate=consumption_rate,
    )

    alerts = []
    if leak_events > 0:
        alerts.append("Possible pipe leak detected in the selected period.")
    if consumption_rate > 15:
        alerts.append("Water consumption is above the usual range.")
    if empty_prediction is not None and empty_prediction < 6:
        alerts.append("Tank may empty within the next 6 hours.")
    if motor_cycles > 12:
        alerts.append("Motor is cycling frequently. Check automation thresholds.")
    if latest_row["seconds_since_sync"] > STALE_AFTER_SECONDS:
        alerts.append("Live telemetry looks stale. Check device connectivity.")
    if not alerts:
        alerts.append("System is stable for the selected range.")

    level_times, level_values = downsample_series(
        level_times,
        level_values,
        ANALYTICS_LEVEL_SERIES_MAX_POINTS,
        preserve_nulls=True,
    )
    motor_times, motor_values = compact_motor_series(motor_times, motor_values)
    motor_times, motor_values = downsample_series(
        motor_times,
        motor_values,
        ANALYTICS_MOTOR_SERIES_MAX_POINTS,
        preserve_nulls=True,
    )

    payload = {
        "range": {
            "label": label,
            "start_date": start_dt.strftime(DATE_ONLY_FORMAT),
            "end_date": (end_exclusive - timedelta(days=1)).strftime(DATE_ONLY_FORMAT),
        },
        "insights": {
            "avg_level": round(level_total / row_count, 2),
            "empty_prediction": round(float(empty_prediction), 2) if empty_prediction is not None else None,
            "health": health["score"],
            "max_level": round(float(level_max if level_max is not None else 0.0), 2),
            "min_level": round(float(level_min if level_min is not None else 0.0), 2),
            "motor_cycles": motor_cycles,
            "consumption_rate": round(float(consumption_rate), 2),
            "leak_events": leak_events,
            "avg_daily_usage": round(avg_daily_usage, 2),
            "peak_usage_day": peak_day,
            "peak_usage_value": round(peak_value, 2),
            "lowest_usage_day": lowest_day,
            "lowest_usage_value": round(lowest_value, 2),
            "latest_day_usage": round(latest_day_usage, 2),
            "previous_day_usage": round(previous_day_usage, 2),
            "usage_change_pct": round(float(usage_change_pct), 2) if usage_change_pct is not None else None,
        },
        "health": health,
        "daily": {
            "dates": daily_dates,
            "values": [round(float(value), 2) for value in daily_values],
        },
        "pattern": {
            "hours": list(range(24)),
            "values": [round(float(value), 2) for value in hourly_usage],
        },
        "levels": {
            "time": level_times,
            "values": [round(float(value), 2) if value is not None else None for value in level_values],
        },
        "motor": {
            "time": motor_times,
            "values": [int(value) if value is not None else None for value in motor_values],
        },
        "comparison": {
            "latest_day": latest_day,
            "latest_day_usage": round(latest_day_usage, 2),
            "previous_day": previous_day,
            "previous_day_usage": round(previous_day_usage, 2),
            "change_pct": round(float(usage_change_pct), 2) if usage_change_pct is not None else None,
        },
        "prediction": {
            "tomorrow_usage": round(avg_daily_usage * 1.05, 2),
        },
        "alerts": alerts,
    }

    return store_cached_analytics(cache_key, payload, now_ts=now_ts)


def analytics_csv_filename_token(value, fallback):
    cleaned = "".join(ch.lower() if str(ch).isalnum() else "-" for ch in str(value or ""))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or fallback


def build_analytics_csv_filename(payload, device_id=None):
    range_info = payload.get("range") or {}
    device_token = analytics_csv_filename_token(device_id, "all-devices")
    start_token = analytics_csv_filename_token(range_info.get("start_date"), now_utc().strftime(DATE_ONLY_FORMAT))
    end_token = analytics_csv_filename_token(range_info.get("end_date"), start_token)
    return f"smart-water-tank-analytics-{device_token}-{start_token}-to-{end_token}.csv"


def format_analytics_csv_value(value, digits=2):
    if value is None or value == "":
        return ""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def build_analytics_csv_rows(payload, device_id=None):
    range_info = payload.get("range") or {}
    shared = {
        "device_id": normalize_device_id(device_id) or "",
        "range_label": str(range_info.get("label") or ""),
        "start_date": str(range_info.get("start_date") or ""),
        "end_date": str(range_info.get("end_date") or ""),
    }
    rows = []

    def append_series(report_type, report_title, x_values, y_values, unit, label_builder=None):
        safe_x = list(x_values or [])
        safe_y = list(y_values or [])
        row_count = min(len(safe_x), len(safe_y))
        if row_count <= 0:
            rows.append(
                {
                    **shared,
                    "report_type": report_type,
                    "report_title": report_title,
                    "row_index": 1,
                    "x_value": "",
                    "y_value": "",
                    "value_label": "No data",
                    "unit": unit,
                }
            )
            return

        for index, (x_value, y_value) in enumerate(zip(safe_x[:row_count], safe_y[:row_count]), start=1):
            rows.append(
                {
                    **shared,
                    "report_type": report_type,
                    "report_title": report_title,
                    "row_index": index,
                    "x_value": x_value,
                    "y_value": "" if y_value is None else y_value,
                    "value_label": (
                        label_builder(x_value, y_value)
                        if callable(label_builder)
                        else format_analytics_csv_value(y_value)
                    ),
                    "unit": unit,
                }
            )

    append_series(
        "tank_level_history",
        "Tank Level History",
        (payload.get("levels") or {}).get("time") or [],
        (payload.get("levels") or {}).get("values") or [],
        "percent",
        label_builder=lambda _x, y: format_analytics_csv_value(y),
    )
    append_series(
        "daily_water_use",
        "Daily Water Use",
        (payload.get("daily") or {}).get("dates") or [],
        (payload.get("daily") or {}).get("values") or [],
        "percent",
        label_builder=lambda _x, y: format_analytics_csv_value(y),
    )
    append_series(
        "hourly_water_pattern",
        "24-Hour Water Pattern",
        [
            f"{int(hour):02d}:00" if str(hour).strip() not in {"", "None"} else ""
            for hour in ((payload.get("pattern") or {}).get("hours") or [])
        ],
        (payload.get("pattern") or {}).get("values") or [],
        "percent",
        label_builder=lambda _x, y: format_analytics_csv_value(y),
    )
    append_series(
        "pump_activity",
        "Pump Activity",
        (payload.get("motor") or {}).get("time") or [],
        (payload.get("motor") or {}).get("values") or [],
        "state",
        label_builder=lambda _x, y: "ON" if str(y) == "1" else "OFF" if str(y) == "0" else "",
    )
    return rows


def build_analytics_csv_payload(payload, device_id=None):
    fieldnames = [
        "report_type",
        "report_title",
        "device_id",
        "range_label",
        "start_date",
        "end_date",
        "row_index",
        "x_value",
        "y_value",
        "value_label",
        "unit",
    ]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in build_analytics_csv_rows(payload, device_id=device_id):
        writer.writerow(row)
    return buffer.getvalue()


def device_status_from_snapshot(snapshot):
    if not snapshot:
        return {
            "device": "offline",
            "status_code": None,
            "source": "last-sync"
        }

    telemetry = snapshot.get("telemetry_status")
    device = "online" if telemetry in {"live", "recent"} else "offline"

    return {
        "device": device,
        "status_code": None,
        "source": "last-sync"
    }


def build_events(limit=12, device_id=None):
    if not TELEMETRY_HISTORY_ENABLED:
        return []

    normalized_device_id = normalize_device_id(device_id)
    source_clause, source_params = device_source_where_clause()
    query = """
        SELECT id, level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
               pump_failure, dry_run, sensor, wifi, created_at
        FROM tank_data
        WHERE 
    """
    query += source_clause
    params = list(source_params)
    if normalized_device_id:
        query += " AND device_id = ?"
        params.append(normalized_device_id)
    query += " ORDER BY created_at DESC, id DESC LIMIT 240"
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    if not rows:
        return []

    timeline = []
    previous = None
    for row in reversed(rows):
        current = dict(row)
        timestamp = format_timestamp(current.get("created_at"))
        level = safe_float(current.get("level"), 0)

        if previous is None or current.get("motor") != previous.get("motor"):
            if current.get("motor") == "ON":
                timeline.append({"time": timestamp, "severity": "info", "message": "Motor started."})
            elif previous and previous.get("motor") == "ON":
                timeline.append({"time": timestamp, "severity": "info", "message": "Motor stopped."})

        if previous is None or current.get("mode") != previous.get("mode"):
            timeline.append({"time": timestamp, "severity": "info", "message": f"Mode changed to {current.get('mode', '--')}."})

        if level <= 20 and (previous is None or safe_float(previous.get("level"), 100) > 20):
            timeline.append({"time": timestamp, "severity": "warning", "message": "Tank dropped below 20%."})

        if level >= 95 and (previous is None or safe_float(previous.get("level"), 0) < 95):
            timeline.append({"time": timestamp, "severity": "success", "message": "Tank reached near-full level."})

        for key, message in (
            ("pipe_leak", "Pipe leak warning detected."),
            ("slow_leak", "Slow leak pattern detected."),
            ("drip", "Drip alert detected."),
            ("abnormal", "Abnormal usage detected."),
            ("pump_failure", "Pump failure warning detected."),
            ("dry_run", "Dry-run protection activated."),
        ):
            if bool_flag(current.get(key)) and (previous is None or not bool_flag(previous.get(key))):
                timeline.append({"time": timestamp, "severity": "warning", "message": message})

        if str(current.get("sensor", "")).upper() != "OK" and (previous is None or str(previous.get("sensor", "")).upper() == "OK"):
            timeline.append({"time": timestamp, "severity": "warning", "message": "Sensor requires attention."})

        if str(current.get("wifi", "")).upper() not in {"", "ONLINE", "OK", "CONNECTED"} and (
            previous is None or str(previous.get("wifi", "")).upper() in {"", "ONLINE", "OK", "CONNECTED"}
        ):
            timeline.append({"time": timestamp, "severity": "warning", "message": "Device connectivity issue detected."})

        previous = current

    return timeline[-limit:][::-1]


def send_alert_webhook(payload):
    if ALERT_WEBHOOK_URL:
        try:
            requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=(3, 8))
        except requests.RequestException as exc:
            logger.warning("Generic alert webhook failed: %s", exc)

    if SLACK_WEBHOOK_URL:
        try:
            text = f"[{payload.get('severity', 'info').upper()}] {payload.get('kind', 'alert')}: {payload.get('message', '')}"
            if payload.get("device_id"):
                text += f" (device {payload['device_id']})"
            requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=(3, 8))
        except requests.RequestException as exc:
            logger.warning("Slack alert webhook failed: %s", exc)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            text = f"*Smart Water Tank Alert*\nSeverity: {payload.get('severity', 'info')}\nKind: {payload.get('kind', 'alert')}\nMessage: {payload.get('message', '')}"
            if payload.get("device_id"):
                text += f"\nDevice: {payload['device_id']}"
            requests.post(
                telegram_url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
                timeout=(3, 8),
            )
        except requests.RequestException as exc:
            logger.warning("Telegram alert delivery failed: %s", exc)

    if WHATSAPP_WEBHOOK_URL:
        try:
            requests.post(
                WHATSAPP_WEBHOOK_URL,
                json={
                    "channel": "whatsapp",
                    "title": "Smart Water Tank Alert",
                    "severity": payload.get("severity", "info"),
                    "kind": payload.get("kind", "alert"),
                    "message": payload.get("message", ""),
                    "device_id": payload.get("device_id"),
                },
                timeout=(3, 8),
            )
        except requests.RequestException as exc:
            logger.warning("WhatsApp-style alert webhook failed: %s", exc)


def log_audit_event(actor, action, target_type, target_id=None, device_id=None, details=None):
    details_json = json.dumps(details, separators=(",", ":")) if isinstance(details, (dict, list)) else details
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO ops_audit_log (actor, action, target_type, target_id, device_id, details)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (actor, action, target_type, target_id, device_id, details_json),
        )
    return cursor.lastrowid


def fetch_audit_events(limit=30, device_id=None):
    query = """
        SELECT id, actor, action, target_type, target_id, device_id, details, created_at
        FROM ops_audit_log
    """
    params = []
    if device_id:
        query += " WHERE COALESCE(device_id, '') = COALESCE(?, '')"
        params.append(device_id)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    events = []
    for row in rows:
        item = dict(row)
        details = item.get("details")
        if details:
            try:
                item["details"] = json.loads(details)
            except (TypeError, ValueError):
                pass
        events.append(item)
    return events


def set_alert(kind, severity, message, device_id=None, active=True, best_effort=False):
    normalized_device_id = normalize_device_id(device_id)
    if should_skip_alert_touch(
        kind,
        severity,
        message,
        device_id=normalized_device_id,
        active=active,
    ):
        return

    webhook_payload = None
    try:
        with get_db() as db:
            existing = db.execute(
                """
                SELECT id, active, message, severity
                FROM ops_alerts
                WHERE kind = ? AND COALESCE(device_id, '') = COALESCE(?, '')
                ORDER BY id DESC
                LIMIT 1
                """,
                (kind, normalized_device_id),
            ).fetchone()

            if active:
                if existing and int(existing["active"]) == 1 and existing["message"] == message and existing["severity"] == severity:
                    db.execute(
                        "UPDATE ops_alerts SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (existing["id"],),
                    )
                    note_alert_touch(kind, severity, message, device_id=normalized_device_id, active=active)
                    return

                if existing and int(existing["active"]) == 1:
                    db.execute(
                        """
                        UPDATE ops_alerts
                        SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (existing["id"],),
                    )

                cursor = db.execute(
                    """
                    INSERT INTO ops_alerts (device_id, kind, severity, message, active)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (normalized_device_id, kind, severity, message),
                )
                webhook_payload = {
                    "id": cursor.lastrowid,
                    "device_id": normalized_device_id,
                    "kind": kind,
                    "severity": severity,
                    "message": message,
                    "active": True,
                }
            elif existing and int(existing["active"]) == 1:
                db.execute(
                    """
                    UPDATE ops_alerts
                    SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (existing["id"],),
                )
    except sqlite3.OperationalError as exc:
        if best_effort and database_is_locked_error(exc):
            note_alert_touch(kind, severity, message, device_id=normalized_device_id, active=active)
            logger.warning(
                "Skipping alert bookkeeping for %s/%s because the SQLite database is busy.",
                str(kind or "").strip() or "alert",
                normalized_device_id or "global",
            )
            return
        raise

    note_alert_touch(kind, severity, message, device_id=normalized_device_id, active=active)
    if webhook_payload:
        send_alert_webhook(webhook_payload)


def evaluate_snapshot_alerts(snapshot):
    if not snapshot:
        set_alert("telemetry_stale", "danger", "No telemetry has been received yet.", active=True, best_effort=True)
        return

    device_id = snapshot.get("device_id")
    stale = snapshot.get("telemetry_status") in {"stale", "offline"}
    set_alert(
        "telemetry_stale",
        "danger",
        f"Telemetry is stale for device {device_id or 'unknown device'}.",
        device_id=device_id,
        active=stale,
        best_effort=True,
    )
    set_alert(
        "pump_failure",
        "danger",
        "Pump failure reported by firmware.",
        device_id=device_id,
        active=bool_flag(snapshot.get("pump_failure")),
        best_effort=True,
    )
    set_alert(
        "dry_run",
        "danger",
        "Dry-run protection triggered.",
        device_id=device_id,
        active=bool_flag(snapshot.get("dry_run")),
        best_effort=True,
    )
    leak_active = any(bool_flag(snapshot.get(key)) for key in ("leak", "drip", "slow_leak", "pipe_leak"))
    set_alert(
        "leak",
        "warning",
        "Leak-related alert reported by firmware.",
        device_id=device_id,
        active=leak_active,
        best_effort=True,
    )
    sensor_bad = str(snapshot.get("sensor", "")).upper() not in {"OK", ""}
    set_alert(
        "sensor_fault",
        "warning",
        "Main tank sensor needs attention.",
        device_id=device_id,
        active=sensor_bad,
        best_effort=True,
    )


def fetch_active_alerts(limit=20, device_id=None):
    query = """
        SELECT id, device_id, kind, severity, message, created_at, updated_at
        FROM ops_alerts
        WHERE active = 1
    """
    params = []
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        query += " AND COALESCE(device_id, '') = COALESCE(?, '')"
        params.append(normalized_device_id)
    query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def fetch_filtered_alerts(limit=20, severity=None, device_id=None):
    query = """
        SELECT id, device_id, kind, severity, message, created_at, updated_at
        FROM ops_alerts
        WHERE active = 1
    """
    params = []
    if severity:
        query += " AND LOWER(severity) = ?"
        params.append(str(severity).lower())
    if device_id:
        query += " AND COALESCE(device_id, '') = COALESCE(?, '')"
        params.append(device_id)
    query += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def resolve_alert_by_id(alert_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT id, active, device_id, kind, severity, message
            FROM ops_alerts
            WHERE id = ?
            """,
            (alert_id,),
        ).fetchone()
        if not row:
            return None
        if int(row["active"]) == 1:
            db.execute(
                """
                UPDATE ops_alerts
                SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (alert_id,),
            )
    return dict(row)


def fetch_device_inventory(limit=20, device_ids=None):
    normalized_device_ids = [item for item in (normalize_device_id(value) for value in (device_ids or [])) if item]
    source_clause, source_params = device_source_where_clause()
    query = """
        SELECT *
        FROM tank_data
        WHERE id IN (
            SELECT MAX(id)
            FROM tank_data
            WHERE 
    """
    query += source_clause
    query += """
            GROUP BY COALESCE(device_id, '')
        )
    """
    params = list(source_params)
    if normalized_device_ids:
        placeholders = ",".join("?" for _ in normalized_device_ids)
        query += f" AND COALESCE(device_id, '') IN ({placeholders})"
        params.extend(normalized_device_ids)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    inventory = []
    for row in rows:
        snapshot = enrich_snapshot(dict(row))
        inventory.append(build_admin_device_entry(snapshot.get("device_id") or "unassigned", snapshot=snapshot))
    return inventory


def fetch_device_snapshot(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    source_clause, source_params = device_source_where_clause()
    with get_db() as db:
        row = db.execute(
            f"""
            SELECT *
            FROM tank_data
            WHERE device_id = ?
              AND {source_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id, *source_params),
        ).fetchone()
        if not row:
            return None
        motor_cycles = db.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT motor,
                       LAG(motor) OVER (ORDER BY id) AS prev_motor
                FROM (
                    SELECT id, motor
                    FROM tank_data
                    WHERE device_id = ?
                      AND {source_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                )
                ORDER BY id
            )
            WHERE motor='ON' AND COALESCE(prev_motor,'OFF')!='ON'
            """,
            (normalized_device_id, *source_params),
        ).fetchone()[0]
        leak_events = db.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT pipe_leak,
                       LAG(pipe_leak) OVER (ORDER BY id) AS prev_pipe_leak
                FROM (
                    SELECT id, pipe_leak
                    FROM tank_data
                    WHERE device_id = ?
                      AND {source_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                )
                ORDER BY id
            )
            WHERE pipe_leak='YES' AND COALESCE(prev_pipe_leak,'NO')!='YES'
            """,
            (normalized_device_id, *source_params),
        ).fetchone()[0]
    return enrich_snapshot(dict(row), motor_cycles, leak_events)


def fetch_device_history(device_id, limit=48):
    if not TELEMETRY_HISTORY_ENABLED:
        return []

    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []
    source_clause, source_params = device_source_where_clause()
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT level, lower_tank_level, motor, sensor, wifi_rssi, created_at
            FROM tank_data
            WHERE device_id = ?
              AND {source_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (normalized_device_id, *source_params, limit),
        ).fetchall()
    history = []
    for row in reversed(rows):
        history.append(
            {
                "time": format_timestamp(row["created_at"]),
                "level": row["level"],
                "lower_tank_level": row["lower_tank_level"],
                "source_tank_level": row["lower_tank_level"],
                "motor": row["motor"],
                "sensor": row["sensor"],
                "wifi_rssi": row["wifi_rssi"],
            }
        )
    return history


def latest_device_id():
    source_clause, source_params = device_source_where_clause()
    with get_db() as db:
        row = db.execute(
            f"""
            SELECT device_id
            FROM tank_data
            WHERE device_id IS NOT NULL AND device_id != ''
              AND {source_clause}
            ORDER BY id DESC
            LIMIT 1
            """,
            tuple(source_params),
        ).fetchone()
    return row["device_id"] if row else None


def normalize_device_base_url(value):
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return f"{parsed.scheme}://{parsed.netloc}"


def is_loopback_device_target(value):
    text = str(value or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = f"http://{text}"

    parsed = urlparse(text)
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost", "::1"}:
        return True
    if not host:
        return False

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_literal_ip_device_target(value):
    text = str(value or "").strip()
    if not text:
        return False
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    host = (parsed.hostname or "").strip()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def build_device_update_url(device_id=None):
    snapshot = fetch_device_snapshot(device_id) if device_id else None
    candidates = []

    if snapshot and snapshot.get("device_local_url") and not is_loopback_device_target(snapshot.get("device_local_url")):
        candidates.append(snapshot.get("device_local_url"))

    if snapshot and snapshot.get("source_ip") and not is_loopback_device_target(snapshot.get("source_ip")) and not is_literal_ip_device_target(snapshot.get("source_ip")):
        candidates.append(snapshot.get("source_ip"))

    if DEVICE and not is_loopback_device_target(DEVICE):
        candidates.append(DEVICE)

    for candidate in candidates:
        base_url = normalize_device_base_url(candidate)
        if base_url:
            return f"{base_url.rstrip('/')}/update"

    return None


def resolve_command_target(target_device=None):
    normalized_target = normalize_device_id(target_device)
    if normalized_target:
        return normalized_target

    latest_device = latest_device_id()
    if latest_device:
        return latest_device

    if len(DEVICE_KEY_MAP) == 1:
        return next(iter(DEVICE_KEY_MAP))

    return None


def queue_device_command(command, target_device):
    with get_db() as db:
        db.execute(
            """
            DELETE FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL
            """,
            (target_device,),
        )
        db.execute(
            """
            INSERT INTO device_command_queue (target_device, command)
            VALUES (?, ?)
            """,
            (target_device, command),
        )
        db.execute(
            """
            DELETE FROM device_command_queue
            WHERE delivered_at IS NOT NULL
              AND delivered_at < datetime('now', '-7 day')
            """
        )


def peek_queued_command(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    with get_db() as db:
        row = db.execute(
            """
            SELECT id, command
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
        if not row:
            return None
    return {"id": row["id"], "command": row["command"]}


def acknowledge_queued_command_id(device_id, command_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return False

    try:
        normalized_command_id = int(command_id)
    except (TypeError, ValueError):
        return False

    if normalized_command_id <= 0:
        return False

    with get_db() as db:
        row = db.execute(
            """
            SELECT id
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL AND id = ?
            LIMIT 1
            """,
            (normalized_device_id, normalized_command_id),
        ).fetchone()
        if not row:
            return False
        db.execute(
            """
            UPDATE device_command_queue
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )
    return True


def acknowledge_queued_command(device_id, command):
    normalized_device_id = normalize_device_id(device_id)
    normalized_command = str(command or "").strip().upper()
    if not normalized_device_id or not normalized_command:
        return False

    with get_db() as db:
        row = db.execute(
            """
            SELECT id
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL AND UPPER(command) = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (normalized_device_id, normalized_command),
        ).fetchone()
        if not row:
            return False
        db.execute(
            """
            UPDATE device_command_queue
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )
    return True


def publish_mqtt_command(command, target_device, clear=False):
    if not mqtt_feature_enabled():
        return False
    topic = mqtt_command_topic(target_device)
    payload = "" if clear else str(command or "").strip().upper()
    with mqtt_lock:
        if not mqtt_client or not mqtt_state["connected"]:
            return False
        info = mqtt_client.publish(topic, payload=payload, qos=MQTT_QOS, retain=MQTT_COMMAND_RETAIN)
    try:
        info.wait_for_publish(timeout=2.0)
    except TypeError:
        pass
    return getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS) == mqtt.MQTT_ERR_SUCCESS


def clear_mqtt_command(target_device):
    publish_mqtt_command("", target_device, clear=True)


def queue_command(command, target_device=None):
    device_command_target = resolve_command_target(target_device)
    if not device_command_target:
        return {
            "status": "error",
            "error": "No target device is available for this command yet.",
            "command": command,
            "control_policy": CONTROL_POLICY,
        }, 400

    normalized_command = str(command or "").strip().upper()
    queue_device_command(normalized_command, device_command_target)
    mqtt_published = publish_mqtt_command(normalized_command, device_command_target)
    result = {
        "status": "queued",
        "command": normalized_command,
        "target_device": device_command_target,
        "queued_at": now_utc().strftime(TIMESTAMP_FORMAT),
        "control_policy": CONTROL_POLICY,
        "mqtt_delivery": "published" if mqtt_published else ("pending" if mqtt_feature_enabled() else "disabled"),
    }
    return result


def relay_status_to_cloud(payload):
    if not RELAY_STATUS_URL_LIST:
        return "ok"
    cleaned = sanitize_payload(payload)
    device_id = normalize_device_id(cleaned.get("device_id"))
    headers = relay_headers_for_device(device_id)
    if not device_id:
        logger.warning("Dropping relay payload without device_id")
        set_alert("relay_failure", "warning", "Cloud relay skipped a payload without device credentials.", active=True)
        return "drop"
    if "X-Device-Id" not in headers or "X-Device-Key" not in headers:
        logger.warning("Dropping relay payload for %s because no relay device key is configured", device_id)
        set_alert(
            "relay_failure",
            "warning",
            f"Cloud relay skipped device {device_id} because no relay credentials are configured.",
            device_id=device_id,
            active=True,
        )
        return "drop"
    for url in RELAY_STATUS_URL_LIST:
        for attempt in range(2):
            try:
                response = requests.post(
                    url,
                    json=cleaned,
                    headers=headers,
                    timeout=(RELAY_CONNECT_TIMEOUT_SEC, RELAY_TIMEOUT_SEC),
                    verify=RELAY_VERIFY_TLS,
                )
                if response.ok:
                    logger.info("Relayed telemetry to %s", url)
                    relay_state["last_success_at"] = now_utc().strftime(TIMESTAMP_FORMAT)
                    relay_state["last_status_code"] = response.status_code
                    relay_state["last_error_at"] = None
                    relay_state["last_error"] = None
                    set_alert("relay_failure", "warning", "Cloud relay is failing.", active=False)
                    return "ok"
                relay_state["last_error_at"] = now_utc().strftime(TIMESTAMP_FORMAT)
                relay_state["last_error"] = f"HTTP {response.status_code}"
                relay_state["last_status_code"] = response.status_code
                logger.warning("Relay telemetry failed (%s): %s", url, response.status_code)
                if response.status_code in {401, 403}:
                    set_alert(
                        "relay_failure",
                        "warning",
                        f"Cloud relay authentication failed for device {device_id} with HTTP {response.status_code}.",
                        device_id=device_id,
                        active=True,
                    )
                    return "drop"
                set_alert("relay_failure", "warning", f"Cloud relay returned HTTP {response.status_code}.", active=True)
            except requests.RequestException as exc:
                relay_state["last_error_at"] = now_utc().strftime(TIMESTAMP_FORMAT)
                relay_state["last_error"] = str(exc)
                relay_state["last_status_code"] = None
                logger.warning("Relay telemetry failed (%s): %s", url, exc)
                set_alert("relay_failure", "warning", f"Cloud relay request failed: {exc}", active=True)
    return "retry"


def enqueue_relay_payload(payload):
    if not RELAY_STATUS_URL_LIST:
        return
    cleaned = sanitize_payload(payload)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO relay_queue (payload, next_attempt_at)
            VALUES (?, CURRENT_TIMESTAMP)
            """,
            (json.dumps(cleaned, separators=(",", ":")),)
        )


def relay_backoff_seconds(attempts):
    return min(300, max(15, attempts * 30))


def drain_relay_queue(max_items=3):
    if not RELAY_STATUS_URL_LIST:
        return

    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, payload, attempts
            FROM relay_queue
            WHERE next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP
            ORDER BY id ASC
            LIMIT ?
            """,
            (max_items,)
        ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except Exception as exc:
            logger.warning("Dropping invalid relay payload %s: %s", row["id"], exc)
            with get_db() as db:
                db.execute("DELETE FROM relay_queue WHERE id = ?", (row["id"],))
            continue

        relay_result = relay_status_to_cloud(payload)
        if relay_result == "ok":
            with get_db() as db:
                db.execute("DELETE FROM relay_queue WHERE id = ?", (row["id"],))
            continue

        if relay_result == "drop":
            logger.warning("Dropping relay payload %s after permanent relay failure", row["id"])
            with get_db() as db:
                db.execute("DELETE FROM relay_queue WHERE id = ?", (row["id"],))
            continue

        attempts = int(row["attempts"] or 0) + 1
        wait_seconds = relay_backoff_seconds(attempts)
        with get_db() as db:
            db.execute(
                """
                UPDATE relay_queue
                SET attempts = ?,
                    next_attempt_at = datetime('now', ?),
                    last_error = ?,
                    last_status_code = ?
                WHERE id = ?
                """,
                (
                    attempts,
                    f"+{wait_seconds} seconds",
                    relay_state.get("last_error"),
                    relay_state.get("last_status_code"),
                    row["id"],
                )
            )


def cloud_relay_enabled_for_device_source(device_source):
    return normalize_device_source(device_source, default=DEVICE_SOURCE_REAL) == DEVICE_SOURCE_REAL


def fetch_cloud_command(device_id=None, device_source=DEVICE_SOURCE_REAL):
    if not cloud_relay_enabled_for_device_source(device_source):
        return None
    if not RELAY_COMMAND_URL_LIST:
        return None
    for url in RELAY_COMMAND_URL_LIST:
        for attempt in range(2):
            try:
                response = requests.get(
                    url,
                    headers=relay_headers_for_device(device_id),
                    timeout=(RELAY_CONNECT_TIMEOUT_SEC, RELAY_TIMEOUT_SEC),
                    verify=RELAY_VERIFY_TLS,
                )
                if not response.ok:
                    logger.warning("Relay command failed (%s): %s", url, response.status_code)
                    continue
                payload = response.json()
                command = payload.get("command")
                command_id = payload.get("command_id")
                if command:
                    logger.info("Relayed command from %s: %s", url, command)
                return {"command": command, "command_id": command_id}
            except requests.RequestException as exc:
                logger.warning("Relay command failed (%s): %s", url, exc)
            except ValueError as exc:
                logger.warning("Relay command invalid JSON (%s): %s", url, exc)
    return None


def acknowledge_relay_command(device_id, command_id, device_source=DEVICE_SOURCE_REAL):
    if not cloud_relay_enabled_for_device_source(device_source):
        return False
    if not RELAY_COMMAND_URL_LIST:
        return False

    try:
        normalized_command_id = int(command_id)
    except (TypeError, ValueError):
        return False

    if normalized_command_id <= 0:
        return False

    headers = relay_headers_for_device(device_id)
    if "X-Device-Id" not in headers or "X-Device-Key" not in headers:
        return False

    for url in RELAY_COMMAND_URL_LIST:
        ack_url = f"{url.rstrip('/')}/ack"
        for attempt in range(2):
            try:
                response = requests.post(
                    ack_url,
                    json={"command_id": normalized_command_id, "command_source": "queue"},
                    headers=headers,
                    timeout=(RELAY_CONNECT_TIMEOUT_SEC, RELAY_TIMEOUT_SEC),
                    verify=RELAY_VERIFY_TLS,
                )
                if not response.ok:
                    logger.warning("Relay command ack failed (%s): %s", ack_url, response.status_code)
                    continue
                payload = response.json()
                if payload.get("acknowledged"):
                    return True
            except requests.RequestException as exc:
                logger.warning("Relay command ack failed (%s): %s", ack_url, exc)
            except ValueError as exc:
                logger.warning("Relay command ack invalid JSON (%s): %s", ack_url, exc)
    return False


def relay_status_async(payload):
    if not RELAY_STATUS_URL_LIST:
        return
    enqueue_relay_payload(payload)
    if not relay_lock.acquire(blocking=False):
        return

    def worker(data):
        try:
            drain_relay_queue()
        finally:
            relay_lock.release()

    threading.Thread(target=worker, args=(payload,), daemon=True).start()


def start_relay_drain_worker():
    if not RELAY_STATUS_URL_LIST:
        return

    def loop():
        while True:
            time.sleep(30)
            if not relay_lock.acquire(blocking=False):
                continue
            try:
                drain_relay_queue()
            finally:
                relay_lock.release()

    threading.Thread(target=loop, daemon=True).start()


def handle_mqtt_telemetry_message(topic, payload_text):
    device_id = mqtt_extract_device_id(topic, "telemetry")
    if not device_id:
        logger.warning("Ignoring MQTT telemetry on unexpected topic %s", topic)
        return

    try:
        payload = json.loads(payload_text or "{}")
    except ValueError as exc:
        logger.warning("Invalid MQTT telemetry JSON for %s: %s", device_id, exc)
        return

    if not isinstance(payload, dict):
        logger.warning("Ignoring MQTT telemetry for %s because payload was not an object", device_id)
        return

    payload_device_id = normalize_device_id(payload.get("device_id"))
    if payload_device_id and payload_device_id != device_id:
        logger.warning(
            "Rejected MQTT telemetry for %s because payload device_id %s did not match topic device_id %s",
            device_id,
            payload_device_id,
            device_id,
        )
        return

    auth_ok, normalized_device_id, error_message, status_code = authenticate_device_identity(
        device_id,
        device_key=payload.get("device_key"),
        remote_addr="mqtt",
        require_key=False,
    )
    if not auth_ok:
        logger.warning("Rejected MQTT telemetry for %s: %s (%s)", device_id, error_message, status_code)
        return

    payload["device_id"] = normalized_device_id
    process_telemetry_payload(payload, source_ip="mqtt", transport="mqtt")
    mqtt_state["last_message_at"] = now_utc().strftime(TIMESTAMP_FORMAT)


def handle_mqtt_command_ack(topic, payload_text):
    device_id = mqtt_extract_device_id(topic, "command_ack")
    if not device_id:
        logger.warning("Ignoring MQTT command ack on unexpected topic %s", topic)
        return

    command = str(payload_text or "").strip().upper()
    if not command:
        return

    if acknowledge_queued_command(device_id, command):
        clear_mqtt_command(device_id)
        logger.info("Acknowledged MQTT command for %s: %s", device_id, command)
    mqtt_state["last_message_at"] = now_utc().strftime(TIMESTAMP_FORMAT)


def on_mqtt_connect(client, _userdata, _flags, reason_code, _properties=None):
    mqtt_state["connected"] = False
    if reason_code != 0:
        mqtt_state["last_error"] = f"connect failed ({reason_code})"
        logger.warning("MQTT connect failed: %s", reason_code)
        return

    mqtt_state["connected"] = True
    mqtt_state["last_connect_at"] = now_utc().strftime(TIMESTAMP_FORMAT)
    mqtt_state["last_error"] = None
    telemetry_subscription = mqtt_topic("+", "telemetry")
    ack_subscription = mqtt_topic("+", "command_ack")
    client.subscribe(telemetry_subscription, qos=MQTT_QOS)
    client.subscribe(ack_subscription, qos=MQTT_QOS)
    logger.info("MQTT connected and subscribed to %s and %s", telemetry_subscription, ack_subscription)


def on_mqtt_disconnect(_client, _userdata, reason_code, _properties=None):
    mqtt_state["connected"] = False
    if reason_code:
        mqtt_state["last_error"] = f"disconnect ({reason_code})"
        logger.warning("MQTT disconnected: %s", reason_code)


def on_mqtt_message(_client, _userdata, message):
    topic = str(message.topic or "")
    payload_text = message.payload.decode("utf-8", errors="ignore")
    if topic.endswith("/telemetry"):
        handle_mqtt_telemetry_message(topic, payload_text)
    elif topic.endswith("/command_ack"):
        handle_mqtt_command_ack(topic, payload_text)


def start_mqtt_bridge():
    global mqtt_client, mqtt_started

    if mqtt is None:
        if MQTT_ENABLED:
            logger.warning("MQTT was enabled but paho-mqtt is not installed. MQTT bridge will stay disabled.")
        return

    if not mqtt_feature_enabled() or mqtt_started:
        return

    client_id = f"smart-water-tank-backend-{os.getpid()}"
    client = mqtt.Client(client_id=client_id)
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_mqtt_connect
    client.on_disconnect = on_mqtt_disconnect
    client.on_message = on_mqtt_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)

    with mqtt_lock:
        mqtt_client = client
        mqtt_started = True

    try:
        client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=MQTT_KEEPALIVE_SEC)
        client.loop_start()
        logger.info("MQTT bridge starting for %s:%s", MQTT_BROKER_HOST, MQTT_BROKER_PORT)
    except Exception as exc:
        mqtt_state["last_error"] = str(exc)
        logger.warning("MQTT bridge failed to start: %s", exc)


def stop_mqtt_bridge():
    with mqtt_lock:
        client = mqtt_client
    if not client:
        return
    try:
        client.loop_stop()
    except Exception:
        pass
    try:
        client.disconnect()
    except Exception:
        pass


@app.route("/motor/on", methods=["POST"])
@login_required
@csrf_protect
def motor_on():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    return queue_command("ON", target_device=current_scope_device_id(request.args.get("device_id", type=str)))


@app.route("/motor/off", methods=["POST"])
@login_required
@csrf_protect
def motor_off():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    return queue_command("OFF", target_device=current_scope_device_id(request.args.get("device_id", type=str)))


@app.route("/motor/auto", methods=["POST"])
@login_required
@csrf_protect
def motor_auto():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    payload = queue_command("AUTO", target_device=current_scope_device_id(request.args.get("device_id", type=str)))
    payload["message"] = "AUTO command queued for the device."
    return payload


@app.route("/sensor/calibrate", methods=["POST"])
@login_required
@csrf_protect
def sensor_calibrate():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    payload = queue_command("CALIBRATE", target_device=current_scope_device_id(request.args.get("device_id", type=str)))
    payload["message"] = "Sensor calibration request queued."
    return payload


@app.route("/sensor/configure", methods=["POST"])
@login_required
@csrf_protect
def sensor_configure():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    height_cm = request.values.get("height_cm", type=float)
    capacity_liters = request.values.get("capacity_liters", type=float)
    target_device = current_scope_device_id(request.values.get("device_id", type=str))
    height_cm = request.values.get("height_cm", type=float)
    capacity_liters = request.values.get("capacity_liters", type=float)
    target_device = current_scope_device_id(request.values.get("device_id", type=str))

    if height_cm is None or capacity_liters is None:
        return jsonify({"error": "height_cm and capacity_liters are required"}), 400

    if height_cm < 30 or height_cm > 500:
        return jsonify({"error": "height_cm must be between 30 and 500"}), 400

    if capacity_liters < 50 or capacity_liters > 50000:
        return jsonify({"error": "capacity_liters must be between 50 and 50000"}), 400

    command = f"CONFIG:{height_cm:.1f}:{capacity_liters:.1f}"
    payload = queue_command(command, target_device=target_device)
    payload = queue_command(command, target_device=target_device)
    payload["height_cm"] = round(height_cm, 1)
    payload["capacity_liters"] = round(capacity_liters, 1)
    payload["message"] = "Tank configuration command queued."
    return payload



def mobile_queue_command_response(command, target_device=None, message=None):
    result = queue_command(command, target_device=target_device)
    if isinstance(result, tuple):
        payload, status_code = result
        return jsonify(payload), status_code
    payload = dict(result)
    if message:
        payload["message"] = message
    return jsonify(payload)


@app.route("/api/mobile/auth/login", methods=["POST"])
def mobile_auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    authenticated_user = authenticate_dashboard_user(username, password)
    if not authenticated_user:
        time.sleep(0.5)
        return jsonify({"error": "invalid username or password"}), 401
    token = issue_mobile_token(authenticated_user)
    return jsonify({
        "token": token,
        "viewer": {
            "role": authenticated_user.get("role"),
            "username": authenticated_user.get("username"),
            "device_id": authenticated_user.get("device_id"),
            "display_name": authenticated_user.get("display_name"),
            "cloud_feed_enabled": authenticated_user.get("cloud_feed_enabled", True),
            "cloud_feed_mode": authenticated_user.get("cloud_feed_mode", DEVICE_SERVICE_CLOUD_FEED_FULL),
            "ai_analysis_enabled": authenticated_user.get("ai_analysis_enabled", True),
        },
        "expires_in_seconds": MOBILE_TOKEN_MAX_AGE_SECONDS,
    })


@app.route("/api/mobile/bootstrap")
@mobile_auth_required
def mobile_bootstrap():
    event_limit = max(1, min(request.args.get("event_limit", default=5, type=int), 30))
    audit_limit = max(1, min(request.args.get("audit_limit", default=5, type=int), 30))
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    viewer = resolve_mobile_user() or {}
    snapshot = load_dashboard_snapshot(scoped_device_id)
    public_snapshot = strip_ip_address_fields(snapshot)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    payload = {
        "snapshot": public_snapshot,
        "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
        "events": build_events(event_limit, device_id=scoped_device_id),
        "generated_at": now_utc().strftime(TIMESTAMP_FORMAT),
        "viewer": viewer,
    }
    if viewer.get("role") == "admin":
        payload["ops"] = build_ops_dashboard_payload(snapshot, device_id=scoped_device_id, audit_limit=audit_limit)
    return jsonify(payload)


@app.route("/api/mobile/analytics")
@mobile_auth_required
def mobile_analytics():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    response = mobile_customer_ai_analysis_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    try:
        start_dt, end_exclusive, label = resolve_date_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(build_analytics(start_dt, end_exclusive, label, device_id=scoped_device_id))


@app.route("/api/mobile/account/password", methods=["POST"])
@mobile_auth_required
def mobile_account_password():
    user = resolve_mobile_user()
    if not user:
        return jsonify({"error": "authentication required"}), 401

    data = request.get_json(silent=True) or {}
    current_password = str(data.get("current_password") or "")
    new_password = str(data.get("new_password") or "")
    confirm_password = str(data.get("confirm_password") or "")

    if not current_password or not new_password or not confirm_password:
        return jsonify({"error": "current_password, new_password, and confirm_password are required"}), 400
    if new_password != confirm_password:
        return jsonify({"error": "New password and confirm password do not match."}), 400
    if len(new_password) < 6:
        return jsonify({"error": "Use at least 6 characters for the new password."}), 400

    role = user.get("role")
    username = user.get("username")
    device_id = user.get("device_id")

    if role == "admin":
        if not verify_dashboard_password(current_password):
            return jsonify({"error": "Current password is incorrect."}), 400
        set_dashboard_password(new_password)
        log_audit_event(
            actor=username or current_actor_username(),
            action="reset_dashboard_password",
            target_type="dashboard_account",
            target_id=LOGIN_USERNAME,
            details={"username": LOGIN_USERNAME, "source": "mobile_api"},
        )
        return jsonify({"message": "Dashboard password updated successfully."})

    if role == "customer" and device_id:
        account = fetch_customer_account(device_id)
        if not account or int(account.get("active", 0)) != 1:
            return jsonify({"error": "Customer account not found."}), 404
        if not check_password_hash(account.get("password_hash", ""), current_password):
            return jsonify({"error": "Current password is incorrect."}), 400
        updated_account = update_customer_password(device_id, new_password)
        updated_service_config = fetch_device_service_config(device_id, account=updated_account)
        log_audit_event(
            actor=username or device_id,
            action="reset_customer_password",
            target_type="customer_account",
            target_id=updated_account["device_id"],
            device_id=updated_account["device_id"],
            details={"display_name": updated_account.get("display_name"), "source": "mobile_api_self_service"},
        )
        return jsonify(
            {
                "message": "Cloud password updated successfully.",
                "viewer": {
                    "role": role,
                    "username": username,
                    "device_id": device_id,
                    "display_name": updated_account.get("display_name") or device_id,
                    "cloud_feed_enabled": updated_service_config.get("cloud_feed_enabled", True),
                    "cloud_feed_mode": updated_service_config.get("cloud_feed_mode"),
                    "ai_analysis_enabled": updated_service_config.get("effective_ai_analysis_enabled", True),
                },
            }
        )

    return jsonify({"error": "Unsupported account type."}), 400


@app.route("/api/mobile/last")
@mobile_auth_required
def mobile_last():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    if not snapshot:
        return jsonify({"error": "no data"}), 404
    return jsonify(strip_ip_address_fields(snapshot))


@app.route("/api/mobile/motor/on", methods=["POST"])
@mobile_auth_required
def mobile_motor_on():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    return mobile_queue_command_response("ON", target_device=current_mobile_scope_device_id(request.args.get("device_id", type=str)))


@app.route("/api/mobile/motor/off", methods=["POST"])
@mobile_auth_required
def mobile_motor_off():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    return mobile_queue_command_response("OFF", target_device=current_mobile_scope_device_id(request.args.get("device_id", type=str)))


@app.route("/api/mobile/motor/auto", methods=["POST"])
@mobile_auth_required
def mobile_motor_auto():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    return mobile_queue_command_response(
        "AUTO",
        target_device=current_mobile_scope_device_id(request.args.get("device_id", type=str)),
        message="AUTO command queued for the device.",
    )


@app.route("/api/mobile/sensor/calibrate", methods=["POST"])
@mobile_auth_required
def mobile_sensor_calibrate():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    return mobile_queue_command_response(
        "CALIBRATE",
        target_device=current_mobile_scope_device_id(request.args.get("device_id", type=str)),
        message="Sensor calibration request queued.",
    )


@app.route("/api/mobile/sensor/configure", methods=["POST"])
@mobile_auth_required
def mobile_sensor_configure():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    data = request.get_json(silent=True) or {}
    height_cm = data.get("height_cm")
    capacity_liters = data.get("capacity_liters")
    target_device = current_mobile_scope_device_id(data.get("device_id"))
    try:
        height_cm = float(height_cm)
        capacity_liters = float(capacity_liters)
    except (TypeError, ValueError):
        return jsonify({"error": "height_cm and capacity_liters are required"}), 400
    if height_cm < 30 or height_cm > 500:
        return jsonify({"error": "height_cm must be between 30 and 500"}), 400
    if capacity_liters < 50 or capacity_liters > 50000:
        return jsonify({"error": "capacity_liters must be between 50 and 50000"}), 400
    command = f"CONFIG:{height_cm:.1f}:{capacity_liters:.1f}"
    result = queue_command(command, target_device=target_device)
    if isinstance(result, tuple):
        payload, status_code = result
        return jsonify(payload), status_code
    payload = dict(result)
    payload["height_cm"] = round(height_cm, 1)
    payload["capacity_liters"] = round(capacity_liters, 1)
    payload["message"] = "Tank configuration command queued."
    return jsonify(payload)


@app.route("/api/mobile/device/status")
@mobile_auth_required
def mobile_device_status():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    service_config = fetch_device_service_config(scoped_device_id) if scoped_device_id else None
    snapshot = load_dashboard_snapshot(scoped_device_id)
    return jsonify({
        "snapshot": strip_ip_address_fields(snapshot),
        "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
        "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
        "service_config": service_config,
        "viewer": resolve_mobile_user(),
    })


@app.route("/api/mobile/device/services", methods=["GET", "POST"])
@mobile_auth_required
def mobile_device_services():
    user = resolve_mobile_user()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "admin access required"}), 403

    source_payload = request.get_json(silent=True) or {}
    requested_device_id = (
        source_payload.get("device_id")
        if request.method == "POST"
        else request.args.get("device_id", type=str)
    )
    target_device = current_mobile_scope_device_id(requested_device_id) or latest_device_id()
    if not target_device:
        return jsonify({"error": "device not found"}), 404

    if request.method == "GET":
        snapshot = fetch_device_snapshot(target_device)
        return jsonify(
            {
                "device_id": target_device,
                "config": fetch_device_service_config(target_device),
                "live_services": {
                    "source_tank_monitoring_enabled": bool(
                        snapshot and str(snapshot.get("lower_tank_service") or "").upper() == "ON"
                    ),
                    "buzzer_enabled": bool(
                        snapshot and str(snapshot.get("buzzer_service") or "").upper() == "ON"
                    ),
                    "led_display_enabled": bool(
                        snapshot and str(snapshot.get("led_display_service") or "").upper() == "ON"
                    ),
                },
            }
        )

    updated_config = upsert_device_service_config(
        target_device,
        source_tank_monitoring_enabled=source_payload.get("source_tank_monitoring_enabled"),
        ai_analysis_enabled=source_payload.get("ai_analysis_enabled"),
        cloud_feed_mode=source_payload.get("cloud_feed_mode"),
        buzzer_enabled=source_payload.get("buzzer_enabled"),
        led_display_enabled=source_payload.get("led_display_enabled"),
    )
    command = build_device_service_command(updated_config)
    queue_result = queue_command(command, target_device=target_device)
    log_audit_event(
        actor=user.get("username") or current_actor_username(),
        action="update_device_services_mobile",
        target_type="device",
        target_id=target_device,
        device_id=target_device,
        details={
            "service_config": updated_config,
            "queued_command": command,
            "source": "mobile_api",
        },
    )
    response_payload = {
        "message": f"Service settings saved for {target_device}.",
        "device_id": target_device,
        "config": updated_config,
        "queued_command": command,
    }
    if isinstance(queue_result, tuple):
        error_payload, status_code = queue_result
        response_payload.update({"queue_error": error_payload.get("error")})
        return jsonify(response_payload), status_code
    response_payload.update(queue_result)
    return jsonify(response_payload)

@app.route("/device/command")
def get_command():
    try:
        request_source = resolve_request_device_source()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if request_source != get_device_source_mode():
        return active_device_source_conflict_response(request_source)

    auth_ok, auth_payload, auth_status = authenticate_device_request()
    if not auth_ok:
        return auth_payload, auth_status
    device_id = auth_payload

    queued = peek_queued_command(device_id)
    if queued is not None:
        return {
            "command": queued["command"],
            "command_id": queued["id"],
            "command_source": "queue",
            "control_policy": CONTROL_POLICY,
            "device_id": device_id,
            "device_source": request_source,
            "device_source_mode": get_device_source_mode(),
        }

    relay_payload = fetch_cloud_command(device_id, device_source=request_source) or {}
    return {
        "command": relay_payload.get("command"),
        "command_id": relay_payload.get("command_id"),
        "command_source": "relay" if relay_payload.get("command") else None,
        "control_policy": CONTROL_POLICY,
        "device_id": device_id,
        "device_source": request_source,
        "device_source_mode": get_device_source_mode(),
    }


@app.route("/device/command/ack", methods=["POST"])
def acknowledge_device_command():
    payload = request.get_json(silent=True) or {}
    try:
        request_source = resolve_request_device_source(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if request_source != get_device_source_mode():
        return active_device_source_conflict_response(request_source)

    auth_ok, auth_payload, auth_status = authenticate_device_request(payload)
    if not auth_ok:
        return auth_payload, auth_status
    device_id = auth_payload

    command_id = payload.get("command_id")
    command_source = str(payload.get("command_source") or "queue").strip().lower()

    if command_source == "relay":
        acknowledged = acknowledge_relay_command(device_id, command_id, device_source=request_source)
    else:
        acknowledged = acknowledge_queued_command_id(device_id, command_id)
        if acknowledged:
            clear_mqtt_command(device_id)

    status_code = 200 if acknowledged else 404
    return {
        "acknowledged": acknowledged,
        "command_id": command_id,
        "command_source": command_source,
        "device_id": device_id,
        "device_source": request_source,
        "device_source_mode": get_device_source_mode(),
        "control_policy": CONTROL_POLICY,
    }, status_code


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next")
    if request.method == "POST":
        return handle_role_login("customer")
    if is_logged_in():
        return redirect(dashboard_home_url())
    if next_url and is_safe_next_url(next_url):
        return redirect(url_for("customer_login", next=next_url))
    return redirect(url_for("customer_login"))


@app.route("/login/customer", methods=["GET", "POST"])
def customer_login():
    return handle_role_login("customer")


@app.route("/login/admin", methods=["GET", "POST"])
def admin_login():
    return handle_role_login("admin")


@app.route("/logout", methods=["POST"])
@login_required
@csrf_protect
def logout():
    session.clear()
    return redirect(url_for("customer_login"))


@app.route("/account/password", methods=["GET", "POST"])
@admin_required
@csrf_protect
def account_password():
    error = None
    success = None

    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not verify_dashboard_password(current_password):
            error = "Current password is incorrect."
        elif new_password != confirm_password:
            error = "New password and confirm password do not match."
        elif len(new_password) < 6:
            error = "Use at least 6 characters for the new password."
        else:
            set_dashboard_password(new_password)
            actor = current_actor_username()
            log_audit_event(
                actor=actor,
                action="reset_dashboard_password",
                target_type="dashboard_account",
                target_id=LOGIN_USERNAME,
                details={"username": LOGIN_USERNAME},
            )
            success = "Dashboard password updated successfully. Use the new password on your next login."
            logger.info("Dashboard password updated by %s", actor)

    return render_dashboard_password_page(error=error, success=success)


def render_dashboard_page(selected_device_id=None):
    scoped_dashboard_device_id = current_scope_device_id(selected_device_id)
    logger.info("Dashboard opened")
    customer_cloud_feed = current_customer_cloud_feed_enabled()
    return render_template(
        "index.html",
        is_admin=is_admin_user(),
        viewer_role=current_user_role(),
        viewer_device_id=current_customer_device_id(),
        viewer_display_name=(current_customer_account() or {}).get("display_name") if current_user_role() == "customer" else "Administrator",
        selected_device_id=scoped_dashboard_device_id,
        cloud_feed_enabled=customer_cloud_feed,
        cloud_feed_error=customer_cloud_feed_error_message() if current_user_role() == "customer" and not customer_cloud_feed else "",
        can_control=is_logged_in() and (is_admin_user() or customer_cloud_feed),
    )


@app.route("/admin/customers", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_customers():
    error = None
    success = None
    search_query = request.args.get("q", "", type=str) or ""

    if request.method == "POST":
        device_id = request.form.get("device_id", "")
        display_name = request.form.get("display_name", "")
        password = request.form.get("password", "")
        try:
            account = upsert_customer_account(device_id, password, display_name=display_name)
            log_audit_event(
                actor=current_actor_username(),
                action="upsert_customer_account",
                target_type="customer_account",
                target_id=account["device_id"],
                device_id=account["device_id"],
                details={"display_name": account.get("display_name")},
            )
            success = f"Customer account saved for {account['device_id']}."
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/password", methods=["POST"])
@admin_required
@csrf_protect
def admin_customer_password_reset(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    new_password = request.form.get("password", "")
    account = fetch_customer_account(device_id)

    if not account:
        error = f"Customer account not found for {normalize_device_id(device_id) or 'that device'}."
    else:
        try:
            updated_account = upsert_customer_account(
                account["device_id"],
                new_password,
                display_name=account.get("display_name"),
            )
            log_audit_event(
                actor=current_actor_username(),
                action="reset_customer_password",
                target_type="customer_account",
                target_id=updated_account["device_id"],
                device_id=updated_account["device_id"],
                details={"display_name": updated_account.get("display_name")},
            )
            success = f"Customer password reset for {updated_account['device_id']}."
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/edit", methods=["POST"])
@admin_required
@csrf_protect
def admin_customer_edit(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    display_name = request.form.get("display_name", "")
    account = fetch_customer_account(device_id)

    if not account:
        error = f"Customer account not found for {normalize_device_id(device_id) or 'that device'}."
    else:
        try:
            updated_account = update_customer_account_profile(
                account["device_id"],
                display_name=display_name,
            )
            log_audit_event(
                actor=current_actor_username(),
                action="update_customer_profile",
                target_type="customer_account",
                target_id=updated_account["device_id"],
                device_id=updated_account["device_id"],
                details={"display_name": updated_account.get("display_name")},
            )
            success = f"Customer name updated for {updated_account['device_id']}."
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/cloud-feed", methods=["POST"])
@admin_required
@csrf_protect
def admin_customer_cloud_feed(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    desired_value = request.form.get("cloud_feed_enabled", "")
    account = fetch_customer_account(device_id)

    if not account:
        error = f"Customer account not found for {normalize_device_id(device_id) or 'that device'}."
    else:
        enable_cloud_feed = str(desired_value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
        updated_config = upsert_device_service_config(
            account["device_id"],
            cloud_feed_mode=(
                DEVICE_SERVICE_CLOUD_FEED_FULL if enable_cloud_feed else DEVICE_SERVICE_CLOUD_FEED_OFF
            ),
        )
        updated_account = fetch_customer_account(account["device_id"])
        log_audit_event(
            actor=current_actor_username(),
            action="set_customer_cloud_feed",
            target_type="customer_account",
            target_id=updated_account["device_id"],
            device_id=updated_account["device_id"],
            details={
                "cloud_feed_enabled": bool(enable_cloud_feed),
                "cloud_feed_mode": updated_config.get("cloud_feed_mode"),
            },
        )
        success = (
            f"Cloud feed enabled for {updated_account['device_id']}."
            if enable_cloud_feed
            else f"Cloud feed disabled for {updated_account['device_id']}."
        )

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/services", methods=["POST"])
@admin_required
@csrf_protect
def admin_customer_services(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    normalized_device_id = normalize_device_id(device_id)

    if not normalized_device_id:
        error = "Choose a valid device before updating services."
    else:
        updated_config = upsert_device_service_config(
            normalized_device_id,
            source_tank_monitoring_enabled=("source_tank_monitoring_enabled" in request.form),
            ai_analysis_enabled=("ai_analysis_enabled" in request.form),
            cloud_feed_mode=request.form.get("cloud_feed_mode"),
            buzzer_enabled=("buzzer_enabled" in request.form),
            led_display_enabled=("led_display_enabled" in request.form),
        )
        queued_command = build_device_service_command(updated_config)
        queue_result = queue_command(queued_command, target_device=normalized_device_id)
        log_audit_event(
            actor=current_actor_username(),
            action="update_device_services",
            target_type="device",
            target_id=normalized_device_id,
            device_id=normalized_device_id,
            details={
                "service_config": updated_config,
                "queued_command": queued_command,
                "queue_result": queue_result if not isinstance(queue_result, tuple) else queue_result[0],
            },
        )
        success = (
            f"Service settings saved for {normalized_device_id}. "
            "Device-side changes will apply on the next command poll."
        )

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/reboot", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_reboot(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    normalized_device_id = normalize_device_id(device_id)

    if not normalized_device_id:
        error = "Choose a valid device before queueing a reboot."
    else:
        result = queue_command("REBOOT", target_device=normalized_device_id)
        if isinstance(result, tuple):
            payload, _status_code = result
            error = payload.get("error") or f"Unable to queue a reboot for {normalized_device_id}."
        else:
            log_audit_event(
                actor=current_actor_username(),
                action="queue_device_reboot",
                target_type="device",
                target_id=normalized_device_id,
                device_id=normalized_device_id,
                details={
                    "command": result.get("command"),
                    "mqtt_delivery": result.get("mqtt_delivery"),
                    "queued_at": result.get("queued_at"),
                },
            )
            success = (
                f"Reboot command queued for {normalized_device_id}. "
                "The device will restart on its next command poll."
            )

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/admin/customers/<device_id>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_delete_known_device(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    normalized_device_id = normalize_device_id(device_id)

    try:
        deleted_counts = delete_known_device(normalized_device_id)
        log_audit_event(
            actor=current_actor_username(),
            action="delete_known_device",
            target_type="device",
            target_id=normalized_device_id,
            details=deleted_counts,
        )
        success = f"Deleted device {normalized_device_id} from admin records."
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=100),
    )
    device_summary = build_admin_device_summary(available_devices)
    filtered_accounts = filter_admin_search_results(accounts, search_query)
    filtered_available_devices = filter_admin_search_results(available_devices, search_query)

    return render_customer_admin_page(
        accounts=filtered_accounts,
        available_devices=filtered_available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary,
    )


@app.route("/")
def dashboard():
    if not is_logged_in():
        return redirect(url_for("customer_login"))
    if is_admin_user():
        return redirect(url_for("admin_customers"))
    return render_dashboard_page(request.args.get("device_id", type=str))


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return redirect(url_for("admin_customers"))


@app.route("/admin/ops/bootstrap")
@admin_required
def admin_ops_bootstrap():
    alert_limit = max(1, min(request.args.get("alert_limit", default=8, type=int), 50))
    audit_limit = max(1, min(request.args.get("audit_limit", default=6, type=int), 30))
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    return jsonify(build_ops_dashboard_payload(snapshot, device_id=scoped_device_id, alert_limit=alert_limit, audit_limit=audit_limit))


@app.route("/admin/db-summary")
@admin_required
def admin_db_summary():
    return jsonify(build_db_summary_payload())


@app.route("/admin/device-source-mode", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_device_source_mode():
    if request.method == "GET":
        return jsonify(
            {
                "device_source_mode": get_device_source_mode(),
                "allowed_modes": [DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL],
            }
        )

    data = request.get_json(silent=True) or {}
    requested_mode = data.get("device_source_mode")
    if requested_mode is None:
        requested_mode = request.form.get("device_source_mode")
    try:
        normalized_mode = set_device_source_mode(requested_mode)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    actor = current_actor_username()
    log_audit_event(
        actor=actor,
        action="set_device_source_mode",
        target_type="backend_setting",
        target_id=DEVICE_SOURCE_MODE_SETTING,
        details={"device_source_mode": normalized_mode},
    )
    return jsonify(
        {
            "status": "updated",
            "device_source_mode": normalized_mode,
            "allowed_modes": [DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL],
        }
    )


@app.route("/admin/dashboard/<device_id>")
@admin_required
def admin_device_dashboard(device_id):
    return redirect(url_for("device_detail_page", device_id=current_scope_device_id(device_id)))


@app.route("/customer/dashboard")
@customer_required
def customer_dashboard():
    return render_dashboard_page()


@app.route("/devices/<device_id>")
@login_required
def device_detail_page(device_id):
    customer_cloud_feed_abort_if_disabled()
    scoped_device_id = current_scope_device_id(device_id)
    return render_template("device_detail.html", device_id=scoped_device_id, is_admin=is_admin_user())


@app.route("/firmware/update")
@login_required
def firmware_update_redirect():
    customer_cloud_feed_abort_if_disabled()
    device_id = current_scope_device_id(request.args.get("device_id", type=str)) or latest_device_id()
    target_url = build_device_update_url(device_id)
    if not target_url:
        return render_firmware_update_unavailable(device_id)

    actor = current_actor_username()
    log_audit_event(
        actor=actor,
        action="open_firmware_update",
        target_type="device",
        target_id=device_id or "latest",
        device_id=device_id,
        details={"target_url": target_url},
    )
    return redirect(target_url)


@app.route("/devices/<device_id>/firmware/update")
@login_required
def device_firmware_update_redirect(device_id):
    customer_cloud_feed_abort_if_disabled()
    scoped_device_id = current_scope_device_id(device_id)
    target_url = build_device_update_url(scoped_device_id)
    if not target_url:
        return render_firmware_update_unavailable(scoped_device_id)

    actor = current_actor_username()
    log_audit_event(
        actor=actor,
        action="open_firmware_update",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={"target_url": target_url},
    )
    return redirect(target_url)


@app.route("/devices/<device_id>/status")
@login_required
def device_detail_status(device_id):
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    if not snapshot:
        return jsonify({"error": "device not found"}), 404
    alerts = fetch_filtered_alerts(limit=10, device_id=scoped_device_id)
    audit = fetch_audit_events(limit=10, device_id=scoped_device_id)
    history = fetch_device_history(scoped_device_id, limit=10)
    events = build_events(limit=10, device_id=scoped_device_id)
    return jsonify(
        {
            "device_id": scoped_device_id,
            "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
            "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
            "snapshot": strip_ip_address_fields(snapshot, keep_device_local_url=True),
            "service_config": fetch_device_service_config(scoped_device_id),
            "alerts": alerts,
            "audit": audit,
            "history": history,
            "events": events,
        }
    )


@app.route("/status", methods=["GET", "POST"])
def status():
    if request.method == "GET":
        return jsonify({
            "server": "Smart Water Tank API",
            "status": "running",
            "version": API_VERSION,
            "swt_version": SWT_VERSION,
            "device_source_mode": get_device_source_mode(),
        })

    data = request.get_json(silent=True)
    if not data:
        logger.warning("Invalid JSON received")
        return jsonify({"error": "invalid json"}), 400
    try:
        data["device_source"] = resolve_request_device_source(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    auth_ok, auth_payload, auth_status = authenticate_device_request(data)
    if not auth_ok:
        return auth_payload, auth_status
    data["device_id"] = auth_payload
    process_telemetry_payload(data, source_ip=request.remote_addr, transport="http")

    return jsonify({
        "result": "saved",
        "server": "Smart Water Tank API",
        "version": API_VERSION,
        "swt_version": SWT_VERSION,
        "server_status": "running",
        "control_policy": CONTROL_POLICY,
        "device_source": data["device_source"],
        "device_source_mode": get_device_source_mode(),
    })


@app.route("/last")
@login_required
def last():
    logger.info("Fetching last status")
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    return jsonify(strip_ip_address_fields(snapshot, keep_device_local_url=True))


@app.route("/history")
@login_required
def history():
    if not TELEMETRY_HISTORY_ENABLED:
        return jsonify([])

    logger.info("Fetching history")
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    source_clause, source_params = device_source_where_clause()

    try:
        start_dt, end_exclusive, _label = resolve_date_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with get_db() as db:
        if scoped_device_id:
            rows = db.execute(
                f"""
                SELECT level, lower_tank_level, ai_usage_rate, created_at
                FROM tank_data
                WHERE created_at >= ? AND created_at < ?
                  AND device_id = ?
                  AND {source_clause}
                ORDER BY created_at ASC, id ASC
                LIMIT 800
                """,
                (
                    start_dt.strftime(TIMESTAMP_FORMAT),
                    end_exclusive.strftime(TIMESTAMP_FORMAT),
                    scoped_device_id,
                    *source_params,
                ),
            ).fetchall()
        else:
            rows = db.execute(
                f"""
                SELECT level, lower_tank_level, ai_usage_rate, created_at
                FROM tank_data
                WHERE created_at >= ? AND created_at < ?
                  AND {source_clause}
                ORDER BY created_at ASC, id ASC
                LIMIT 800
                """,
                (start_dt.strftime(TIMESTAMP_FORMAT), end_exclusive.strftime(TIMESTAMP_FORMAT), *source_params)
            ).fetchall()

    return jsonify(
        [
            {
                "level": row["level"],
                "lower_tank_level": row["lower_tank_level"],
                "source_tank_level": row["lower_tank_level"],
                "ai_usage_rate": row["ai_usage_rate"],
                "time": format_timestamp(row["created_at"])
            }
            for row in rows
        ]
    )


@app.route("/health")
def health():
    return {
        "status": "ok",
        "database": DB_FILE,
        "version": API_VERSION,
        "swt_version": SWT_VERSION,
        "capacity_liters": round(TANK_CAPACITY_LITERS, 1),
        "control_policy": CONTROL_POLICY,
        "device_source_mode": get_device_source_mode(),
    }


@app.route("/device/status")
@login_required
def device_status():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    if not snapshot_has_live_device_data(snapshot):
        refresh_operational_alerts(None)
        return device_status_from_snapshot(None)
    refresh_operational_alerts(snapshot)

    return device_status_from_snapshot(snapshot)


@app.route("/system/status")
@login_required
def system_status():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    return build_system_status_payload(snapshot, device_id=scoped_device_id)


@app.route("/relay/health")
@admin_required
def relay_health():
    with get_db() as db:
        pending = db.execute("SELECT COUNT(*) FROM relay_queue").fetchone()[0]
    return jsonify({
        "relay_enabled": bool(RELAY_STATUS_URL_LIST),
        "relay_targets": [],
        "verify_tls": RELAY_VERIFY_TLS,
        "last_success_at": relay_state.get("last_success_at"),
        "last_error_at": relay_state.get("last_error_at"),
        "last_error": relay_state.get("last_error"),
        "last_status_code": relay_state.get("last_status_code"),
        "pending_count": pending,
        "device_source_mode": get_device_source_mode(),
    })


@app.route("/monitoring/alerts")
@login_required
def monitoring_alerts():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    refresh_operational_alerts(load_dashboard_snapshot(scoped_device_id) if scoped_device_id else None)
    limit = max(1, min(request.args.get("limit", default=20, type=int), 100))
    severity = request.args.get("severity", type=str)
    return jsonify(fetch_filtered_alerts(limit=limit, severity=severity, device_id=scoped_device_id))


@app.route("/monitoring/alerts/<int:alert_id>/resolve", methods=["POST"])
@admin_required
@csrf_protect
def monitoring_alert_resolve(alert_id):
    alert = resolve_alert_by_id(alert_id)
    if not alert:
        return jsonify({"error": "alert not found"}), 404
    actor = current_actor_username()
    log_audit_event(
        actor=actor,
        action="resolve_alert",
        target_type="ops_alert",
        target_id=str(alert_id),
        device_id=alert.get("device_id"),
        details={"kind": alert.get("kind"), "severity": alert.get("severity"), "message": alert.get("message")},
    )
    logger.info("Alert %s resolved from dashboard", alert_id)
    return jsonify({"status": "resolved", "alert_id": alert_id, "kind": alert.get("kind"), "device_id": alert.get("device_id")})


@app.route("/monitoring/summary")
@login_required
def monitoring_summary():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    return jsonify(build_monitoring_summary_payload(snapshot, device_id=scoped_device_id))


@app.route("/monitoring/audit")
@login_required
def monitoring_audit():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    limit = max(1, min(request.args.get("limit", default=30, type=int), 100))
    device_id = current_scope_device_id(request.args.get("device_id", type=str))
    return jsonify(fetch_audit_events(limit=limit, device_id=device_id))


@app.route("/events")
@login_required
def events():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    limit = max(1, min(request.args.get("limit", default=12, type=int), 30))
    return jsonify(build_events(limit, device_id=current_scope_device_id(request.args.get("device_id", type=str))))


@app.route("/dashboard/bootstrap")
@login_required
def dashboard_bootstrap():
    event_limit = max(1, min(request.args.get("event_limit", default=5, type=int), 30))
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    public_snapshot = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)

    return jsonify(
        {
            "snapshot": public_snapshot,
            "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
            "events": build_events(event_limit, device_id=scoped_device_id),
            "generated_at": now_utc().strftime(TIMESTAMP_FORMAT),
            "viewer": {
                "role": current_user_role(),
                "device_id": scoped_device_id,
                "display_name": (current_customer_account() or {}).get("display_name") if current_user_role() == "customer" else "Administrator",
                "cloud_feed_enabled": current_customer_cloud_feed_enabled(),
                "cloud_feed_mode": (
                    (current_customer_service_config() or {}).get("cloud_feed_mode")
                    if current_user_role() == "customer"
                    else DEVICE_SERVICE_CLOUD_FEED_FULL
                ),
                "ai_analysis_enabled": current_customer_ai_analysis_enabled(),
            },
        }
    )


@app.route("/analytics")
@login_required
def analytics():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    response = customer_ai_analysis_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    try:
        start_dt, end_exclusive, label = resolve_date_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if TELEMETRY_HISTORY_ENABLED:
        logger.info("Running analytics engine for %s", label)
    return jsonify(build_analytics(start_dt, end_exclusive, label, device_id=scoped_device_id))


@app.route("/analytics/export.csv")
@login_required
def analytics_csv_export():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    response = customer_ai_analysis_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    try:
        start_dt, end_exclusive, label = resolve_date_window()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    payload = build_analytics(start_dt, end_exclusive, label, device_id=scoped_device_id)
    csv_payload = build_analytics_csv_payload(payload, device_id=scoped_device_id)
    filename = build_analytics_csv_filename(payload, device_id=scoped_device_id)
    return Response(
        csv_payload,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/ml/predict")
@login_required
def ml_predict():
    response = customer_ai_analysis_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str)) or latest_device_id()
    if not scoped_device_id:
        return jsonify({"error": "device not found"}), 404

    try:
        payload = build_level_forecast_payload(scoped_device_id)
    except LookupError:
        return jsonify({"error": "device not found"}), 404
    except FileNotFoundError:
        return jsonify(
            {
                "error": (
                    "Level forecast model artifact not found. Train it first with: "
                    "python scripts/train_level_forecast_model.py --db-path data/tank.db --horizon-hours 1"
                ),
                "device_id": scoped_device_id,
            }
        ), 404
    except ValueError as exc:
        return jsonify({"error": str(exc), "device_id": scoped_device_id}), 409
    except RuntimeError as exc:
        logger.warning("ML prediction unavailable for %s: %s", scoped_device_id, exc)
        return jsonify({"error": str(exc), "device_id": scoped_device_id}), 503
    except Exception:
        logger.exception("Unexpected ML prediction failure for %s", scoped_device_id)
        return jsonify({"error": "ML prediction failed unexpectedly.", "device_id": scoped_device_id}), 500

    return jsonify(payload)


logger.info("Initializing database")
logger.info("Database path resolved to %s (%s)", DB_FILE, DB_PATH_SOURCE)
for rejected_db_path in DB_PATH_REJECTED:
    logger.warning(
        "Database path %s (%s) is not writable; skipping it.",
        rejected_db_path["path"],
        rejected_db_path["source"],
    )
validate_runtime_db_configuration()
init_db()
ensure_app_secret_key_persisted()
maybe_reset_admin_password_on_boot()
if APP_SECRET_KEY_SOURCE == "env":
    logger.info("APP_SECRET_KEY loaded from environment.")
elif APP_SECRET_KEY_SOURCE == "default":
    logger.warning("APP_SECRET_KEY fallback is active because no persistent secret could be loaded. Set APP_SECRET_KEY before production.")
else:
    logger.info("APP_SECRET_KEY loaded from persistent storage at %s.", APP_SECRET_KEY_SOURCE)
if IS_RENDER and not render_persistent_db_active():
    logger.warning("Render deployment is using DB_FILE=%s. Use /var/data/tank.db on a mounted disk to preserve dashboard and customer passwords across deploys.", DB_FILE)
if not TELEMETRY_HISTORY_ENABLED:
    logger.warning("Telemetry history persistence is disabled. This deployment keeps only the latest snapshot per device.")
elif MAX_TELEMETRY_ROWS_PER_DEVICE > 0:
    logger.warning(
        "Telemetry history is capped at %s rows per device with %s-day retention.",
        MAX_TELEMETRY_ROWS_PER_DEVICE,
        DATA_RETENTION_DAYS,
    )
if is_default_dashboard_password():
    logger.warning("Dashboard is using default login credentials. Change LOGIN_USERNAME and LOGIN_PASSWORD before production.")
if app.secret_key == DEFAULT_APP_SECRET_KEY:
    logger.warning("APP_SECRET_KEY is using the default value. Change it before production.")
if DEVICE_KEYS_SOURCE != "default":
    logger.info("Device key registry loaded from %s.", DEVICE_KEYS_SOURCE)
if LOCAL_VIRTUAL_DEVICE_AUTH_MAP:
    logger.info(
        "Loaded %s local virtual device auth entries from env files.",
        len(LOCAL_VIRTUAL_DEVICE_AUTH_MAP),
    )
if device_keys_look_default():
    logger.warning("DEVICE_KEYS is using placeholder values. Replace them before production.")
start_relay_drain_worker()
start_mqtt_bridge()
atexit.register(stop_mqtt_bridge)


if __name__ == "__main__":
    logger.info("Starting Smart Water Tank Server")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)




























