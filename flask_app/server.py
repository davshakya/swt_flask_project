import atexit
from datetime import datetime, timedelta, timezone
from functools import wraps
import base64
import csv
import hashlib
import hmac
import ipaddress
import io
import json
import logging
import os
from pathlib import Path
import binascii
import re
import secrets
import smtplib
import subprocess
import time
import threading
import math
from email.message import EmailMessage

pd = None
PANDAS_IMPORT_ERROR = None
import requests
try:
    import pymysql
    from pymysql.cursors import DictCursor as MySqlDictCursor
except Exception:
    pymysql = None
    MySqlDictCursor = None
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None
from flask import Flask, abort, g, has_request_context, jsonify, redirect, render_template, render_template_string, request, send_file, send_from_directory, session, url_for
from flask import Response
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from urllib.parse import urlparse
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from flask_app.android_releases import (
    android_release_storage_path as android_release_storage_path_for_dir,
    build_android_apk_blob_response as build_android_apk_blob_response_payload,
    build_android_apk_file_response as build_android_apk_file_response_payload,
    build_android_release_manifest,
    make_stored_android_apk_filename,
    read_uploaded_android_apk,
)
from flask_app.forecast_payloads import (
    build_level_forecast_payload as build_level_forecast_response_payload,
    build_unavailable_level_forecast_payload as build_unavailable_level_forecast_response_payload,
)
from flask_app.firmware_artifacts import (
    build_firmware_artifact_file_response as build_firmware_artifact_file_response_payload,
    build_firmware_artifact_payload as build_firmware_artifact_response_payload,
    firmware_artifact_storage_path as firmware_artifact_storage_path_for_dir,
    make_stored_firmware_filename,
    read_uploaded_firmware,
    sanitize_firmware_filename as sanitize_firmware_filename_value,
    extract_firmware_version_label as extract_firmware_version_label_from_payload,
)
from flask_app.mobile_firmware_routes import register_mobile_firmware_routes
from flask_app.runtime_utils import (
    env_float,
    env_int,
    env_flag as runtime_env_flag,
    load_dotenv_values,
    normalize_db_path as runtime_normalize_db_path,
    normalize_http_base_url as runtime_normalize_http_base_url,
    parse_simple_dotenv,
)


PLACEHOLDER_DEVICE_CONFIG_MARKERS = (
    "change-me",
    "replace-with-a-real",
    "build-default",
)
CLEARABLE_DEVICE_ENV_KEYS = {
    "RELAY_STATUS_URLS",
    "RELAY_COMMAND_URLS",
}
DEVICE_ENV_PRESERVE_EXPLICIT_BLANK_KEYS = {
    "DATABASE_URL",
    "MYSQL_PASSWORD",
}
DEVICE_ENV_OVERRIDE_KEYS = {
    "CUSTOMER_COMMUNICATION_FROM_EMAIL",
    "CUSTOMER_COMMUNICATION_FROM_NAME",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_USE_TLS",
    "SMTP_USE_SSL",
    "SMTP_TIMEOUT_SECONDS",
    "SALES_ENQUIRY_TO_EMAILS",
    "SALES_ENQUIRY_BACKUP_PATH",
}


def device_config_value_is_placeholder(value):
    normalized_value = str(value or "").strip().lower()
    if not normalized_value:
        return True
    return any(marker in normalized_value for marker in PLACEHOLDER_DEVICE_CONFIG_MARKERS)


def load_workspace_device_env_files(project_root, environ):
    workspace_root = project_root.parent
    project_device_env = project_root / "device.env"
    candidate_paths = (
        project_device_env,
        workspace_root / "device.env",
        workspace_root / "swt_firmware_project" / "device.env",
        workspace_root / "swt_android_app_project" / "device.env",
    )
    merged_values = {}
    blank_overrides = set()
    forced_overrides = set()
    for dotenv_path in candidate_paths:
        parsed_values = parse_simple_dotenv(dotenv_path)
        if not parsed_values:
            continue
        prefer_real_override = dotenv_path == project_device_env
        for key, value in parsed_values.items():
            if not str(value or "").strip():
                if key in CLEARABLE_DEVICE_ENV_KEYS:
                    blank_overrides.add(key)
                else:
                    continue
            if prefer_real_override and key in DEVICE_ENV_OVERRIDE_KEYS:
                merged_values[key] = value
                forced_overrides.add(key)
                continue
            current_value = merged_values.get(key)
            if current_value is None or device_config_value_is_placeholder(current_value):
                merged_values[key] = value
                continue
            if prefer_real_override and not device_config_value_is_placeholder(value):
                merged_values[key] = value
    for key, value in merged_values.items():
        existing_value = environ.get(key)
        if key in forced_overrides:
            environ[key] = value
        elif key in blank_overrides:
            environ[key] = ""
        elif existing_value == "" and key in DEVICE_ENV_PRESERVE_EXPLICIT_BLANK_KEYS:
            continue
        elif existing_value is None or device_config_value_is_placeholder(existing_value):
            environ[key] = value


def load_local_env_files():
    module_root = Path(__file__).resolve().parent
    project_root = module_root.parent
    load_dotenv_values(
        (project_root / ".env", module_root / ".env"),
        environ=os.environ,
        preserve_existing=True,
    )
    load_workspace_device_env_files(project_root, environ=os.environ)


load_local_env_files()

DEFAULT_APP_SECRET_KEY = "change-me-before-production"
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "change-me-admin-password"
DEFAULT_DEVICE_KEY = "change-me-device-key"
DEFAULT_DEVICE_KEYS = ""
DATE_ONLY_FORMAT = "%Y-%m-%d"
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
IST_TIMEZONE = timezone(timedelta(hours=5, minutes=30))
MOBILE_TOKEN_SALT = "smart-water-tank-mobile"
APP_SECRET_KEY_SETTING = "app_secret_key"
DASHBOARD_PASSWORD_SETTING = "dashboard_password"
DEVICE_SOURCE_MODE_SETTING = "device_source_mode"
DEVICE_SIMULATOR_STATE_PREFIX = "device_simulator_state:"
CUSTOMER_ACCOUNTS_BOOTSTRAP_ENV = "CUSTOMER_ACCOUNTS_BOOTSTRAP_B64"
DASHBOARD_PASSWORD_HASH_ENV = "DASHBOARD_PASSWORD_HASH"
DEVICE_SOURCE_REAL = "real"
DEVICE_SOURCE_VIRTUAL = "virtual"
DEVICE_SOURCE_HEADER = "X-Device-Source"
IS_RENDER = bool(os.environ.get("RENDER") or os.environ.get("RENDER_SERVICE_ID"))
APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
DEFAULT_PROJECT_ROOT = PROJECT_ROOT
STATIC_DIR = APP_ROOT / "static"
DATA_DIR = PROJECT_ROOT / "data"


def resolve_test_repo_root():
    configured_path = str(os.environ.get("SWT_FLASK_TEST_REPO") or "").strip()
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if not candidate.is_absolute():
            candidate = (PROJECT_ROOT / candidate).resolve()
        return candidate.resolve()
    return (PROJECT_ROOT.parent / "swt_flask_test_project").resolve()


TEST_REPO_ROOT = resolve_test_repo_root()


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


def validate_runtime_db_configuration():
    return


def resolve_app_secret_key():
    configured_secret = os.environ.get("APP_SECRET_KEY", "").strip()
    if configured_secret:
        return configured_secret, "env"
    raise RuntimeError("APP_SECRET_KEY must be set explicitly when using the MySQL backend.")


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


def resolve_database_backend():
    configured_backend = os.environ.get("DB_BACKEND", "").strip().lower()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if configured_backend:
        if configured_backend in {"mysql", "mariadb"}:
            return "mysql"
        raise RuntimeError("Only MySQL/MariaDB is supported. Set DB_BACKEND=mysql and configure DATABASE_URL or MYSQL_* values.")
    if database_url.lower().startswith(("mysql://", "mysql+pymysql://", "mariadb://")):
        return "mysql"
    if database_url:
        raise RuntimeError("Only MySQL/MariaDB DATABASE_URL values are supported.")
    return "mysql"


DB_BACKEND = resolve_database_backend()
USING_MYSQL = DB_BACKEND == "mysql"
APP_SECRET_KEY, APP_SECRET_KEY_SOURCE = resolve_app_secret_key()

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
DEFAULT_CUSTOMER_DEVICE_ID = os.environ.get("SWT_DEVICE_ID", "").strip() or "swt-node-01"
DEFAULT_CUSTOMER_ACCOUNTS = ((DEFAULT_CUSTOMER_DEVICE_ID, "Tank Owner"),)
CUSTOMER_PASSWORD_RESET_TTL_MINUTES = max(10, env_int("CUSTOMER_PASSWORD_RESET_TTL_MINUTES", 60))
CUSTOMER_COMMUNICATION_FROM_EMAIL = os.environ.get("CUSTOMER_COMMUNICATION_FROM_EMAIL", "support@salewell.co.in").strip()
CUSTOMER_COMMUNICATION_FROM_NAME = os.environ.get("CUSTOMER_COMMUNICATION_FROM_NAME", "SaleWell Smart Tank Support").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.salewell.co.in").strip()
SMTP_PORT = env_int("SMTP_PORT", 465)
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", CUSTOMER_COMMUNICATION_FROM_EMAIL).strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_USE_TLS = env_flag("SMTP_USE_TLS", default=False)
SMTP_USE_SSL = env_flag("SMTP_USE_SSL", default=True)
SMTP_TIMEOUT_SECONDS = max(3, env_int("SMTP_TIMEOUT_SECONDS", 10))
SALES_ENQUIRY_TO_EMAILS = os.environ.get("SALES_ENQUIRY_TO_EMAILS", CUSTOMER_COMMUNICATION_FROM_EMAIL).strip()
SALES_ENQUIRY_BACKUP_PATH = Path(
    os.environ.get("SALES_ENQUIRY_BACKUP_PATH", str(DATA_DIR / "sales_enquiries.jsonl")).strip()
).expanduser()
if not SALES_ENQUIRY_BACKUP_PATH.is_absolute():
    SALES_ENQUIRY_BACKUP_PATH = PROJECT_ROOT / SALES_ENQUIRY_BACKUP_PATH
LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", DEFAULT_ADMIN_USERNAME).strip() or DEFAULT_ADMIN_USERNAME
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", DEFAULT_ADMIN_PASSWORD).strip() or DEFAULT_ADMIN_PASSWORD
RESET_ADMIN_PASSWORD_ON_BOOT = os.environ.get("RESET_ADMIN_PASSWORD_ON_BOOT", "false").lower() in {"1", "true", "yes", "on"}
DEVICE_KEYS, DEVICE_KEYS_SOURCE = resolve_device_key_registry()
DEVICE_AUTH_REQUIRED = env_flag("DEVICE_AUTH_REQUIRED", default=True)
AUTO_REGISTER_DEVICE_KEYS = env_flag("AUTO_REGISTER_DEVICE_KEYS", default=False)
AUTO_REGISTER_DEVICE_ID_PREFIXES = tuple(
    prefix.strip()
    for prefix in os.environ.get("AUTO_REGISTER_DEVICE_ID_PREFIXES", "swt-").split(",")
    if prefix.strip()
)
AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH = max(16, env_int("AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH", 32))
TANK_CAPACITY_LITERS = env_float("TANK_CAPACITY_LITERS", 1000.0)
STALE_AFTER_SECONDS = env_int("DATA_STALE_AFTER_SECONDS", 180)
DIRECT_PEER_STALE_AFTER_SECONDS = max(1, env_int("DIRECT_PEER_STALE_AFTER_SECONDS", 15))
DATA_RETENTION_DAYS = max(1, env_int("DATA_RETENTION_DAYS", 45))
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
# Keep local sibling test-repo virtual devices discoverable without extra shell setup.
SEED_VIRTUAL_DEVICE_ENVS = (
    not IS_RENDER
    if str(os.environ.get("SWT_FLASK_TEST_REPO") or "").strip()
    else env_flag("SEED_VIRTUAL_DEVICE_ENVS", default=not IS_RENDER)
)
PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT = env_flag("PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT", default=IS_RENDER)
SEED_CONFIGURED_DEVICES_ON_VIEW = env_flag("SEED_CONFIGURED_DEVICES_ON_VIEW", default=False)
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
DEFAULT_RELAY_STATUS_URLS = ""
DEFAULT_RELAY_COMMAND_URLS = ""
RELAY_STATUS_URLS = os.environ.get("RELAY_STATUS_URLS", DEFAULT_RELAY_STATUS_URLS)
RELAY_COMMAND_URLS = os.environ.get("RELAY_COMMAND_URLS", DEFAULT_RELAY_COMMAND_URLS)
AUTO_RELAY_LOCAL_TO_SHARED_CLOUD = env_flag("AUTO_RELAY_LOCAL_TO_SHARED_CLOUD", default=True)
RELAY_TIMEOUT_SEC = env_float("RELAY_TIMEOUT_SEC", 25.0)
RELAY_CONNECT_TIMEOUT_SEC = env_float("RELAY_CONNECT_TIMEOUT_SEC", 5.0)
RELAY_VERIFY_TLS = os.environ.get("RELAY_VERIFY_TLS", "true").lower() not in {"0", "false", "no"}
FIRMWARE_ARTIFACT_DIR = normalize_db_path(
    os.environ.get("FIRMWARE_ARTIFACT_DIR", str(DATA_DIR / "firmware_artifacts"))
)
FIRMWARE_ARTIFACT_MAX_BYTES = max(256 * 1024, env_int("FIRMWARE_ARTIFACT_MAX_MB", 4) * 1024 * 1024)
GLOBAL_FIRMWARE_TARGET = "__all_customers__"
ANDROID_RELEASE_DIR = normalize_db_path(
    os.environ.get("ANDROID_RELEASE_DIR", str(DATA_DIR / "android_releases"))
)
ANDROID_RELEASE_MAX_BYTES = max(1024 * 1024, env_int("ANDROID_RELEASE_MAX_MB", 128) * 1024 * 1024)
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


def logging_ist_converter(timestamp, *_args):
    return datetime.fromtimestamp(timestamp, IST_TIMEZONE).timetuple()


logging.Formatter.converter = staticmethod(logging_ist_converter)
logging.basicConfig(
    level=APP_LOG_LEVEL,
    format="%(asctime)s IST | %(levelname)s | %(message)s",
    datefmt=TIMESTAMP_FORMAT,
)

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


def url_origin(value):
    parsed = urlparse(str(value or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def url_hostname(value):
    parsed = urlparse(str(value or "").strip())
    return (parsed.hostname or "").lower()


def host_is_private_or_local(host):
    normalized = str(host or "").strip().lower()
    if normalized in {"localhost"}:
        return True
    if not normalized:
        return False
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def normalize_relay_target_url(value, default_path):
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    parsed = urlparse(raw_value)
    if not parsed.scheme or not parsed.netloc:
        logger.warning("Ignoring relay URL without scheme and host: %s", raw_value)
        return ""
    if parsed.path in {"", "/"} and default_path:
        return f"{parsed.scheme}://{parsed.netloc}{default_path}"
    return raw_value.rstrip("/")


def parse_relay_url_list(value, default_path):
    urls = []
    seen = set()
    for item in parse_url_list(value):
        url = normalize_relay_target_url(item, default_path)
        if not url:
            continue
        key = url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def current_request_origin():
    if not has_request_context():
        return ""
    return url_origin(request.url_root)


def derived_shared_cloud_relay_urls(default_path):
    return parse_relay_url_list(DEFAULT_SHARED_CLOUD_BASE_URL, default_path)


def should_auto_relay_local_request_to_shared_cloud():
    if not AUTO_RELAY_LOCAL_TO_SHARED_CLOUD or not has_request_context():
        return False
    request_host = url_hostname(current_request_origin())
    cloud_host = url_hostname(DEFAULT_SHARED_CLOUD_BASE_URL)
    return bool(request_host and cloud_host and request_host != cloud_host and host_is_private_or_local(request_host))


def relay_urls_for_current_request(urls, default_path=None):
    effective_urls = list(urls)
    if not effective_urls and default_path and should_auto_relay_local_request_to_shared_cloud():
        effective_urls = derived_shared_cloud_relay_urls(default_path)
    request_host = url_hostname(current_request_origin())
    if not request_host:
        return effective_urls
    return [url for url in effective_urls if url_hostname(url) != request_host]


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


RELAY_STATUS_URL_LIST = parse_relay_url_list(RELAY_STATUS_URLS, "/status")
RELAY_COMMAND_URL_LIST = parse_relay_url_list(RELAY_COMMAND_URLS, "/device/command")


def resolve_device_key_value(value):
    text = str(value or "").strip()
    if text == "SWT_DEVICE_API_KEY":
        return os.environ.get("SWT_DEVICE_API_KEY", "").strip()
    env_reference = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text)
    if env_reference:
        return os.environ.get(env_reference.group(1), "").strip()
    return text


def parse_device_key_registry(value):
    registry = {}
    for item in parse_url_list(value):
        if ":" not in item:
            continue
        device_id, key = item.split(":", 1)
        device_id = device_id.strip()
        key = resolve_device_key_value(key)
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
        key = resolve_device_key_value(key)
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
    if DEVICE_AUTH_REQUIRED:
        return True
    if AUTO_REGISTER_DEVICE_KEYS:
        return True
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


def hash_device_api_key(device_key):
    return hashlib.sha256(str(device_key or "").encode("utf-8")).hexdigest()


def device_id_allowed_for_auto_registration(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return False
    if AUTO_REGISTER_DEVICE_ID_PREFIXES and not any(
        normalized_device_id.startswith(prefix)
        for prefix in AUTO_REGISTER_DEVICE_ID_PREFIXES
    ):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}", normalized_device_id))


def device_key_allowed_for_auto_registration(device_key):
    text = str(device_key or "").strip()
    if len(text) < AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH:
        return False
    if any(marker in text.lower() for marker in PLACEHOLDER_DEVICE_CONFIG_MARKERS):
        return False
    return len(set(text)) >= 12


def fetch_auto_registered_device_auth_rule(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_key_hash, registration_source
            FROM device_auth_keys
            WHERE device_id = ?
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "kind": "auto_registered",
        "pattern": normalized_device_id,
        "prefix": normalized_device_id,
        "key_hash": row["device_key_hash"],
        "registration_source": row["registration_source"],
    }


def remember_auto_registered_device_key(device_id, device_key, remote_addr=None, registration_source="auto_activation"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    device_key_hash = hash_device_api_key(device_key)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO device_auth_keys(device_id, device_key_hash, registration_source, first_seen_at, last_seen_at, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                device_key_hash=excluded.device_key_hash,
                registration_source=excluded.registration_source,
                last_seen_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            """,
            (normalized_device_id, device_key_hash, registration_source),
        )
    logger.info("Registered device credentials for %s from %s", normalized_device_id, remote_addr or "unknown")
    return {
        "kind": "auto_registered",
        "pattern": normalized_device_id,
        "prefix": normalized_device_id,
        "key_hash": device_key_hash,
        "registration_source": registration_source,
    }


def register_device_credentials(device_id, device_key, registration_source="admin_manual", remote_addr=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("Device ID is required.")
    if not device_id_allowed_for_auto_registration(normalized_device_id):
        raise ValueError("Device ID is not allowed for registration.")
    if not device_key_allowed_for_auto_registration(device_key):
        raise ValueError(f"Device API key must be at least {AUTO_REGISTER_DEVICE_KEY_MIN_LENGTH} characters and must not be a placeholder.")
    rule = remember_auto_registered_device_key(
        normalized_device_id,
        device_key,
        remote_addr=remote_addr,
        registration_source=registration_source,
    )
    remember_registered_device(
        normalized_device_id,
        registration_source=registration_source,
        key_rule=rule.get("pattern") if rule else normalized_device_id,
        best_effort=True,
    )
    return normalized_device_id


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
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "database is busy" in message
        or "lock wait timeout" in message
        or "deadlock found" in message
    )


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
    except Exception as exc:
        if best_effort:
            logger.warning("Could not remember registered device %s: %s", normalized_device_id, exc)
            note_registered_device_touch(normalized_device_id, registration_source, key_rule)
            return
        raise
    note_registered_device_touch(normalized_device_id, registration_source, key_rule)


def iter_virtual_device_tests_roots():
    seen = set()
    roots = [PROJECT_ROOT / "tests"]
    try:
        project_root_is_overridden = PROJECT_ROOT.resolve() != DEFAULT_PROJECT_ROOT.resolve()
    except OSError:
        project_root_is_overridden = str(PROJECT_ROOT) != str(DEFAULT_PROJECT_ROOT)
    if not project_root_is_overridden:
        roots.append(TEST_REPO_ROOT / "tests")

    for tests_root in roots:
        resolved = str(tests_root.resolve()) if tests_root.exists() else str(tests_root)
        if resolved in seen:
            continue
        seen.add(resolved)
        yield tests_root


def iter_virtual_device_env_paths():
    seen = set()
    candidates = []
    for tests_root in iter_virtual_device_tests_roots():
        candidates.append(tests_root / "virtual_device.env")
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
    resolved_path = path.resolve() if path.exists() else path
    for project_root in (PROJECT_ROOT, TEST_REPO_ROOT):
        try:
            return str(resolved_path.relative_to(project_root))
        except ValueError:
            continue
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
    if not SEED_CONFIGURED_DEVICES_ON_VIEW:
        return

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
LOCAL_VIRTUAL_DEVICE_AUTH_ENTRIES = {}
LOCAL_VIRTUAL_DEVICE_AUTH_MAP = {}
LOCAL_VIRTUAL_DEVICE_AUTH_KEY_RULES = {}


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


def dashboard_identity_prefix(role):
    return "admin" if role == "admin" else "customer"


def clear_dashboard_identity(role=None):
    prefix = dashboard_identity_prefix(role or session.get("role") or "customer")
    for key in ("logged_in", "username", "role", "device_id", "auth_marker"):
        session.pop(key, None)
    for key in ("logged_in", "username", "device_id", "auth_marker"):
        session.pop(f"{prefix}_{key}", None)


def set_active_dashboard_identity(role, username, device_id=None, auth_marker=None):
    session["logged_in"] = True
    session["username"] = username
    session["role"] = role
    session["device_id"] = device_id
    session["auth_marker"] = auth_marker


def store_dashboard_identity(authenticated_user):
    role = authenticated_user["role"]
    prefix = dashboard_identity_prefix(role)
    session[f"{prefix}_logged_in"] = True
    session[f"{prefix}_username"] = authenticated_user["username"]
    session[f"{prefix}_device_id"] = authenticated_user.get("device_id")
    session[f"{prefix}_auth_marker"] = authenticated_user.get("auth_marker")
    set_active_dashboard_identity(
        role,
        authenticated_user["username"],
        authenticated_user.get("device_id"),
        authenticated_user.get("auth_marker"),
    )


def activate_dashboard_identity(role):
    prefix = dashboard_identity_prefix(role)
    if not session.get(f"{prefix}_logged_in"):
        return False
    set_active_dashboard_identity(
        role,
        session.get(f"{prefix}_username"),
        session.get(f"{prefix}_device_id"),
        session.get(f"{prefix}_auth_marker"),
    )
    return is_logged_in()


def current_session_auth_marker():
    if not session.get("logged_in"):
        return None
    return current_auth_marker_for_identity(
        session.get("role") or "admin",
        username=session.get("username"),
        device_id=session.get("device_id"),
    )


def refresh_current_session_auth_marker():
    auth_marker = current_session_auth_marker()
    if not auth_marker:
        return False
    session["auth_marker"] = auth_marker
    return True


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

def is_logged_in():
    if not bool(session.get("logged_in")):
        return False
    stored_auth_marker = str(session.get("auth_marker") or "").strip()
    expected_auth_marker = current_session_auth_marker()
    if stored_auth_marker and expected_auth_marker and secrets.compare_digest(stored_auth_marker, expected_auth_marker):
        return True
    clear_dashboard_identity()
    return False


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_logged_in() or activate_dashboard_identity("admin") or activate_dashboard_identity("customer"):
            return view(*args, **kwargs)
        return redirect(url_for("customer_login", next=request.path))

    return wrapped


def role_mismatch_response():
    if request.method in {"GET", "HEAD"}:
        return redirect(dashboard_home_url())
    abort(403)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not activate_dashboard_identity("admin"):
            return redirect(url_for("admin_login", next=request.path))
        if not is_admin_user():
            return role_mismatch_response()
        return view(*args, **kwargs)

    return wrapped


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not activate_dashboard_identity("customer"):
            return redirect(url_for("customer_login", next=request.path))
        if current_user_role() != "customer":
            return role_mismatch_response()
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
    if app.config.get("SESSION_COOKIE_SECURE") and not request.is_secure:
        warnings.append(
            "SESSION_COOKIE_SECURE is enabled but this page is being served over HTTP. "
            "Browsers will refuse to keep the login cookie, so users can appear to be logged out immediately. "
            "Use HTTPS or set SESSION_COOKIE_SECURE=false for local/LAN HTTP deployments."
        )
    if APP_SECRET_KEY_SOURCE == "default":
        warnings.append(
            "APP_SECRET_KEY is using the default fallback value. Browser and mobile sessions can be invalidated after restarts."
        )
    return warnings


def homepage_login_status():
    if not is_logged_in():
        return None
    role = current_user_role()
    if role == "admin":
        return {"role": "admin", "display_name": "Admin"}
    account = current_customer_account() or {}
    return {
        "role": "customer",
        "display_name": account.get("display_name") or session.get("username") or "Customer",
    }


def stored_dashboard_identity_is_valid(role):
    prefix = dashboard_identity_prefix(role)
    if not session.get(f"{prefix}_logged_in"):
        return False
    username = session.get(f"{prefix}_username")
    device_id = session.get(f"{prefix}_device_id")
    stored_auth_marker = str(session.get(f"{prefix}_auth_marker") or "").strip()
    expected_auth_marker = current_auth_marker_for_identity(role, username=username, device_id=device_id)
    return bool(
        stored_auth_marker
        and expected_auth_marker
        and secrets.compare_digest(stored_auth_marker, expected_auth_marker)
    )


def homepage_auth_status():
    active_user = homepage_login_status()
    active_role = (active_user or {}).get("role")
    customer_device_id = normalize_device_id(session.get("customer_device_id"))
    customer_account = fetch_customer_account(customer_device_id) if customer_device_id else None
    return {
        "active_user": active_user,
        "admin_logged_in": active_role == "admin" or stored_dashboard_identity_is_valid("admin"),
        "customer_logged_in": active_role == "customer" or stored_dashboard_identity_is_valid("customer"),
        "customer_display_name": (
            (customer_account or {}).get("display_name")
            or session.get("customer_username")
            or "Customer"
        ),
    }


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


def sales_form_from_pricing_query():
    selected_plan = str(request.args.get("plan", "")).strip()
    if not selected_plan:
        return {}

    selected_segment = str(request.args.get("segment", "")).strip()
    valid_segments = {
        "Home / Villa",
        "Apartment / Hostel",
        "Hotel / Institution",
        "Dealer / Installer",
        "Commercial Site",
    }
    if selected_segment not in valid_segments:
        selected_segment = "Commercial Site" if selected_plan == "Enterprise" or "Commercial" in selected_plan else "Home / Villa"

    return {
        "segment": selected_segment,
        "device_count": "1",
        "message": f"Selected pricing plan: {selected_plan}",
    }


def render_login_page(mode="customer", error=None, next_url="/", sales_error=None, sales_success=None, sales_form=None):
    is_admin_mode = mode == "admin"
    is_landing_page = request.endpoint in {"dashboard", "homepage"}
    login_action = url_for("admin_login" if is_admin_mode else "customer_login")
    switch_href = url_for("customer_login" if is_admin_mode else "admin_login")
    switch_label = "Customer Login" if is_admin_mode else "Admin Login"
    persistence_warnings = auth_persistence_warnings()
    sales_success = sales_success or (
        "Thanks for your enquiry. Our team can now follow up with pricing, installation guidance, or a demo."
        if request.args.get("enquiry") == "success"
        else None
    )
    if request.args.get("enquiry") == "saved_email_pending" and not sales_success:
        sales_success = (
            "Thanks for your enquiry. Your request was saved, but the support email delivery needs SMTP checking."
        )
    sales_form = sales_form or sales_form_from_pricing_query()
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
        sales_error=sales_error,
        sales_success=sales_success,
        sales_form=sales_form,
        homepage_user=homepage_login_status(),
        homepage_auth=homepage_auth_status(),
        on_dedicated_login_route=not is_landing_page,
        show_login_modal=bool(error) or not is_landing_page,
    )


def validate_sales_enquiry_payload(form):
    cleaned = {
        "name": str(form.get("name", "")).strip(),
        "phone": str(form.get("phone", "")).strip(),
        "email": str(form.get("email", "")).strip(),
        "city": str(form.get("city", "")).strip(),
        "segment": str(form.get("segment", "")).strip(),
        "device_count": str(form.get("device_count", "")).strip(),
        "message": str(form.get("message", "")).strip(),
    }
    errors = []

    if len(cleaned["name"]) < 2:
        errors.append("Please enter your name.")

    if not cleaned["email"]:
        errors.append("Please enter your email address.")
    else:
        try:
            cleaned["email"] = normalize_customer_email(cleaned["email"])
        except ValueError:
            errors.append("Please enter a valid email address.")

    phone_digits = re.sub(r"\D", "", cleaned["phone"])
    if len(phone_digits) < 10:
        errors.append("Please enter a valid phone or WhatsApp number.")

    if not cleaned["city"]:
        errors.append("Please enter your city or service area.")

    if not cleaned["segment"]:
        errors.append("Please choose the project type.")

    if not cleaned["device_count"]:
        errors.append("Please estimate how many devices you need.")
    else:
        try:
            device_count = int(cleaned["device_count"])
        except (TypeError, ValueError):
            errors.append("Device quantity must be a whole number.")
        else:
            if device_count < 1 or device_count > 10000:
                errors.append("Device quantity must be between 1 and 10000.")
            else:
                cleaned["device_count"] = str(device_count)

    if len(cleaned["message"]) > 800:
        errors.append("Project notes must stay under 800 characters.")

    return cleaned, errors


def build_sales_enquiry_email(cleaned, lead_details):
    submitted_at = datetime.now(timezone.utc).astimezone(IST_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    subject_parts = ["New SaleWell Smart Tank booking"]
    if cleaned.get("segment"):
        subject_parts.append(cleaned["segment"])
    if cleaned.get("city"):
        subject_parts.append(cleaned["city"])
    subject = " | ".join(subject_parts)

    body_lines = [
        "New SaleWell Smart Tank booking/enquiry received.",
        "",
        f"Submitted at: {submitted_at}",
        f"Name: {cleaned.get('name') or '--'}",
        f"Phone / WhatsApp: {cleaned.get('phone') or '--'}",
        f"Email: {cleaned.get('email') or '--'}",
        f"City / service area: {cleaned.get('city') or '--'}",
        f"Project type: {cleaned.get('segment') or '--'}",
        f"Estimated devices needed: {cleaned.get('device_count') or '--'}",
        "",
        "Project notes:",
        cleaned.get("message") or "--",
        "",
        "Request metadata:",
        f"Landing mode: {lead_details.get('landing_mode') or '--'}",
        f"Remote address: {lead_details.get('remote_addr') or '--'}",
    ]
    return subject, "\n".join(body_lines)


def sales_enquiry_recipients():
    configured = SALES_ENQUIRY_TO_EMAILS or CUSTOMER_COMMUNICATION_FROM_EMAIL
    recipients = []
    seen = set()
    for raw_email in re.split(r"[,;\s]+", configured):
        normalized_email = normalize_customer_email(raw_email)
        if normalized_email and normalized_email not in seen:
            recipients.append(normalized_email)
            seen.add(normalized_email)
    fallback_email = normalize_customer_email(CUSTOMER_COMMUNICATION_FROM_EMAIL)
    if not recipients and fallback_email:
        recipients.append(fallback_email)
    return recipients


def send_sales_enquiry_email(cleaned, lead_details):
    subject, body = build_sales_enquiry_email(cleaned, lead_details)
    recipients = sales_enquiry_recipients()
    if not recipients:
        logger.warning("Sales enquiry email was not sent because no support recipient is configured.")
        return False

    sent_any = False
    try:
        for recipient in recipients:
            sent = send_customer_email(
                recipient,
                subject,
                body,
                category="transactional",
                reply_to=cleaned.get("email") or None,
            )
            sent_any = sent_any or sent
    except Exception as exc:
        logger.warning("Sales enquiry email failed for %s (%s): %s", cleaned.get("name"), cleaned.get("phone"), exc)
        return False
    if not sent_any:
        logger.warning("Sales enquiry email was not sent because SMTP is not configured.")
    else:
        logger.info("Sales enquiry email sent to %s", ", ".join(recipients))
    return sent_any


def append_sales_enquiry_backup(cleaned, lead_details, support_email_sent=False, confirmation_email_sent=False):
    record = {
        "saved_at": datetime.now(timezone.utc).astimezone(IST_TIMEZONE).isoformat(),
        "support_email_sent": bool(support_email_sent),
        "confirmation_email_sent": bool(confirmation_email_sent),
        "support_recipients": sales_enquiry_recipients(),
        "lead": {
            "name": cleaned.get("name") or "",
            "phone": cleaned.get("phone") or "",
            "email": cleaned.get("email") or "",
            "city": cleaned.get("city") or "",
            "segment": cleaned.get("segment") or "",
            "device_count": cleaned.get("device_count") or "",
            "message": cleaned.get("message") or "",
        },
        "metadata": {
            "landing_mode": lead_details.get("landing_mode") or "",
            "next_url": lead_details.get("next_url") or "",
            "remote_addr": lead_details.get("remote_addr") or "",
        },
    }
    try:
        SALES_ENQUIRY_BACKUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SALES_ENQUIRY_BACKUP_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    except Exception as exc:
        logger.warning("Sales enquiry backup could not be written to %s: %s", SALES_ENQUIRY_BACKUP_PATH, exc)


def selected_pricing_plan_from_notes(message):
    for line in str(message or "").splitlines():
        normalized_line = line.strip()
        if normalized_line.lower().startswith("selected pricing plan:"):
            return normalized_line.split(":", 1)[1].strip()
    return ""


def build_sales_enquiry_confirmation_email(cleaned):
    selected_plan = selected_pricing_plan_from_notes(cleaned.get("message"))
    greeting_name = cleaned.get("name") or "there"
    subject = "Thanks for booking SaleWell Smart Tank"
    plan_line = f"Selected plan: {selected_plan}\n" if selected_plan else ""
    body = (
        f"Hi {greeting_name},\n\n"
        "Thank you for booking a SaleWell Smart Tank pricing/demo request. "
        "We have received your details and our team will review your site requirements shortly.\n\n"
        "Booking summary:\n"
        f"{plan_line}"
        f"Project type: {cleaned.get('segment') or '--'}\n"
        f"City / service area: {cleaned.get('city') or '--'}\n"
        f"Estimated devices needed: {cleaned.get('device_count') or '--'}\n\n"
        "What happens next:\n"
        "1. Our team will check the selected plan and your site type.\n"
        "2. We will contact you on your phone/WhatsApp number for setup details.\n"
        "3. If needed, we will suggest a better-fit plan before final pricing or installation.\n\n"
        "For urgent questions, reply to this email or contact support@salewell.co.in.\n\n"
        "Welcome to SaleWell Smart Tank.\n"
        "SaleWell IoT Solutions Pvt. Ltd.\n"
    )
    return subject, body


def send_sales_enquiry_confirmation_email(cleaned):
    if not cleaned.get("email"):
        return False
    subject, body = build_sales_enquiry_confirmation_email(cleaned)
    try:
        return send_customer_email(cleaned["email"], subject, body, category="transactional")
    except Exception as exc:
        logger.warning("Sales enquiry confirmation email failed for %s: %s", cleaned.get("email"), exc)
        return False


def handle_role_login(mode):
    expected_role = "admin" if mode == "admin" else "customer"
    if request.method == "GET" and activate_dashboard_identity(expected_role):
        return redirect(dashboard_home_url(expected_role))

    default_url = dashboard_home_url(expected_role)
    next_url = resolve_next_url(default_url)
    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        authenticated_user = authenticate_dashboard_user(username, password)
        if authenticated_user and authenticated_user["role"] == expected_role:
            store_dashboard_identity(authenticated_user)
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
    <div class="eyebrow">SaleWell Smart Tank</div>
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


def admin_alert_cutoff_timestamp(hours=24):
    return (now_utc() - timedelta(hours=hours)).strftime(TIMESTAMP_FORMAT)


def resolve_admin_expired_alerts(updated_before=None):
    cutoff = updated_before or admin_alert_cutoff_timestamp()
    with get_db() as db:
        db.execute(
            """
            UPDATE ops_alerts
            SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE active = 1
              AND updated_at < ?
            """,
            (cutoff,),
        )


def fetch_active_alert_device_ids(updated_since=None):
    with get_db() as db:
        params = []
        query = """
            SELECT DISTINCT device_id
            FROM ops_alerts
            WHERE active = 1
              AND COALESCE(device_id, '') != ''
        """
        if updated_since:
            query += " AND updated_at >= ?"
            params.append(updated_since)
        rows = db.execute(
            query,
            tuple(params),
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


def direct_peer_packet_is_fresh(entry):
    if str(entry.get("direct_peer") or "").strip().lower() in {"disabled", "off"}:
        return None
    raw_age = entry.get("direct_peer_last_packet_age_s")
    try:
        age = int(raw_age)
    except (TypeError, ValueError):
        return None
    if age < 0:
        return False
    return age <= DIRECT_PEER_STALE_AFTER_SECONDS


def admin_node_status_fields(entry, service_config=None):
    service_config = service_config or {}
    node_role = str(entry.get("node_role") or "").strip().lower()
    online = admin_device_is_online(entry)
    master_label = "Reachable" if online else "Unreachable"
    master_tone = "online" if online else "offline"

    slave_configured = bool(service_config.get("slave_device_enabled", True))
    if not slave_configured:
        return {
            "master_status_label": master_label,
            "master_status_tone": master_tone,
            "slave_status_label": "Disabled",
            "slave_status_tone": "clear",
        }

    if node_role == "slave_tank":
        return {
            "master_status_label": "--",
            "master_status_tone": "clear",
            "slave_status_label": "Reachable" if online else "Unreachable",
            "slave_status_tone": "online" if online else "offline",
        }

    peer_packet_fresh = direct_peer_packet_is_fresh(entry)
    if peer_packet_fresh is not None:
        slave_label = "Reachable" if online and peer_packet_fresh else "Unreachable"
        slave_tone = "online" if online and peer_packet_fresh else "offline"
        return {
            "master_status_label": master_label,
            "master_status_tone": master_tone,
            "slave_status_label": slave_label,
            "slave_status_tone": slave_tone,
        }

    lower_tank_service = str(entry.get("lower_tank_service") or "").strip().upper()
    lower_sensor = str(
        entry.get("lower_sensor") or entry.get("source_sensor") or ""
    ).strip().upper()
    if not online:
        slave_label = "Unreachable"
        slave_tone = "offline"
    elif lower_tank_service in {"ON", "OK"}:
        slave_label = "Reachable"
        slave_tone = "online"
    elif lower_tank_service in {"DISABLED", "OFF"} or lower_sensor in {"DISABLED", "OFF"}:
        slave_label = "Unreachable"
        slave_tone = "offline"
    elif lower_sensor and lower_sensor not in {"DISABLED", "OFF", "--"}:
        slave_label = "Reachable"
        slave_tone = "online"
    elif lower_tank_service in {"UNKNOWN", ""}:
        slave_label = "Unreachable"
        slave_tone = "offline"
    else:
        slave_label = "Disabled"
        slave_tone = "clear"

    return {
        "master_status_label": master_label,
        "master_status_tone": master_tone,
        "slave_status_label": slave_label,
        "slave_status_tone": slave_tone,
    }


def admin_reachable_status_fields(enabled, reachable):
    if not enabled:
        return "Disabled", "clear"
    if reachable:
        return "Reachable", "online"
    return "Unreachable", "offline"


def admin_sensor_reachable(raw_status):
    normalized = str(raw_status or "").strip().upper()
    if normalized in {"OK", "ON", "READY", "CONNECTED", "HEALTHY"}:
        return True
    if normalized in {"DISABLED", "OFF", "--", "UNKNOWN", "ERROR", "FAULT", "DISCONNECTED", "TIMEOUT", ""}:
        return False
    return True


def admin_relay_sensor_status_fields(entry, service_config=None):
    service_config = service_config or {}
    online = admin_device_is_online(entry)

    relay_service = str(entry.get("command_service") or entry.get("relay_service") or "").strip().upper()
    relay_enabled = relay_service not in {"DISABLED", "OFF"}
    if bool_flag(entry.get("pump_failure")) or bool_flag(entry.get("dry_run")):
        relay_label, relay_tone = "No level rise", "warning"
    else:
        relay_label, relay_tone = admin_reachable_status_fields(relay_enabled, online and relay_enabled)

    upper_sensor = entry.get("upper_sensor") or entry.get("main_sensor") or entry.get("sensor")
    upper_enabled = bool(service_config.get("main_sensor_enabled", True))
    if str(upper_sensor or "").strip().upper() in {"DISABLED", "OFF"}:
        upper_enabled = False
    upper_label, upper_tone = admin_reachable_status_fields(
        upper_enabled,
        online and upper_enabled and admin_sensor_reachable(upper_sensor),
    )

    lower_sensor = entry.get("lower_sensor") or entry.get("source_sensor")
    lower_enabled = bool(service_config.get("source_tank_monitoring_enabled", True))
    if str(lower_sensor or "").strip().upper() in {"DISABLED", "OFF"}:
        lower_enabled = False
    lower_label, lower_tone = admin_reachable_status_fields(
        lower_enabled,
        online and lower_enabled and admin_sensor_reachable(lower_sensor),
    )

    return {
        "relay_status_label": relay_label,
        "relay_status_tone": relay_tone,
        "upper_sensor_status_label": upper_label,
        "upper_sensor_status_tone": upper_tone,
        "lower_sensor_status_label": lower_label,
        "lower_sensor_status_tone": lower_tone,
    }


def alert_severity_rank(value):
    normalized = str(value or "").strip().lower()
    if normalized == "danger":
        return 3
    if normalized == "warning":
        return 2
    if normalized == "success":
        return 1
    return 0


def fetch_active_alert_summaries(device_ids=None, updated_since=None):
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
    if updated_since:
        query += " AND updated_at >= ?"
        params.append(updated_since)
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
    source_ip = payload.get("source_ip")
    device_local_url = normalize_device_base_url(payload.get("device_local_url") or payload.get("device_ip_url"))
    device_local_host = None
    if device_local_url:
        try:
            device_local_host = urlparse(str(device_local_url)).hostname
        except Exception:
            device_local_host = None
    return {
        "device_id": normalized_device_id,
        "level": payload.get("level"),
        "firmware_version": payload.get("firmware_version"),
        "reset_reason": payload.get("reset_reason"),
        "device_local_url": device_local_url,
        "device_local_host": device_local_host,
        "source_ip": source_ip,
        "last_sync_at": payload.get("last_sync_at"),
        "telemetry_status": payload.get("telemetry_status"),
        "channel_mode": payload.get("channel_mode"),
        "telemetry_service": payload.get("telemetry_service"),
        "command_service": payload.get("command_service"),
        "ota_service": payload.get("ota_service"),
        "lower_tank_service": payload.get("lower_tank_service"),
        "buzzer_service": payload.get("buzzer_service"),
        "led_display_service": payload.get("led_display_service"),
        "local_firmware_upload_service": payload.get("local_firmware_upload_service"),
        "arch_id": payload.get("arch_id"),
        "node_role": payload.get("node_role"),
        "device_type": payload.get("device_type"),
        "direct_peer": payload.get("direct_peer"),
        "direct_peer_remote_ip": payload.get("direct_peer_remote_ip"),
        "direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s"),
        "direct_peer_last_packet_bytes": payload.get("direct_peer_last_packet_bytes"),
        "wifi": payload.get("wifi"),
        "wifi_rssi": payload.get("wifi_rssi"),
        "sensor": payload.get("sensor"),
        "upper_sensor": payload.get("upper_sensor") or payload.get("main_sensor") or payload.get("sensor"),
        "lower_sensor": payload.get("lower_sensor") or payload.get("source_sensor"),
        "motor": payload.get("motor"),
        "mode": payload.get("mode"),
        "registered_account": False,
        "account_active": False,
        "cloud_feed_enabled": False,
        "email": None,
        "service_updates_enabled": True,
        "marketing_emails_enabled": False,
    }


def build_admin_device_summary(available_devices):
    resolve_admin_expired_alerts()
    alert_device_ids = fetch_active_alert_device_ids(updated_since=admin_alert_cutoff_timestamp())
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
    resolve_admin_expired_alerts()
    alert_cutoff = admin_alert_cutoff_timestamp()
    return render_template(
        "admin_customers.html",
        accounts=accounts,
        available_devices=available_devices,
        error=error,
        success=success,
        search_query=search_query,
        device_summary=device_summary or build_admin_device_summary(available_devices),
        global_alerts=fetch_filtered_alerts(limit=10, updated_since=alert_cutoff),
        persistence_warnings=auth_persistence_warnings(),
        latest_android_release=fetch_latest_android_app_release(),
        latest_global_firmware_artifact=fetch_latest_firmware_artifact(GLOBAL_FIRMWARE_TARGET),
    )


def build_admin_known_devices(accounts, available_devices, include_registered_devices=False, seed_configuration=False):
    if seed_configuration:
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
        entry["email"] = account.get("email")
        entry["registered_account"] = True
        entry["account_active"] = int(account.get("active", 1) or 0) == 1
        entry["cloud_feed_enabled"] = int(account.get("cloud_feed_enabled", 1) or 0) == 1
        entry["service_updates_enabled"] = int(account.get("service_updates_enabled", 1) or 0) == 1
        entry["marketing_emails_enabled"] = int(account.get("marketing_emails_enabled", 0) or 0) == 1
        accounts_by_device[normalized_device_id] = account

    if include_registered_devices:
        for device_id in sorted(DEVICE_KEY_MAP.keys()):
            normalized_device_id = normalize_device_id(device_id)
            if not normalized_device_id or normalized_device_id in ignored_device_ids:
                continue
            entry = merged.get(normalized_device_id)
            if entry is None:
                entry = build_admin_device_entry(normalized_device_id)
                merged[normalized_device_id] = entry
            entry["server_registered"] = True

        for device_id in list_registered_device_ids(limit=200):
            normalized_device_id = normalize_device_id(device_id)
            if not normalized_device_id or normalized_device_id in ignored_device_ids:
                continue

            entry = merged.get(normalized_device_id)
            if entry is None:
                entry = build_admin_device_entry(normalized_device_id)
                merged[normalized_device_id] = entry

            entry["server_registered"] = True

    refresh_operational_alerts_for_devices(merged.keys())
    for entry in merged.values():
        refresh_admin_entry_alerts(entry)
    service_configs = list_device_service_configs(merged.keys(), accounts_by_device=accounts_by_device)
    alert_summaries = fetch_active_alert_summaries(
        merged.keys(),
        updated_since=admin_alert_cutoff_timestamp(),
    )
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
        entry.update(admin_node_status_fields(entry, service_config))
        entry.update(admin_relay_sensor_status_fields(entry, service_config))
        entry["latest_firmware_artifact"] = fetch_latest_firmware_artifact(device_id)
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
            for value in (
                item.get("device_id"),
                item.get("source_ip"),
                item.get("device_local_host"),
                item.get("device_local_url"),
                item.get("display_name"),
                item.get("email"),
            )
        )
        if normalized_query in haystack:
            filtered.append(item)
    return filtered


def load_admin_known_devices(accounts, inventory_limit=100):
    return build_admin_known_devices(
        accounts=accounts,
        available_devices=fetch_device_inventory(limit=inventory_limit),
        include_registered_devices=True,
        seed_configuration=True,
    )


def refresh_admin_entry_alerts(entry):
    if not snapshot_has_live_device_data(entry):
        return
    if not entry.get("last_sync_at"):
        return
    evaluate_snapshot_alerts(entry)


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
        matched_rule = fetch_auto_registered_device_auth_rule(normalized_device_id)
        if matched_rule:
            expected_hash = str(matched_rule.get("key_hash") or "")
            provided_hash = hash_device_api_key(device_key)
            if require_key and not hmac.compare_digest(provided_hash, expected_hash):
                logger.warning("Rejected auto-registered device auth for %s", normalized_device_id)
                return False, None, "invalid device credentials", 403

    if not matched_rule:
        if (
            AUTO_REGISTER_DEVICE_KEYS
            and require_key
            and device_id_allowed_for_auto_registration(normalized_device_id)
            and device_key_allowed_for_auto_registration(device_key)
        ):
            matched_rule = remember_auto_registered_device_key(
                normalized_device_id,
                device_key,
                remote_addr=remote_addr,
            )
        else:
            logger.warning("Rejected device auth for %s", normalized_device_id or "<missing>")
            return False, None, "invalid device credentials", 403

    if not matched_rule:
        logger.warning("Rejected device auth for %s", normalized_device_id or "<missing>")
        return False, None, "invalid device credentials", 403

    if "key_hash" in matched_rule:
        expected_hash = str(matched_rule.get("key_hash") or "")
        provided_hash = hash_device_api_key(device_key)
        if require_key and not hmac.compare_digest(provided_hash, expected_hash):
            logger.warning("Rejected auto-registered device auth for %s", normalized_device_id)
            return False, None, "invalid device credentials", 403
    elif require_key and not hmac.compare_digest(str(device_key or ""), str(matched_rule["key"] or "")):
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


def refresh_operational_alerts_for_devices(device_ids):
    refreshed = []
    seen_device_ids = set()
    for device_id in device_ids or []:
        normalized_device_id = normalize_device_id(device_id)
        if not normalized_device_id or normalized_device_id in seen_device_ids:
            continue
        seen_device_ids.add(normalized_device_id)
        snapshot = fetch_device_snapshot(normalized_device_id)
        if not snapshot_has_live_device_data(snapshot):
            continue
        evaluate_snapshot_alerts(snapshot)
        refreshed.append(snapshot)
    return refreshed


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
                "ML dependencies are unavailable. Install them with: pip install -r requirements.txt",
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


def build_unavailable_level_forecast_payload(device_id, reason_code, message, remediation=None):
    return build_unavailable_level_forecast_response_payload(
        normalize_device_id(device_id),
        reason_code,
        message,
        resolve_level_forecast_artifact_path(),
        remediation=remediation,
    )


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
            "ML dependencies are unavailable. Install them with: pip install -r requirements.txt",
        ) from exc
    except ImportError as exc:
        raise RuntimeError(f"ML forecasting helpers could not be imported: {exc}") from exc

    with get_db() as db:
        raw = query_device_forecast_rows(db, normalized_device_id, device_source=get_device_source_mode())
    prediction = predict_latest_level(raw, artifact)

    return build_level_forecast_response_payload(
        normalized_device_id,
        prediction,
        artifact,
        artifact_path,
        artifact_updated_at,
        PROJECT_ROOT,
        format_timestamp,
    )


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


def apply_device_status_aliases(cleaned):
    if not isinstance(cleaned, dict):
        return cleaned
    if not cleaned.get("device_local_url") and cleaned.get("device_ip_url"):
        cleaned["device_local_url"] = cleaned.get("device_ip_url")
    if cleaned.get("level") is None and cleaned.get("main_tank_level") is not None:
        cleaned["level"] = cleaned.get("main_tank_level")
    if cleaned.get("motor") is None and cleaned.get("pump") is not None:
        cleaned["motor"] = cleaned.get("pump")
    if cleaned.get("sensor") is None and cleaned.get("upper_sensor") is not None:
        cleaned["sensor"] = cleaned.get("upper_sensor")
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
    return {"main_bytes": 0, "wal_bytes": 0, "shm_bytes": 0, "total_bytes": 0}


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


def prune_tank_data_retention(cursor):
    cursor.execute(
        """
        DELETE FROM tank_data
        WHERE created_at < datetime('now', ?)
        """,
        (f"-{DATA_RETENTION_DAYS} day",),
    )
    return max(0, int(cursor.rowcount or 0))


def prune_retained_rows(cursor, device_id=None, latest_row_id=None):
    pruned = {}

    pruned["tank_data_retention"] = prune_tank_data_retention(cursor)

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
                SELECT capped_rows.id FROM (
                    SELECT id
                    FROM tank_data
                    WHERE COALESCE(device_id, '') = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                ) AS capped_rows
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
          AND id NOT IN (
              SELECT audit_keep.id FROM (
                  SELECT MAX(id) AS id
                  FROM ops_audit_log
                  GROUP BY COALESCE(device_id, '')
              ) AS audit_keep
          )
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
    return False


def process_telemetry_payload(data, source_ip=None, transport="http"):
    cleaned = sanitize_payload(dict(data or {}))
    cleaned.pop("device_key", None)
    apply_device_status_aliases(cleaned)
    apply_source_tank_aliases(cleaned)
    cleaned["device_source"] = normalize_device_source(cleaned.get("device_source"), default=DEVICE_SOURCE_REAL)

    mode = str(cleaned.get("mode", "AUTO")).upper()
    if mode not in {"AUTO", "MANUAL"}:
        mode = "AUTO"

    latest_row_id = None
    insert_values = (
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
        cleaned.get("local_firmware_upload_service"),
        cleaned.get("arch_id"),
        cleaned.get("node_role"),
        cleaned.get("device_type"),
        cleaned.get("direct_peer"),
        cleaned.get("direct_peer_remote_ip"),
        cleaned.get("direct_peer_last_packet_age_s"),
        cleaned.get("direct_peer_last_packet_bytes"),
    )
    placeholders = ",".join("?" for _ in insert_values)
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            f"""
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
                buzzer_service, led_display_service, local_firmware_upload_service,
                arch_id, node_role, device_type,
                direct_peer, direct_peer_remote_ip, direct_peer_last_packet_age_s,
                direct_peer_last_packet_bytes
            )
            VALUES ({placeholders})
            """,
            insert_values,
        )
        latest_row_id = cursor.lastrowid

    maybe_prune_retained_rows(
        device_id=cleaned.get("device_id"),
        latest_row_id=latest_row_id,
        force=True,
    )
    clear_runtime_caches(cleaned.get("device_id"))
    record_device_simulator_state(
        cleaned.get("device_id"),
        simulator_payload_enabled(cleaned),
        source=transport,
    )
    try:
        sync_device_events(device_id=cleaned.get("device_id"))
    except Exception as exc:
        logger.warning("Device event sync failed for %s: %s", cleaned.get("device_id") or "unknown device", exc)
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


def mysql_connection_config():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    configured_database = resolve_mysql_database_name(os.environ.get("MYSQL_DATABASE", ""))
    if database_url:
        parsed = urlparse(database_url)
        return {
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 3306,
            "user": parsed.username or "",
            "password": parsed.password or "",
            "database": resolve_mysql_database_name((parsed.path or "").lstrip("/") or configured_database),
        }
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1").strip() or "127.0.0.1",
        "port": env_int("MYSQL_PORT", 3306),
        "user": os.environ.get("MYSQL_USER", "").strip(),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": configured_database,
    }


def mysql_database_name_is_placeholder(value):
    normalized_value = str(value or "").strip().lower()
    return (
        not normalized_value
        or "replace-with" in normalized_value
        or "replace_with" in normalized_value
        or normalized_value in {"change-me", "database", "dbname"}
    )


def resolve_mysql_database_name(value=None):
    configured_database = str(value if value is not None else os.environ.get("MYSQL_DATABASE", "")).strip()
    if not mysql_database_name_is_placeholder(configured_database):
        return configured_database
    database_from_user = os.environ.get("MYSQL_USER", "").strip()
    if database_from_user:
        return database_from_user
    return ""


def quote_mysql_identifier(identifier):
    identifier = str(identifier or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_$]+", identifier):
        raise RuntimeError(f"Unsafe MySQL database name: {identifier!r}")
    return f"`{identifier.replace('`', '``')}`"


def ensure_mysql_database_exists(config):
    database_name = config["database"]
    if not database_name:
        return
    if os.environ.get("MYSQL_AUTO_CREATE_DATABASE", "true").strip().lower() in {"0", "false", "no", "off"}:
        return
    server_conn = pymysql.connect(
        host=config["host"],
        port=int(config["port"]),
        user=config["user"],
        password=config["password"],
        charset="utf8mb4",
        cursorclass=MySqlDictCursor,
        autocommit=True,
    )
    try:
        with server_conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {quote_mysql_identifier(database_name)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    finally:
        server_conn.close()


def normalize_mysql_interval_modifier(value):
    text = str(value or "").strip().lower()
    match = re.fullmatch(r"([+-]?\d+)\s*(day|days|second|seconds)", text)
    if not match:
        return None
    amount = int(match.group(1))
    unit = "DAY" if match.group(2).startswith("day") else "SECOND"
    return amount, unit


def translate_mysql_datetime_offsets(sql, params):
    source_params = list(params or ())
    consumed_indexes = set()

    def replace_param_offset(match):
        param_index = sql[:match.start()].count("?")
        if param_index >= len(source_params):
            return "UTC_TIMESTAMP()"
        consumed_indexes.add(param_index)
        modifier = normalize_mysql_interval_modifier(source_params[param_index])
        if modifier is None:
            return "UTC_TIMESTAMP()"
        amount, unit = modifier
        return f"DATE_ADD(UTC_TIMESTAMP(), INTERVAL {amount} {unit})"

    sql = re.sub(r"datetime\s*\(\s*'now'\s*,\s*\?\s*\)", replace_param_offset, sql, flags=re.I)

    def replace_literal_offset(match):
        modifier = normalize_mysql_interval_modifier(match.group(1))
        if modifier is None:
            return "UTC_TIMESTAMP()"
        amount, unit = modifier
        return f"DATE_ADD(UTC_TIMESTAMP(), INTERVAL {amount} {unit})"

    sql = re.sub(r"datetime\s*\(\s*'now'\s*,\s*'([^']+)'\s*\)", replace_literal_offset, sql, flags=re.I)
    next_params = [value for index, value in enumerate(source_params) if index not in consumed_indexes]
    return sql, tuple(next_params)


def translate_mysql_schema_sql(sql):
    replacements = {
        "INTEGER PRIMARY KEY AUTOINCREMENT": "BIGINT PRIMARY KEY AUTO_INCREMENT",
        "key TEXT PRIMARY KEY": "`key` VARCHAR(191) PRIMARY KEY",
        "device_id TEXT PRIMARY KEY": "device_id VARCHAR(191) PRIMARY KEY",
        "device_id TEXT": "device_id VARCHAR(191)",
        "email TEXT": "email VARCHAR(255)",
        "target_device TEXT NOT NULL": "target_device VARCHAR(191) NOT NULL",
        "token_hash TEXT NOT NULL UNIQUE": "token_hash VARCHAR(128) NOT NULL UNIQUE",
        "expires_at TEXT NOT NULL": "expires_at DATETIME NOT NULL",
        "target_id TEXT": "target_id VARCHAR(191)",
        "key_rule TEXT": "key_rule VARCHAR(191)",
        "command TEXT NOT NULL": "command VARCHAR(64) NOT NULL",
        "event_key TEXT NOT NULL UNIQUE": "event_key VARCHAR(191) NOT NULL UNIQUE",
        "event_kind TEXT NOT NULL": "event_kind VARCHAR(96) NOT NULL",
        "source_table TEXT": "source_table VARCHAR(96)",
        "source_row_id TEXT": "source_row_id VARCHAR(191)",
        "event_at TEXT NOT NULL": "event_at DATETIME NOT NULL",
        "started_at TEXT": "started_at DATETIME",
        "ended_at TEXT": "ended_at DATETIME",
        "details_json TEXT": "details_json JSON",
        "kind TEXT NOT NULL": "kind VARCHAR(96) NOT NULL",
        "severity TEXT NOT NULL": "severity VARCHAR(32) NOT NULL",
        "actor TEXT NOT NULL": "actor VARCHAR(191) NOT NULL",
        "action TEXT NOT NULL": "action VARCHAR(191) NOT NULL",
        "target_type TEXT NOT NULL": "target_type VARCHAR(96) NOT NULL",
        "registration_source TEXT NOT NULL": "registration_source VARCHAR(191) NOT NULL",
        "cloud_feed_mode TEXT NOT NULL": "cloud_feed_mode VARCHAR(32) NOT NULL",
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP": "created_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "updated_at TEXT DEFAULT CURRENT_TIMESTAMP": "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP": "first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP": "last_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP",
        "resolved_at TEXT": "resolved_at DATETIME",
        "delivered_at TEXT": "delivered_at DATETIME",
        "used_at TEXT": "used_at DATETIME",
        "next_attempt_at TEXT": "next_attempt_at DATETIME",
        "uploaded_by TEXT": "uploaded_by VARCHAR(191)",
        "original_filename TEXT NOT NULL": "original_filename VARCHAR(255) NOT NULL",
        "stored_filename TEXT NOT NULL": "stored_filename VARCHAR(255) NOT NULL",
        "version_name TEXT NOT NULL": "version_name VARCHAR(96) NOT NULL",
        "version_label TEXT": "version_label VARCHAR(96)",
        "md5 TEXT NOT NULL": "md5 VARCHAR(64) NOT NULL",
        "content_type TEXT": "content_type VARCHAR(128)",
        "apk_blob BLOB": "apk_blob LONGBLOB",
    }
    for old, new in replacements.items():
        sql = sql.replace(old, new)
    return sql


def translate_mysql_upsert_sql(sql):
    conflict_match = re.search(r"\s+ON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+NOTHING\s*$", sql, flags=re.I | re.S)
    if conflict_match:
        sql = re.sub(r"^\s*INSERT\s+INTO", "INSERT IGNORE INTO", sql, flags=re.I)
        return sql[:conflict_match.start()]

    conflict_match = re.search(r"\s+ON\s+CONFLICT\s*\(([^)]+)\)\s+DO\s+UPDATE\s+SET\s+(.+?)\s*$", sql, flags=re.I | re.S)
    if not conflict_match:
        return sql

    update_clause = conflict_match.group(2)
    update_clause = re.sub(r"excluded\.([A-Za-z_][A-Za-z0-9_]*)", r"VALUES(\1)", update_clause)
    return f"{sql[:conflict_match.start()]} ON DUPLICATE KEY UPDATE {update_clause}"


def translate_mysql_query(sql, params=None):
    sql, params = translate_mysql_datetime_offsets(sql, params or ())
    sql = translate_mysql_schema_sql(sql)
    sql = translate_mysql_upsert_sql(sql)
    sql = sql.replace("app_settings(key", "app_settings(`key`")
    sql = re.sub(r"\bWHERE\s+key\s*=", "WHERE `key` =", sql, flags=re.I)
    sql = re.sub(r"\bLIMIT\s+-1\s+OFFSET\b", "LIMIT 18446744073709551615 OFFSET", sql, flags=re.I)
    sql = sql.replace("?", "%s")
    return sql, tuple(params or ())


class DbRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def adapt_mysql_row(row):
    if row is None or isinstance(row, tuple):
        return row
    return DbRow(row)


class MySqlCursorAdapter:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = None
        self.rowcount = -1
        self._buffered_rows = None

    def execute(self, sql, params=None):
        table_info_match = re.match(r"\s*PRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*$", sql, flags=re.I)
        if table_info_match:
            table_name = table_info_match.group(1)
            self.cursor.execute(
                """
                SELECT COLUMN_NAME, COLUMN_DEFAULT, IS_NULLABLE, COLUMN_KEY, DATA_TYPE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (table_name,),
            )
            self._buffered_rows = [
                (index, row["COLUMN_NAME"], row["DATA_TYPE"], 0 if row["IS_NULLABLE"] == "YES" else 1, row["COLUMN_DEFAULT"], 1 if row["COLUMN_KEY"] == "PRI" else 0)
                for index, row in enumerate(self.cursor.fetchall())
            ]
            self.rowcount = len(self._buffered_rows)
            return self
        self._buffered_rows = None
        translated_sql, translated_params = translate_mysql_query(sql, params)
        self.cursor.execute(translated_sql, translated_params)
        self.lastrowid = self.cursor.lastrowid
        self.rowcount = self.cursor.rowcount
        return self

    def fetchone(self):
        if self._buffered_rows is not None:
            return self._buffered_rows.pop(0) if self._buffered_rows else None
        return adapt_mysql_row(self.cursor.fetchone())

    def fetchall(self):
        if self._buffered_rows is not None:
            rows = self._buffered_rows
            self._buffered_rows = []
            return rows
        return [adapt_mysql_row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        if self._buffered_rows is not None:
            return iter(self._buffered_rows)
        return iter(adapt_mysql_row(row) for row in self.cursor)


class MySqlConnectionAdapter:
    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return MySqlCursorAdapter(self.connection.cursor())

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        return self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()
        return False


def connect_mysql():
    if pymysql is None:
        raise RuntimeError("DB_BACKEND=mysql requires PyMySQL. Install requirements.txt first.")
    config = mysql_connection_config()
    if not config["user"] or not config["database"]:
        raise RuntimeError("MySQL requires MYSQL_USER plus MYSQL_DATABASE, or a DATABASE_URL.")
    ssl_ca = os.environ.get("MYSQL_SSL_CA", "").strip()
    connect_kwargs = {
        "host": config["host"],
        "port": int(config["port"]),
        "user": config["user"],
        "password": config["password"],
        "database": config["database"],
        "charset": "utf8mb4",
        "cursorclass": MySqlDictCursor,
        "autocommit": False,
        "ssl": {"ca": ssl_ca} if ssl_ca else None,
    }
    try:
        conn = pymysql.connect(**connect_kwargs)
    except Exception as exc:
        error_code = getattr(exc, "args", [None])[0]
        if error_code != 1049:
            raise RuntimeError(
                "MySQL is required but the server is not reachable or credentials are invalid. "
                f"Check MYSQL_HOST={config['host']!r}, MYSQL_PORT={config['port']}, "
                f"MYSQL_USER={config['user']!r}, and make sure the MySQL service is running."
            ) from exc
        logger.info("MySQL database %s does not exist; attempting to create it.", config["database"])
        try:
            ensure_mysql_database_exists(config)
        except Exception as create_exc:
            raise RuntimeError(
                "MySQL database could not be created automatically. In cPanel, create "
                f"database {config['database']!r}, assign user {config['user']!r} to it, "
                "then restart the app."
            ) from create_exc
        try:
            conn = pymysql.connect(**connect_kwargs)
        except Exception as reconnect_exc:
            raise RuntimeError(
                "MySQL database was created or already exists, but Flask still could not connect. "
                "Check MYSQL_* credentials and database-user permissions."
            ) from reconnect_exc
    with conn.cursor() as cursor:
        cursor.execute("SET time_zone = '+00:00'")
    return MySqlConnectionAdapter(conn)


def get_db():
    return connect_mysql()


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
        "local_firmware_upload_service": "TEXT",
        "arch_id": "TEXT",
        "node_role": "TEXT",
        "device_type": "TEXT",
        "direct_peer": "TEXT",
        "direct_peer_remote_ip": "TEXT",
        "direct_peer_last_packet_age_s": "INTEGER",
        "direct_peer_last_packet_bytes": "INTEGER",
    }

    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE tank_data ADD COLUMN {column} {definition}")


def ensure_tank_data_mysql_column_types(cursor):
    for column in ("runtime", "current_runtime", "last_runtime", "fill_time"):
        cursor.execute(f"ALTER TABLE tank_data MODIFY COLUMN {column} TEXT")


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
                    runtime TEXT,
                    current_runtime TEXT,
                    last_runtime TEXT,
                    fill_time TEXT,
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
            local_firmware_upload_service TEXT,
            arch_id TEXT,
            node_role TEXT,
            device_type TEXT,
            direct_peer TEXT,
            direct_peer_remote_ip TEXT,
            direct_peer_last_packet_age_s INTEGER,
            direct_peer_last_packet_bytes INTEGER,
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


def ensure_firmware_artifacts_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS firmware_artifacts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_device TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            version_label TEXT,
            notes TEXT,
            md5 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            content_type TEXT,
            uploaded_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_android_app_releases_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS android_app_releases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_filename TEXT NOT NULL,
            stored_filename TEXT NOT NULL,
            version_name TEXT NOT NULL,
            version_code INTEGER NOT NULL,
            notes TEXT,
            md5 TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            content_type TEXT,
            apk_blob BLOB,
            uploaded_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_android_app_releases_columns(cursor):
    cursor.execute("PRAGMA table_info(android_app_releases)")
    existing = {row[1] for row in cursor.fetchall()}
    optional_columns = {
        "apk_blob": "BLOB",
    }
    for column, definition in optional_columns.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE android_app_releases ADD COLUMN {column} {definition}")


def ensure_android_app_releases_mysql_column_types(cursor):
    if USING_MYSQL:
        cursor.execute("ALTER TABLE android_app_releases MODIFY COLUMN apk_blob LONGBLOB")


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


def ensure_device_events_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS device_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key TEXT NOT NULL UNIQUE,
            device_id TEXT,
            event_kind TEXT NOT NULL,
            severity TEXT NOT NULL,
            message TEXT NOT NULL,
            details_json TEXT,
            source_table TEXT,
            source_row_id TEXT,
            started_at TEXT,
            ended_at TEXT,
            duration_seconds INTEGER,
            event_at TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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
            email TEXT,
            password_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            cloud_feed_enabled INTEGER NOT NULL DEFAULT 1,
            service_updates_enabled INTEGER NOT NULL DEFAULT 1,
            marketing_emails_enabled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_customer_accounts_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(customer_accounts)").fetchall()}
    required = {
        "display_name": "TEXT",
        "email": "TEXT",
        "active": "INTEGER NOT NULL DEFAULT 1",
        "cloud_feed_enabled": "INTEGER NOT NULL DEFAULT 1",
        "service_updates_enabled": "INTEGER NOT NULL DEFAULT 1",
        "marketing_emails_enabled": "INTEGER NOT NULL DEFAULT 0",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE customer_accounts ADD COLUMN {column} {definition}")


def ensure_customer_password_reset_tokens_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_password_reset_tokens(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_device_service_configs_table(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS device_service_configs(
            device_id TEXT PRIMARY KEY,
            main_sensor_enabled INTEGER NOT NULL DEFAULT 1,
            slave_device_enabled INTEGER NOT NULL DEFAULT 1,
            source_tank_monitoring_enabled INTEGER NOT NULL DEFAULT 1,
            ai_analysis_enabled INTEGER NOT NULL DEFAULT 1,
            cloud_feed_mode TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}',
            ota_enabled INTEGER NOT NULL DEFAULT 0,
            local_firmware_upload_enabled INTEGER NOT NULL DEFAULT 0,
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
        "main_sensor_enabled": "INTEGER NOT NULL DEFAULT 1",
        "slave_device_enabled": "INTEGER NOT NULL DEFAULT 1",
        "source_tank_monitoring_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_analysis_enabled": "INTEGER NOT NULL DEFAULT 1",
        "cloud_feed_mode": f"TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}'",
        "ota_enabled": "INTEGER NOT NULL DEFAULT 0",
        "local_firmware_upload_enabled": "INTEGER NOT NULL DEFAULT 0",
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


def ensure_device_auth_keys_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS device_auth_keys(
            device_id TEXT PRIMARY KEY,
            device_key_hash TEXT NOT NULL,
            registration_source TEXT NOT NULL,
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
        email = normalize_customer_email(item.get("email")) if item.get("email") else None
        active = 1 if int(item.get("active", 1) or 0) == 1 else 0
        cloud_feed_enabled = 1 if int(item.get("cloud_feed_enabled", 1) or 0) == 1 else 0
        service_updates_enabled = 1 if int(item.get("service_updates_enabled", 1) or 0) == 1 else 0
        marketing_emails_enabled = 1 if int(item.get("marketing_emails_enabled", 0) or 0) == 1 else 0
        if not normalized_device_id or not password_hash:
            continue
        cursor.execute(
            """
            INSERT INTO customer_accounts(
                device_id, display_name, email, password_hash, active, cloud_feed_enabled,
                service_updates_enabled, marketing_emails_enabled, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO NOTHING
            """,
            (
                normalized_device_id,
                display_name,
                email,
                password_hash,
                active,
                cloud_feed_enabled,
                service_updates_enabled,
                marketing_emails_enabled,
            ),
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
            INSERT INTO customer_accounts(device_id, display_name, password_hash, active, cloud_feed_enabled, service_updates_enabled, marketing_emails_enabled, updated_at)
            VALUES (?, ?, ?, 1, 1, 1, 0, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO NOTHING
            """,
            (device_id, display_name, password_hash),
        )


def init_db():
    logger.info("Initializing MySQL database schema")
    with get_db() as db:
        cursor = db.cursor()
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
                buzzer_service TEXT,
                led_display_service TEXT,
                local_firmware_upload_service TEXT,
                arch_id TEXT,
                node_role TEXT,
                device_type TEXT,
                direct_peer TEXT,
                direct_peer_remote_ip TEXT,
                direct_peer_last_packet_age_s INTEGER,
                direct_peer_last_packet_bytes INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        ensure_tank_data_columns(cursor)
        ensure_tank_data_mysql_column_types(cursor)
        ensure_relay_queue_table(cursor)
        ensure_device_command_queue_table(cursor)
        ensure_firmware_artifacts_table(cursor)
        ensure_android_app_releases_table(cursor)
        ensure_android_app_releases_columns(cursor)
        ensure_android_app_releases_mysql_column_types(cursor)
        ensure_alerts_table(cursor)
        ensure_audit_table(cursor)
        ensure_device_events_table(cursor)
        ensure_app_settings_table(cursor)
        seed_bootstrap_dashboard_password(cursor)
        ensure_customer_accounts_table(cursor)
        ensure_customer_accounts_columns(cursor)
        ensure_customer_password_reset_tokens_table(cursor)
        ensure_device_service_configs_table(cursor)
        ensure_device_service_configs_columns(cursor)
        ensure_registered_devices_table(cursor)
        ensure_device_auth_keys_table(cursor)
        ensure_ignored_devices_table(cursor)
        seed_bootstrap_customer_accounts(cursor)
        seed_default_customer_accounts(cursor)
        for statement in (
            "CREATE INDEX idx_created_at ON tank_data(created_at)",
            "CREATE INDEX idx_tank_data_device_created ON tank_data(device_id, created_at DESC, id DESC)",
            "CREATE INDEX idx_alerts_active ON ops_alerts(active, kind, device_id)",
            "CREATE INDEX idx_device_events_device_event_at ON device_events(device_id, event_at DESC, id DESC)",
            "CREATE INDEX idx_device_events_kind_event_at ON device_events(event_kind, event_at DESC)",
            "CREATE INDEX idx_device_command_queue_target_pending ON device_command_queue(target_device, delivered_at, id DESC)",
            "CREATE INDEX idx_firmware_artifacts_target_created ON firmware_artifacts(target_device, created_at DESC, id DESC)",
            "CREATE INDEX idx_android_app_releases_created ON android_app_releases(created_at DESC, id DESC)",
            "CREATE INDEX idx_audit_device_created ON ops_audit_log(device_id, created_at)",
            "CREATE INDEX idx_registered_devices_last_seen ON registered_devices(last_seen_at, device_id)",
            "CREATE INDEX idx_device_auth_keys_updated ON device_auth_keys(updated_at, device_id)",
            "CREATE INDEX idx_device_service_configs_updated ON device_service_configs(updated_at, device_id)",
            "CREATE INDEX idx_ignored_devices_updated ON ignored_devices(updated_at, device_id)",
        ):
            try:
                cursor.execute(statement)
            except Exception as exc:
                if "duplicate" not in str(exc).lower():
                    raise

    maybe_reset_device_source_mode_on_boot()
    deleted_counts = purge_configured_virtual_device_records()
    if deleted_counts.get("device_ids"):
        logger.info(
            "Purged %s configured virtual devices from startup database state.",
            deleted_counts["device_ids"],
        )
    logger.info("MySQL database initialization complete")


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


def build_auth_marker(*parts):
    payload = "||".join(str(part or "") for part in parts).encode("utf-8")
    secret = str(app.secret_key or "").encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def current_dashboard_auth_marker():
    stored_value = get_app_setting("dashboard_password")
    password_state = f"stored:{stored_value}" if stored_value is not None else f"env:{LOGIN_PASSWORD}"
    return build_auth_marker("admin", LOGIN_USERNAME, password_state)


def customer_auth_marker(device_id, account=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    resolved_account = account
    if normalize_device_id((resolved_account or {}).get("device_id")) != normalized_device_id:
        resolved_account = fetch_customer_account(normalized_device_id)
    if not resolved_account or int(resolved_account.get("active", 0) or 0) != 1:
        return None
    password_hash = str(resolved_account.get("password_hash") or "").strip()
    if not password_hash:
        return None
    return build_auth_marker("customer", normalized_device_id, password_hash)


def current_auth_marker_for_identity(role, username=None, device_id=None, account=None):
    resolved_role = str(role or "").strip()
    resolved_username = str(username or "").strip()
    resolved_device_id = normalize_device_id(device_id or resolved_username)
    if resolved_role == "admin" and resolved_username == LOGIN_USERNAME:
        return current_dashboard_auth_marker()
    if resolved_role == "customer":
        return customer_auth_marker(resolved_device_id, account=account)
    return None


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


def normalize_customer_email(value):
    email = str(value or "").strip().lower()
    if not email:
        return None
    if len(email) > 254 or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError("Enter a valid customer email address.")
    local, domain = email.rsplit("@", 1)
    if not local or "." not in domain or any(ch.isspace() for ch in email):
        raise ValueError("Enter a valid customer email address.")
    return email


def form_flag(name, default=False):
    if name not in request.form:
        return 1 if default else 0
    return 1 if str(request.form.get(name) or "").strip().lower() in {"1", "true", "yes", "on"} else 0


def utc_timestamp(delta=None):
    value = datetime.now(timezone.utc) + (delta or timedelta())
    return value.strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def fetch_customer_account(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, display_name, email, password_hash, active, cloud_feed_enabled,
                   service_updates_enabled, marketing_emails_enabled, created_at, updated_at
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
            SELECT device_id, display_name, email, active, cloud_feed_enabled,
                   service_updates_enabled, marketing_emails_enabled, created_at, updated_at
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


def fetch_customer_account_by_email(email):
    normalized_email = normalize_customer_email(email)
    if not normalized_email:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, display_name, email, password_hash, active, cloud_feed_enabled,
                   service_updates_enabled, marketing_emails_enabled, created_at, updated_at
            FROM customer_accounts
            WHERE lower(email) = lower(?)
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (normalized_email,),
        ).fetchone()
    return dict(row) if row else None


def smtp_email_configured():
    return bool(SMTP_HOST and (not SMTP_USERNAME or SMTP_PASSWORD))


def send_customer_email(to_email, subject, body, category="transactional", account=None, reply_to=None):
    normalized_email = normalize_customer_email(to_email)
    if not normalized_email:
        return False
    normalized_reply_to = normalize_customer_email(reply_to) if reply_to else None
    if category in {"updates", "service_update"} and account and int(account.get("service_updates_enabled", 1) or 0) != 1:
        logger.info("Customer service update email skipped because consent is off for %s", account.get("device_id"))
        return False
    if category in {"marketing", "advertisement", "offer"} and account and int(account.get("marketing_emails_enabled", 0) or 0) != 1:
        logger.info("Customer marketing email skipped because consent is off for %s", account.get("device_id"))
        return False

    if not smtp_email_configured():
        logger.info("SMTP is not fully configured. Customer email queued for %s: %s", normalized_email, subject)
        return False

    message = EmailMessage()
    from_header = f"{CUSTOMER_COMMUNICATION_FROM_NAME} <{CUSTOMER_COMMUNICATION_FROM_EMAIL}>" if CUSTOMER_COMMUNICATION_FROM_NAME else CUSTOMER_COMMUNICATION_FROM_EMAIL
    message["From"] = from_header
    message["To"] = normalized_email
    message["Subject"] = subject
    if normalized_reply_to:
        message["Reply-To"] = normalized_reply_to
    message.set_content(body)
    smtp_client = smtplib.SMTP_SSL if SMTP_USE_SSL else smtplib.SMTP
    with smtp_client(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            smtp.starttls()
        if SMTP_USERNAME:
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)
    return True


def upsert_customer_account(
    device_id,
    password,
    display_name=None,
    email=None,
    active=None,
    cloud_feed_enabled=None,
    service_updates_enabled=None,
    marketing_emails_enabled=None,
):
    normalized_device_id = normalize_device_id(device_id)
    normalized_display_name = str(display_name or "").strip()
    normalized_email = normalize_customer_email(email)
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
    resolved_service_updates_enabled = (
        1 if service_updates_enabled is None and not existing
        else (1 if int(service_updates_enabled if service_updates_enabled is not None else existing.get("service_updates_enabled", 1) or 0) == 1 else 0)
    )
    resolved_marketing_emails_enabled = (
        0 if marketing_emails_enabled is None and not existing
        else (1 if int(marketing_emails_enabled if marketing_emails_enabled is not None else existing.get("marketing_emails_enabled", 0) or 0) == 1 else 0)
    )
    with get_db() as db:
        db.execute("DELETE FROM ignored_devices WHERE device_id = ?", (normalized_device_id,))
        db.execute(
            """
            INSERT INTO customer_accounts(
                device_id, display_name, email, password_hash, active, cloud_feed_enabled,
                service_updates_enabled, marketing_emails_enabled, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                display_name=excluded.display_name,
                email=excluded.email,
                password_hash=excluded.password_hash,
                active=excluded.active,
                cloud_feed_enabled=excluded.cloud_feed_enabled,
                service_updates_enabled=excluded.service_updates_enabled,
                marketing_emails_enabled=excluded.marketing_emails_enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                normalized_device_id,
                normalized_display_name or None,
                normalized_email,
                password_hash,
                resolved_active,
                resolved_cloud_feed_enabled,
                resolved_service_updates_enabled,
                resolved_marketing_emails_enabled,
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
        email=account.get("email"),
        active=account.get("active", 1),
        cloud_feed_enabled=account.get("cloud_feed_enabled", 1),
        service_updates_enabled=account.get("service_updates_enabled", 1),
        marketing_emails_enabled=account.get("marketing_emails_enabled", 0),
    )


def update_customer_account_profile(
    device_id,
    display_name=None,
    email=None,
    active=None,
    cloud_feed_enabled=None,
    service_updates_enabled=None,
    marketing_emails_enabled=None,
):
    account = fetch_customer_account(device_id)
    if not account:
        raise ValueError("customer account not found")

    resolved_display_name = account.get("display_name") if display_name is None else (str(display_name).strip() or None)
    resolved_email = account.get("email") if email is None else normalize_customer_email(email)
    resolved_active = 1 if int(active if active is not None else account.get("active", 1) or 0) == 1 else 0
    resolved_cloud_feed_enabled = (
        1
        if int(cloud_feed_enabled if cloud_feed_enabled is not None else account.get("cloud_feed_enabled", 1) or 0) == 1
        else 0
    )
    resolved_service_updates_enabled = (
        1
        if int(service_updates_enabled if service_updates_enabled is not None else account.get("service_updates_enabled", 1) or 0) == 1
        else 0
    )
    resolved_marketing_emails_enabled = (
        1
        if int(marketing_emails_enabled if marketing_emails_enabled is not None else account.get("marketing_emails_enabled", 0) or 0) == 1
        else 0
    )

    with get_db() as db:
        db.execute(
            """
            UPDATE customer_accounts
            SET display_name = ?,
                email = ?,
                active = ?,
                cloud_feed_enabled = ?,
                service_updates_enabled = ?,
                marketing_emails_enabled = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE device_id = ?
            """,
            (
                resolved_display_name,
                resolved_email,
                resolved_active,
                resolved_cloud_feed_enabled,
                resolved_service_updates_enabled,
                resolved_marketing_emails_enabled,
                account["device_id"],
            ),
        )

    return fetch_customer_account(account["device_id"])


def create_customer_password_reset(account):
    token = secrets.token_urlsafe(32)
    token_hash = sha256_text(token)
    expires_at = utc_timestamp(timedelta(minutes=CUSTOMER_PASSWORD_RESET_TTL_MINUTES))
    with get_db() as db:
        db.execute(
            """
            UPDATE customer_password_reset_tokens
            SET used_at = CURRENT_TIMESTAMP
            WHERE device_id = ? AND used_at IS NULL
            """,
            (account["device_id"],),
        )
        db.execute(
            """
            INSERT INTO customer_password_reset_tokens(device_id, token_hash, expires_at)
            VALUES (?, ?, ?)
            """,
            (account["device_id"], token_hash, expires_at),
        )
    return token, expires_at


def fetch_customer_password_reset(token):
    token_hash = sha256_text(token)
    with get_db() as db:
        row = db.execute(
            """
            SELECT id, device_id, token_hash, expires_at, used_at, created_at
            FROM customer_password_reset_tokens
            WHERE token_hash = ?
            LIMIT 1
            """,
            (token_hash,),
        ).fetchone()
    return dict(row) if row else None


def customer_password_reset_valid(reset_row):
    if not reset_row or reset_row.get("used_at"):
        return False
    expires_at = str(reset_row.get("expires_at") or "")
    return bool(expires_at and expires_at > utc_timestamp())


def mark_customer_password_reset_used(reset_id):
    with get_db() as db:
        db.execute(
            "UPDATE customer_password_reset_tokens SET used_at = CURRENT_TIMESTAMP WHERE id = ?",
            (reset_id,),
        )


def send_customer_password_reset(account, token):
    reset_url = url_for("customer_reset_password", token=token, _external=True)
    subject = "Reset your SaleWell Smart Tank password"
    body = (
        f"Hello {account.get('display_name') or account['device_id']},\n\n"
        "We received a request to reset your SaleWell Smart Tank customer password.\n\n"
        f"Reset link: {reset_url}\n\n"
        f"This link expires in {CUSTOMER_PASSWORD_RESET_TTL_MINUTES} minutes. "
        "If you did not request this, you can ignore this email.\n\n"
        "SaleWell Smart Tank Support"
    )
    sent = send_customer_email(account["email"], subject, body, category="transactional", account=account)
    if not sent:
        logger.info("Customer password reset link for %s: %s", account["device_id"], reset_url)
    return sent


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
    main_sensor_enabled = boolish_enabled(payload.get("main_sensor_enabled"), default=True)
    slave_device_enabled = boolish_enabled(payload.get("slave_device_enabled"), default=True)
    source_tank_monitoring_enabled = boolish_enabled(payload.get("source_tank_monitoring_enabled"), default=True)
    ai_analysis_enabled = boolish_enabled(payload.get("ai_analysis_enabled"), default=True)
    ota_enabled = boolish_enabled(payload.get("ota_enabled"), default=False)
    local_firmware_upload_enabled = boolish_enabled(payload.get("local_firmware_upload_enabled"), default=False)
    buzzer_enabled = boolish_enabled(payload.get("buzzer_enabled"), default=True)
    led_display_enabled = boolish_enabled(payload.get("led_display_enabled"), default=True)
    effective_cloud_feed_enabled = cloud_feed_mode != DEVICE_SERVICE_CLOUD_FEED_OFF and account_cloud_feed_enabled
    effective_ai_analysis_enabled = (
        ai_analysis_enabled
        and cloud_feed_mode == DEVICE_SERVICE_CLOUD_FEED_FULL
        and effective_cloud_feed_enabled
    )
    hardware_enabled_count = int(ota_enabled) + int(local_firmware_upload_enabled) + int(buzzer_enabled) + int(led_display_enabled)
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
        "main_sensor_enabled": main_sensor_enabled,
        "slave_device_enabled": slave_device_enabled,
        "source_tank_monitoring_enabled": source_tank_monitoring_enabled,
        "ai_analysis_enabled": ai_analysis_enabled,
        "effective_ai_analysis_enabled": effective_ai_analysis_enabled,
        "cloud_feed_mode": cloud_feed_mode,
        "cloud_feed_mode_label": DEVICE_SERVICE_CLOUD_MODE_LABELS.get(cloud_feed_mode, "Unknown"),
        "cloud_feed_enabled": effective_cloud_feed_enabled,
        "cloud_note": cloud_note,
        "ota_enabled": ota_enabled,
        "local_firmware_upload_enabled": local_firmware_upload_enabled,
        "buzzer_enabled": buzzer_enabled,
        "led_display_enabled": led_display_enabled,
        "hardware_enabled_count": hardware_enabled_count,
        "hardware_enabled_label": f"{hardware_enabled_count}/4 device services active",
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
            "main_sensor_enabled": True,
            "slave_device_enabled": True,
            "source_tank_monitoring_enabled": True,
            "ai_analysis_enabled": True,
            "cloud_feed_mode": (
                DEVICE_SERVICE_CLOUD_FEED_FULL if cloud_feed_enabled else DEVICE_SERVICE_CLOUD_FEED_OFF
            ),
            "ota_enabled": False,
            "local_firmware_upload_enabled": False,
            "buzzer_enabled": True,
            "led_display_enabled": True,
        },
        account=account,
    )


def resolve_service_config_device_id(device_id=None, snapshot=None):
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        return normalized_device_id
    if snapshot:
        return normalize_device_id(snapshot.get("device_id"))
    return None


def resolve_device_service_config(device_id=None, account=None, snapshot=None):
    resolved_device_id = resolve_service_config_device_id(device_id, snapshot=snapshot)
    if not resolved_device_id:
        return default_device_service_config(device_id, account=account)
    return fetch_device_service_config(resolved_device_id, account=account)


def fetch_device_service_config(device_id, account=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return default_device_service_config(device_id, account=account)
    resolved_account = account if account is not None else fetch_customer_account(normalized_device_id)
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, main_sensor_enabled, slave_device_enabled,
                   source_tank_monitoring_enabled, ai_analysis_enabled,
                   cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
                   buzzer_enabled, led_display_enabled,
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
        SELECT device_id, main_sensor_enabled, slave_device_enabled,
               source_tank_monitoring_enabled, ai_analysis_enabled,
               cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
               buzzer_enabled, led_display_enabled,
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
    main_sensor_enabled=None,
    slave_device_enabled=None,
    source_tank_monitoring_enabled=None,
    ai_analysis_enabled=None,
    cloud_feed_mode=None,
    ota_enabled=None,
    local_firmware_upload_enabled=None,
    buzzer_enabled=None,
    led_display_enabled=None,
):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    account = fetch_customer_account(normalized_device_id)
    existing = fetch_device_service_config(normalized_device_id, account=account)
    resolved_main_sensor_enabled = boolish_enabled(
        main_sensor_enabled,
        default=existing.get("main_sensor_enabled", True),
    )
    resolved_slave_device_enabled = boolish_enabled(
        slave_device_enabled,
        default=existing.get("slave_device_enabled", True),
    )
    resolved_source_tank_monitoring_enabled = boolish_enabled(
        source_tank_monitoring_enabled,
        default=existing.get("source_tank_monitoring_enabled", True),
    )
    resolved_ai_analysis_enabled = boolish_enabled(
        ai_analysis_enabled,
        default=existing.get("ai_analysis_enabled", True),
    )
    resolved_ota_enabled = boolish_enabled(
        ota_enabled,
        default=existing.get("ota_enabled", False),
    )
    resolved_local_firmware_upload_enabled = boolish_enabled(
        local_firmware_upload_enabled,
        default=existing.get("local_firmware_upload_enabled", False),
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
                device_id, main_sensor_enabled, slave_device_enabled,
                source_tank_monitoring_enabled, ai_analysis_enabled,
                cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
                buzzer_enabled, led_display_enabled,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                main_sensor_enabled=excluded.main_sensor_enabled,
                slave_device_enabled=excluded.slave_device_enabled,
                source_tank_monitoring_enabled=excluded.source_tank_monitoring_enabled,
                ai_analysis_enabled=excluded.ai_analysis_enabled,
                cloud_feed_mode=excluded.cloud_feed_mode,
                ota_enabled=excluded.ota_enabled,
                local_firmware_upload_enabled=excluded.local_firmware_upload_enabled,
                buzzer_enabled=excluded.buzzer_enabled,
                led_display_enabled=excluded.led_display_enabled,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                normalized_device_id,
                1 if resolved_main_sensor_enabled else 0,
                1 if resolved_slave_device_enabled else 0,
                1 if resolved_source_tank_monitoring_enabled else 0,
                1 if resolved_ai_analysis_enabled else 0,
                resolved_cloud_feed_mode,
                1 if resolved_ota_enabled else 0,
                1 if resolved_local_firmware_upload_enabled else 0,
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
    slave_device_enabled = bool(config.get("slave_device_enabled", True))
    source_tank_enabled = bool(config.get("source_tank_monitoring_enabled"))
    return "SERVICECFG3:{slave}:{source}:{buzzer}:{led}:{ota}:{upload}".format(
        slave=1 if slave_device_enabled else 0,
        source=1 if source_tank_enabled else 0,
        buzzer=1 if bool(config.get("buzzer_enabled")) else 0,
        led=1 if bool(config.get("led_display_enabled")) else 0,
        ota=1 if bool(config.get("ota_enabled")) else 0,
        upload=1 if bool(config.get("local_firmware_upload_enabled")) else 0,
    )


def authenticate_dashboard_user(username, password):
    normalized_username = str(username or "").strip()
    if normalized_username == LOGIN_USERNAME and verify_dashboard_password(password):
        return {
            "role": "admin",
            "username": normalized_username,
            "device_id": None,
            "display_name": "Administrator",
            "auth_marker": current_dashboard_auth_marker(),
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
        "slave_device_enabled": service_config.get("slave_device_enabled", True),
        "source_tank_monitoring_enabled": service_config.get("source_tank_monitoring_enabled", True),
        "cloud_feed_enabled": service_config.get("cloud_feed_enabled", True),
        "cloud_feed_mode": service_config.get("cloud_feed_mode"),
        "ai_analysis_enabled": service_config.get("effective_ai_analysis_enabled", True),
        "auth_marker": current_auth_marker_for_identity("customer", device_id=normalized_username, account=customer),
    }



def issue_mobile_token(user):
    auth_marker = str(
        user.get("auth_marker")
        or current_auth_marker_for_identity(
            user.get("role"),
            username=user.get("username"),
            device_id=user.get("device_id"),
        )
        or ""
    ).strip()
    payload = {
        "role": user.get("role"),
        "username": user.get("username"),
        "device_id": normalize_device_id(user.get("device_id")),
        "auth_marker": auth_marker,
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
    token_auth_marker = str(payload.get("auth_marker") or "").strip()

    if not token_auth_marker:
        g.mobile_user = None
        return None

    if role == "admin" and username == LOGIN_USERNAME:
        current_auth_marker = current_dashboard_auth_marker()
        if not secrets.compare_digest(token_auth_marker, current_auth_marker):
            g.mobile_user = None
            return None
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
        current_auth_marker = current_auth_marker_for_identity("customer", device_id=device_id, account=customer)
        if not current_auth_marker or not secrets.compare_digest(token_auth_marker, current_auth_marker):
            g.mobile_user = None
            return None
        service_config = fetch_device_service_config(device_id, account=customer)
        user = {
            "role": "customer",
            "username": device_id,
            "device_id": device_id,
            "display_name": customer.get("display_name") or device_id,
            "slave_device_enabled": service_config.get("slave_device_enabled", True),
            "source_tank_monitoring_enabled": service_config.get("source_tank_monitoring_enabled", True),
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


def resolve_device_type_label(snapshot):
    data = snapshot or {}
    explicit_type = str(data.get("device_type") or "").strip()
    if explicit_type:
        return explicit_type

    arch_id = str(data.get("arch_id") or "").strip()
    node_role = str(data.get("node_role") or "").strip().lower()
    if arch_id and node_role:
        return f"architecture_{arch_id}_{node_role}"
    if arch_id:
        return f"architecture_{arch_id}"
    return "legacy_controller"


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
            ) recent_motor_rows
            ORDER BY id
        ) motor_transitions
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
            ) recent_leak_rows
            ORDER BY id
        ) leak_transitions
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
    data["simulator"] = str(data.get("simulator") or "OFF").strip().upper() or "OFF"
    if data["simulator"] not in {"ON", "OFF"}:
        data["simulator"] = "OFF"
    data["source_tank_simulator"] = str(data.get("source_tank_simulator") or "OFF").strip().upper() or "OFF"
    if data["source_tank_simulator"] not in {"ON", "OFF"}:
        data["source_tank_simulator"] = "OFF"
    data["device_source"] = normalize_device_source(data.get("device_source"), default=DEVICE_SOURCE_REAL)
    data["device_source_mode"] = get_device_source_mode()
    data["control_policy"] = CONTROL_POLICY
    data["arch_id"] = str(data.get("arch_id") or "").strip()
    data["node_role"] = str(data.get("node_role") or "").strip()
    data["device_type"] = resolve_device_type_label(data)
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
    data["local_firmware_upload_service"] = normalize_service_state(data.get("local_firmware_upload_service"))
    data["direct_peer"] = str(data.get("direct_peer") or "").strip().lower()
    data["direct_peer_remote_ip"] = str(data.get("direct_peer_remote_ip") or "").strip()
    for key in ("direct_peer_last_packet_age_s", "direct_peer_last_packet_bytes"):
        try:
            data[key] = int(data[key]) if data.get(key) not in (None, "", "null") else None
        except (TypeError, ValueError):
            data[key] = None
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

    payload = {
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
    payload["guidance"] = build_shared_guidance_payload(snapshot, payload)
    return payload


def meaningful_forecast_hours(snapshot, analytics_payload):
    insights = (analytics_payload or {}).get("insights") or {}
    forecast = insights.get("empty_prediction")
    if forecast in (None, "", "null"):
        return None
    try:
        forecast_hours = float(forecast)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(forecast_hours) or forecast_hours <= 0:
        return None

    level = safe_float((snapshot or {}).get("level"), 0)
    consumption_rate = safe_float(insights.get("consumption_rate"), 0)
    # A short extrapolated empty-time from noisy history should not override a
    # clearly full live tank. Keep the forecast actionable only near the working
    # range or when current consumption is extremely high.
    if level >= 90 and forecast_hours < 12 and consumption_rate < 20:
        return None
    if level >= 75 and forecast_hours < 6 and consumption_rate < 12:
        return None
    return round(forecast_hours, 2)


def build_shared_guidance_payload(snapshot=None, analytics_payload=None):
    snapshot = snapshot or {}
    analytics_payload = analytics_payload or {}
    insights = analytics_payload.get("insights") or {}
    comparison = analytics_payload.get("comparison") or {}

    level = safe_float(snapshot.get("level"), 0)
    motor = str(snapshot.get("motor") or "").upper()
    telemetry = str(snapshot.get("telemetry_status") or "no-data").lower()
    sensor = str(snapshot.get("sensor") or "").upper()
    source_service = str(snapshot.get("lower_tank_service") or "").upper()
    source_sensor = str(snapshot.get("lower_sensor") or "").upper()
    source_level_raw = snapshot.get("lower_tank_level")
    source_level = None if source_level_raw in (None, "", "null") else safe_float(source_level_raw, -1)
    effective_empty = meaningful_forecast_hours(snapshot, analytics_payload)
    usage_change = comparison.get("change_pct")
    try:
        usage_change = float(usage_change)
    except (TypeError, ValueError):
        usage_change = None

    pipe_leak = any(bool_flag(snapshot.get(key)) for key in ("pipe_leak", "slow_leak", "drip"))
    dry_run = bool_flag(snapshot.get("dry_run"))
    source_blocked = (
        source_service == "ON"
        and (source_level is None or source_level < 20 or (source_sensor and source_sensor != "OK"))
    )
    main_sensor_bad = bool(sensor and sensor != "OK")
    pump_running = motor == "ON"

    severity = "normal"
    tone = "ok"
    title = "Water system is stable"
    summary = "Tank level, pump state, and connection look steady right now."
    action_title = "No urgent action"
    action_note = "Auto protection is active and the tank trend looks manageable."
    observations = []
    actions = []

    if telemetry == "stale":
        severity = "warning"
        tone = "warn"
        title = "Live sync is delayed"
        summary = "Showing the last known device state until fresh telemetry arrives."
        action_title = "Check device power or Wi-Fi"
        action_note = "Restore live telemetry before relying on remote guidance."
        observations.append("The device has not reported fresh telemetry recently.")
        actions.append("Restore device power, signal, or Wi-Fi so live protection can update again.")
    elif dry_run:
        severity = "critical"
        tone = "bad"
        title = "Pump locked by source tank safety"
        summary = "Dry-run protection stopped the pump to protect the motor."
        action_title = "Check source water before starting"
        action_note = "Start the pump only after source water is available."
        observations.append("Dry-run protection is active.")
        actions.append("Check source water before starting the pump again.")
    elif pipe_leak:
        severity = "warning"
        tone = "warn"
        title = "Possible leak detected"
        summary = "Leak-related signals are active and need inspection."
        action_title = "Inspect pipes and fittings now"
        action_note = "Keep watching the trend after inspection to confirm it settles."
        observations.append("Leak-related alerts are active on the device.")
        actions.append("Check pipes, valves, and overflow points for unexpected water loss.")
    elif source_blocked:
        severity = "warning"
        tone = "warn"
        title = "Pump start is paused"
        summary = "Source tank protection is preventing an unsafe pump start."
        action_title = "Wait for source tank recovery"
        action_note = "The pump can start after the source sensor and level are safe."
        observations.append("Source tank monitoring is blocking pump start for safety.")
        actions.append("Wait until the source tank reading recovers before starting the pump.")
    elif level <= 20 or (effective_empty is not None and effective_empty <= 6):
        severity = "critical" if level <= 15 or (effective_empty is not None and effective_empty <= 3) else "warning"
        tone = "bad" if severity == "critical" else "warn"
        title = "Tank may empty soon" if effective_empty is not None else "Low water level"
        summary = (
            f"Estimated time left is {effective_empty:g} hrs at the current pace."
            if effective_empty is not None
            else "Main tank level is below the low-water threshold."
        )
        action_title = "Run the pump soon to avoid low water"
        action_note = "Auto protection still applies while the tank is topped up."
        observations.append(f"Main tank level is {level:.1f}%.")
        actions.append(action_title)
    elif pump_running:
        title = "Pump is filling the tank"
        summary = snapshot.get("fill_time") or "Water is being refilled now."
        action_title = "Let the cycle finish"
        action_note = "Auto protection still controls the stop point while the tank fills."
        observations.append("The pump is currently running and the tank is refilling.")
    elif usage_change is not None and usage_change >= 35:
        severity = "warning"
        tone = "warn"
        title = "Water use is higher than normal"
        summary = f"Usage is {usage_change:+.0f}% versus the previous day."
        action_title = "Check for extra use or leakage"
        action_note = "Look for taps, flush lines, or unusual draw before the tank drops further."
        observations.append(summary)
        actions.append("Monitor usage for the next few hours to confirm whether demand stays high.")
    elif main_sensor_bad:
        severity = "warning"
        tone = "warn"
        title = "Main tank sensor needs attention"
        summary = snapshot.get("sensor_info") or "The main tank sensor reading is not healthy."
        action_title = "Inspect the main tank sensor"
        action_note = "Clean wiring and sensor placement, then wait for the next telemetry sync."
        observations.append("The main tank sensor needs attention.")
        actions.append(action_title)
    else:
        observations.append("Tank level, pump state, and connection look steady right now.")
        actions.append("Keep auto protection enabled and review the chart trend later today.")

    motor_cycles = safe_float(insights.get("motor_cycles"), safe_float(snapshot.get("motor_cycles"), 0))
    if motor_cycles > 12:
        observations.append("Pump cycling is higher than normal.")
        actions.append("Review auto-start and auto-stop thresholds if the pump keeps short-cycling.")

    confidence = 92
    if telemetry == "stale":
        confidence -= 20
    if not analytics_payload or not (analytics_payload.get("levels") or {}).get("values"):
        confidence -= 10
    if main_sensor_bad:
        confidence -= 8
    confidence = max(45, min(96, confidence))

    return {
        "severity": severity,
        "tone": tone,
        "title": title,
        "summary": summary,
        "action_title": action_title,
        "action_note": action_note,
        "time_to_empty_hours": effective_empty,
        "confidence_percent": int(confidence),
        "observations": observations[:5],
        "actions": list(dict.fromkeys(actions))[:5],
        "source": "shared_server_guidance",
    }


def build_empty_snapshot_payload(device_id=None):
    payload = {
        "level": 0,
        "mode": "AUTO",
        "simulator": "OFF",
        "source_tank_simulator": "OFF",
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
        "local_firmware_upload_service": "UNKNOWN",
        "direct_peer": "",
        "direct_peer_remote_ip": "",
        "direct_peer_last_packet_age_s": None,
        "direct_peer_last_packet_bytes": None,
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
    if snapshot_has_live_device_data(snapshot):
        evaluate_snapshot_alerts(snapshot)
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
        "local_firmware_upload_service": snapshot.get("local_firmware_upload_service") if snapshot else "UNKNOWN",
        "uptime_label": snapshot.get("uptime_label") if snapshot else "--",
        "free_heap_label": snapshot.get("free_heap_label") if snapshot else "--",
        "active_alert_count": len(active_alerts),
        "active_alerts": active_alerts,
    }


def build_monitoring_summary_payload(snapshot, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    if snapshot_has_live_device_data(snapshot):
        evaluate_snapshot_alerts(snapshot)
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
            "local_firmware_upload_service": snapshot.get("local_firmware_upload_service") if snapshot else "UNKNOWN",
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
    mysql_config = mysql_connection_config()
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

    database_payload = {
        "backend": DB_BACKEND,
        "is_render": IS_RENDER,
        "host": mysql_config.get("host"),
        "port": mysql_config.get("port"),
        "database": mysql_config.get("database"),
        "user": mysql_config.get("user"),
        "ssl_ca_configured": bool(os.environ.get("MYSQL_SSL_CA", "").strip()),
    }

    return {
        "database": database_payload,
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
    if normalized_device_id:
        guidance_snapshot = fetch_device_snapshot(normalized_device_id) or latest_row
    else:
        guidance_snapshot = latest_row
    payload["guidance"] = build_shared_guidance_payload(guidance_snapshot, payload)

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


def build_generated_device_events(limit=12, device_id=None):
    if not TELEMETRY_HISTORY_ENABLED:
        return []

    normalized_device_id = normalize_device_id(device_id)
    source_clause, source_params = device_source_where_clause()
    query = """
        SELECT id, device_id, level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
               pump_failure, dry_run, sensor, wifi, wifi_rssi, firmware_version,
               reset_reason, free_heap, uptime_s, lower_tank_level, lower_sensor,
               channel_mode, telemetry_service, command_service, ota_service,
               lower_tank_service, buzzer_service, led_display_service,
               local_firmware_upload_service, tank_height_cm, tank_capacity_liters,
               created_at
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

    timeline = []
    previous = None
    pump_started_at = None
    pump_started_level = None
    active_flag_started_at = {}
    sensor_fault_started_at = None
    source_low_started_at = None
    weak_signal_threshold = -75
    recovered_signal_threshold = -70
    significant_rssi_delta = 10
    low_heap_threshold = 15000
    source_low_threshold = 20
    source_recovered_threshold = 35
    frequent_reboot_window_seconds = 24 * 60 * 60
    reboot_times = []
    wifi_issue_times = []
    config_fields = (
        ("channel_mode", "Channel mode"),
        ("telemetry_service", "Telemetry service"),
        ("command_service", "Command service"),
        ("ota_service", "OTA service"),
        ("lower_tank_service", "Source tank service"),
        ("buzzer_service", "Buzzer service"),
        ("led_display_service", "LED display service"),
        ("local_firmware_upload_service", "Local firmware upload service"),
        ("tank_height_cm", "Tank height"),
        ("tank_capacity_liters", "Tank capacity"),
    )

    def add_event(current, severity, message, kind, details=None):
        timeline.append(
            {
                "time": format_timestamp(current.get("created_at")),
                "severity": severity,
                "message": message,
                "kind": kind,
                "details": {
                    "source_table": "tank_data",
                    "source_row_id": current.get("id"),
                    "device_id": normalize_device_id(current.get("device_id")),
                    **(details or {}),
                },
            }
        )

    def duration_details(started_at, ended_at):
        if not started_at or not ended_at:
            return {}
        duration_seconds = max(0, int((ended_at - started_at).total_seconds()))
        return {
            "duration_seconds": duration_seconds,
            "duration_label": format_compact_uptime(duration_seconds),
        }

    for row in reversed(rows):
        current = dict(row)
        current_time = parse_timestamp(current.get("created_at"))
        previous_time = parse_timestamp(previous.get("created_at")) if previous else None
        level = safe_float(current.get("level"), 0)
        previous_level = safe_float(previous.get("level"), level) if previous else level
        source_level = safe_float(current.get("lower_tank_level"), None)
        previous_source_level = safe_float(previous.get("lower_tank_level"), None) if previous else None

        if previous is None:
            add_event(
                current,
                "success",
                "Device telemetry feed is active.",
                "telemetry_feed_active",
                {"level": round(level, 2)},
            )
        elif current_time and previous_time:
            gap_seconds = int((current_time - previous_time).total_seconds())
            if gap_seconds > STALE_AFTER_SECONDS:
                add_event(
                    current,
                    "success",
                    f"Device checked in after a {format_compact_uptime(gap_seconds)} telemetry gap.",
                    "telemetry_recovered",
                    {"gap_seconds": gap_seconds},
                )

        if previous is None or current.get("motor") != previous.get("motor"):
            if current.get("motor") == "ON":
                pump_started_at = current_time
                pump_started_level = level
                add_event(
                    current,
                    "info",
                    f"Pump started at {level:.1f}% tank level.",
                    "pump_started",
                    {"level": round(level, 2), "mode": current.get("mode")},
                )
            elif previous and previous.get("motor") == "ON":
                run_seconds = int((current_time - pump_started_at).total_seconds()) if current_time and pump_started_at else None
                level_delta = level - (pump_started_level if pump_started_level is not None else previous_level)
                add_event(
                    current,
                    "info",
                    f"Pump stopped at {level:.1f}% tank level after {format_compact_uptime(run_seconds) if run_seconds is not None else 'a run'}.",
                    "pump_stopped",
                    {
                        "level": round(level, 2),
                        "level_delta": round(level_delta, 2),
                        "run_seconds": run_seconds,
                        "level_rise_rate_pct_per_min": (
                            round((level_delta / max(run_seconds, 1)) * 60.0, 3)
                            if run_seconds is not None else None
                        ),
                        "mode": current.get("mode"),
                    },
                )
                if run_seconds is not None and run_seconds >= 30 and level_delta < 0.5:
                    add_event(
                        current,
                        "warning",
                        "Pump ran but tank level did not rise enough.",
                        "pump_no_level_rise",
                        {"run_seconds": run_seconds, "level_delta": round(level_delta, 2)},
                    )
                pump_started_at = None
                pump_started_level = None

        if previous is None or current.get("mode") != previous.get("mode"):
            add_event(
                current,
                "info",
                f"Mode changed to {current.get('mode', '--')}.",
                "mode_changed",
                {"mode": current.get("mode")},
            )

        if level <= 20 and (previous is None or safe_float(previous.get("level"), 100) > 20):
            add_event(current, "warning", "Tank dropped below 20%.", "tank_low", {"level": round(level, 2)})
        elif previous is not None and level > 20 and previous_level <= 20:
            add_event(current, "success", "Tank recovered above 20%.", "tank_low_recovered", {"level": round(level, 2)})

        if level >= 95 and (previous is None or safe_float(previous.get("level"), 0) < 95):
            add_event(current, "success", "Tank reached near-full level.", "tank_near_full", {"level": round(level, 2)})

        if source_level is not None:
            if source_level <= source_low_threshold and (
                previous_source_level is None or previous_source_level > source_low_threshold
            ):
                source_low_started_at = current_time
                add_event(
                    current,
                    "warning",
                    f"Source tank dropped below {source_low_threshold}%.",
                    "source_tank_low",
                    {"source_tank_level": round(source_level, 2)},
                )
            elif previous_source_level is not None and source_level >= source_recovered_threshold and previous_source_level <= source_low_threshold:
                add_event(
                    current,
                    "success",
                    f"Source tank recovered enough for safer pumping after {format_compact_uptime((current_time - source_low_started_at).total_seconds()) if current_time and source_low_started_at else 'a low-level period'}.",
                    "source_tank_recovered",
                    {"source_tank_level": round(source_level, 2), **duration_details(source_low_started_at, current_time)},
                )
                source_low_started_at = None

        for key, message in (
            ("pipe_leak", "Pipe leak warning detected."),
            ("slow_leak", "Slow leak pattern detected."),
            ("drip", "Drip alert detected."),
            ("abnormal", "Abnormal usage detected."),
            ("pump_failure", "Pump failure warning detected."),
            ("dry_run", "Dry-run protection activated."),
        ):
            if bool_flag(current.get(key)) and (previous is None or not bool_flag(previous.get(key))):
                active_flag_started_at[key] = current_time
                add_event(current, "warning", message, key, {"state": "active"})
            elif previous is not None and not bool_flag(current.get(key)) and bool_flag(previous.get(key)):
                duration = duration_details(active_flag_started_at.get(key), current_time)
                add_event(
                    current,
                    "success",
                    f"{message.removesuffix(' detected.').removesuffix(' activated.')} cleared"
                    f"{' after ' + duration['duration_label'] if duration.get('duration_label') else ''}.",
                    f"{key}_cleared",
                    {"state": "cleared", **duration},
                )
                active_flag_started_at.pop(key, None)

        if str(current.get("sensor", "")).upper() != "OK" and (previous is None or str(previous.get("sensor", "")).upper() == "OK"):
            sensor_fault_started_at = current_time
            add_event(
                current,
                "warning",
                "Main tank sensor requires attention.",
                "sensor_fault",
                {"sensor": current.get("sensor")},
            )
        elif previous is not None and str(current.get("sensor", "")).upper() == "OK" and str(previous.get("sensor", "")).upper() != "OK":
            duration = duration_details(sensor_fault_started_at, current_time)
            add_event(
                current,
                "success",
                f"Main tank sensor is reporting normally again"
                f"{' after ' + duration['duration_label'] if duration.get('duration_label') else ''}.",
                "sensor_recovered",
                duration,
            )
            sensor_fault_started_at = None

        if str(current.get("wifi", "")).upper() not in {"", "ONLINE", "OK", "CONNECTED"} and (
            previous is None or str(previous.get("wifi", "")).upper() in {"", "ONLINE", "OK", "CONNECTED"}
        ):
            if current_time:
                wifi_issue_times.append(current_time)
                wifi_issue_times = [
                    item
                    for item in wifi_issue_times
                    if (current_time - item).total_seconds() <= frequent_reboot_window_seconds
                ]
            add_event(
                current,
                "warning",
                "Device connectivity issue detected.",
                "wifi_issue",
                {"wifi": current.get("wifi"), "wifi_rssi": current.get("wifi_rssi")},
            )
            if len(wifi_issue_times) >= 3:
                add_event(
                    current,
                    "warning",
                    f"Device disconnected {len(wifi_issue_times)} times in the last 24 hours.",
                    "wifi_disconnect_frequency_high",
                    {"disconnect_count_24h": len(wifi_issue_times)},
                )
        elif previous is not None and str(current.get("wifi", "")).upper() in {"ONLINE", "OK", "CONNECTED"} and str(previous.get("wifi", "")).upper() not in {"", "ONLINE", "OK", "CONNECTED"}:
            add_event(current, "success", "Device connectivity recovered.", "wifi_recovered")

        rssi = safe_float(current.get("wifi_rssi"), None)
        previous_rssi = safe_float(previous.get("wifi_rssi"), None) if previous else None
        if rssi is not None:
            if rssi < weak_signal_threshold and (previous_rssi is None or previous_rssi >= weak_signal_threshold):
                add_event(
                    current,
                    "warning",
                    f"Wi-Fi signal became weak ({int(rssi)} dBm).",
                    "wifi_signal_weak",
                    {"wifi_rssi": int(rssi)},
                )
            elif previous_rssi is not None and rssi >= recovered_signal_threshold and previous_rssi < weak_signal_threshold:
                add_event(
                    current,
                    "success",
                    f"Wi-Fi signal recovered ({int(rssi)} dBm).",
                    "wifi_signal_recovered",
                    {"wifi_rssi": int(rssi)},
                )
            elif previous_rssi is not None and (previous_rssi - rssi) >= significant_rssi_delta:
                add_event(
                    current,
                    "warning",
                    f"Wi-Fi signal dropped from {int(previous_rssi)} to {int(rssi)} dBm.",
                    "wifi_signal_drop",
                    {"wifi_rssi": int(rssi), "previous_wifi_rssi": int(previous_rssi)},
                )
            elif previous_rssi is not None and (rssi - previous_rssi) >= significant_rssi_delta:
                add_event(
                    current,
                    "success",
                    f"Wi-Fi signal improved from {int(previous_rssi)} to {int(rssi)} dBm.",
                    "wifi_signal_improved",
                    {"wifi_rssi": int(rssi), "previous_wifi_rssi": int(previous_rssi)},
                )

        free_heap = safe_float(current.get("free_heap"), None)
        previous_free_heap = safe_float(previous.get("free_heap"), None) if previous else None
        if free_heap is not None:
            if free_heap < low_heap_threshold and (previous_free_heap is None or previous_free_heap >= low_heap_threshold):
                add_event(
                    current,
                    "warning",
                    f"Controller memory is low ({int(free_heap)} B free).",
                    "heap_low",
                    {"free_heap": int(free_heap)},
                )
            elif previous_free_heap is not None and free_heap >= low_heap_threshold and previous_free_heap < low_heap_threshold:
                add_event(
                    current,
                    "success",
                    f"Controller memory recovered ({int(free_heap)} B free).",
                    "heap_recovered",
                    {"free_heap": int(free_heap)},
                )

        if previous is not None and current.get("firmware_version") and current.get("firmware_version") != previous.get("firmware_version"):
            add_event(
                current,
                "info",
                f"Firmware updated to {current.get('firmware_version')}.",
                "firmware_changed",
                {"firmware_version": current.get("firmware_version"), "previous_firmware_version": previous.get("firmware_version")},
            )

        reset_reason = str(current.get("reset_reason") or "").strip()
        previous_reset_reason = str(previous.get("reset_reason") or "").strip() if previous else ""
        if reset_reason and reset_reason != previous_reset_reason:
            if current_time:
                reboot_times.append(current_time)
                reboot_times = [
                    item
                    for item in reboot_times
                    if (current_time - item).total_seconds() <= frequent_reboot_window_seconds
                ]
            add_event(
                current,
                "info",
                f"Controller rebooted ({reset_reason}).",
                "device_rebooted",
                {"reset_reason": reset_reason},
            )
            if len(reboot_times) >= 3:
                add_event(
                    current,
                    "warning",
                    f"Controller rebooted {len(reboot_times)} times in the last 24 hours.",
                    "reboot_frequency_high",
                    {"reboot_count_24h": len(reboot_times)},
                )

        if previous is not None:
            for field, label in config_fields:
                current_value = current.get(field)
                previous_value = previous.get(field)
                if current_value in (None, "") or current_value == previous_value:
                    continue
                add_event(
                    current,
                    "info",
                    f"{label} changed to {current_value}.",
                    "config_changed",
                    {"field": field, "value": current_value, "previous_value": previous_value},
                )

        previous = current

    timeline.extend(build_command_events(limit=40, device_id=normalized_device_id))
    timeline.extend(build_ota_events(limit=20, device_id=normalized_device_id))
    timeline.sort(key=lambda item: parse_timestamp(item.get("time")) or datetime.min, reverse=True)
    return timeline[:limit]


def build_command_events(limit=20, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT id, target_device, command, created_at, delivered_at
        FROM device_command_queue
        WHERE 1 = 1
    """
    params = []
    if normalized_device_id:
        query += " AND target_device = ?"
        params.append(normalized_device_id)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit or 20)))

    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    events = []
    now = now_utc()
    failure_after_seconds = max(300, STALE_AFTER_SECONDS * 2)
    for row in rows:
        command = str(row["command"] or "").strip().upper() or "COMMAND"
        target_device = normalize_device_id(row["target_device"])
        created_at = format_timestamp(row["created_at"])
        delivered_at = format_timestamp(row["delivered_at"])
        events.append(
            {
                "time": created_at,
                "severity": "info",
                "message": f"Command queued: {command}.",
                "kind": "command_queued",
                "details": {
                    "source_table": "device_command_queue",
                    "source_row_id": row["id"],
                    "command": command,
                    "device_id": target_device,
                    "command_id": row["id"],
                },
            }
        )
        if row["delivered_at"]:
            events.append(
                {
                    "time": delivered_at,
                    "severity": "success",
                    "message": f"Command acknowledged by device: {command}.",
                    "kind": "command_acknowledged",
                    "details": {
                        "source_table": "device_command_queue",
                        "source_row_id": row["id"],
                        "command": command,
                        "device_id": target_device,
                        "command_id": row["id"],
                    },
                }
            )
        else:
            created_dt = parse_timestamp(row["created_at"])
            age_seconds = int((now - created_dt).total_seconds()) if created_dt else None
            severity = "warning" if age_seconds is not None and age_seconds >= failure_after_seconds else "info"
            kind = "command_delivery_failed" if severity == "warning" else "command_delivery_pending"
            message = (
                f"Command has not been acknowledged yet: {command}."
                if severity == "warning"
                else f"Command waiting for device acknowledgement: {command}."
            )
            events.append(
                {
                    "time": created_at,
                    "severity": severity,
                    "message": message,
                    "kind": kind,
                    "details": {
                        "source_table": "device_command_queue",
                        "source_row_id": row["id"],
                        "command": command,
                        "device_id": target_device,
                        "command_id": row["id"],
                        "age_seconds": age_seconds,
                    },
                }
            )
    return events


def build_ota_events(limit=20, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT id, target_device, version_label, original_filename, created_at
        FROM firmware_artifacts
        WHERE target_device = ?
    """
    params = [GLOBAL_FIRMWARE_TARGET]
    if normalized_device_id:
        query += " OR target_device = ?"
        params.append(normalized_device_id)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit or 20)))

    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    latest_snapshot = fetch_device_snapshot(normalized_device_id) if normalized_device_id else None
    current_firmware = str((latest_snapshot or {}).get("firmware_version") or "").strip()
    events = []
    now = now_utc()
    ota_failure_after_seconds = 24 * 60 * 60
    for row in rows:
        version_label = str(row["version_label"] or row["original_filename"] or "firmware").strip()
        target_device = normalize_device_id(row["target_device"]) or GLOBAL_FIRMWARE_TARGET
        scoped = target_device != GLOBAL_FIRMWARE_TARGET
        target_label = target_device if scoped else "fleet"
        created_at = parse_timestamp(row["created_at"])
        age_seconds = int((now - created_at).total_seconds()) if created_at else None
        events.append(
            {
                "time": format_timestamp(row["created_at"]),
                "severity": "info",
                "message": f"Firmware update published for {target_label}: {version_label}.",
                "kind": "ota_published",
                "details": {
                    "source_table": "firmware_artifacts",
                    "source_row_id": row["id"],
                    "artifact_id": row["id"],
                    "target_device": target_device,
                    "version_label": version_label,
                },
            }
        )
        if normalized_device_id and version_label and current_firmware:
            if version_label == current_firmware or current_firmware in version_label or version_label in current_firmware:
                events.append(
                    {
                        "time": format_timestamp(row["created_at"]),
                        "severity": "success",
                        "message": f"Firmware version is active on device: {current_firmware}.",
                        "kind": "ota_succeeded",
                        "details": {
                            "source_table": "firmware_artifacts",
                            "source_row_id": row["id"],
                            "artifact_id": row["id"],
                            "target_device": normalized_device_id,
                            "version_label": version_label,
                            "firmware_version": current_firmware,
                        },
                    }
                )
            else:
                failed = age_seconds is not None and age_seconds >= ota_failure_after_seconds
                events.append(
                    {
                        "time": format_timestamp(row["created_at"]),
                        "severity": "warning" if failed else "info",
                        "message": (
                            f"Firmware update has not become active after {format_compact_uptime(age_seconds)}: {version_label}."
                            if failed
                            else f"Firmware update pending on device: {version_label}."
                        ),
                        "kind": "ota_failed" if failed else "ota_pending",
                        "details": {
                            "source_table": "firmware_artifacts",
                            "source_row_id": row["id"],
                            "artifact_id": row["id"],
                            "target_device": normalized_device_id,
                            "version_label": version_label,
                            "firmware_version": current_firmware,
                            "age_seconds": age_seconds,
                        },
                    }
                )
    return events


def normalize_device_event_time(value):
    parsed = parse_timestamp(value)
    return format_timestamp(parsed) if parsed else now_utc().strftime(TIMESTAMP_FORMAT)


def device_event_key(event, default_device_id=None):
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    device_id = normalize_device_id(details.get("device_id") or default_device_id) or ""
    source_table = str(details.get("source_table") or "").strip()
    source_row_id = str(details.get("source_row_id") or "").strip()
    event_kind = str(event.get("kind") or "event").strip().lower()
    event_at = normalize_device_event_time(event.get("time"))
    basis = "|".join(
        (
            device_id,
            event_kind,
            event_at,
            source_table,
            source_row_id,
            str(event.get("message") or "").strip(),
        )
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]


def persist_device_events(events, default_device_id=None):
    if not events:
        return 0

    persisted = 0
    with get_db() as db:
        for event in events:
            if not isinstance(event, dict):
                continue
            details = event.get("details") if isinstance(event.get("details"), dict) else {}
            event_at = normalize_device_event_time(event.get("time"))
            event_kind = str(event.get("kind") or "event").strip().lower() or "event"
            severity = str(event.get("severity") or "info").strip().lower() or "info"
            message = str(event.get("message") or event_kind.replace("_", " ").title()).strip()
            device_id = normalize_device_id(details.get("device_id") or default_device_id)
            duration_seconds = details.get("duration_seconds")
            try:
                duration_seconds = int(duration_seconds) if duration_seconds is not None else None
            except (TypeError, ValueError):
                duration_seconds = None
            event_key = device_event_key(event, default_device_id=device_id)
            db.execute(
                """
                INSERT INTO device_events(
                    event_key, device_id, event_kind, severity, message, details_json,
                    source_table, source_row_id, started_at, ended_at, duration_seconds,
                    event_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(event_key) DO UPDATE SET
                    device_id=excluded.device_id,
                    event_kind=excluded.event_kind,
                    severity=excluded.severity,
                    message=excluded.message,
                    details_json=excluded.details_json,
                    source_table=excluded.source_table,
                    source_row_id=excluded.source_row_id,
                    started_at=excluded.started_at,
                    ended_at=excluded.ended_at,
                    duration_seconds=excluded.duration_seconds,
                    event_at=excluded.event_at,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    event_key,
                    device_id,
                    event_kind,
                    severity,
                    message,
                    json.dumps(details, separators=(",", ":"), sort_keys=True),
                    str(details.get("source_table") or "").strip() or None,
                    str(details.get("source_row_id") or "").strip() or None,
                    normalize_device_event_time(details.get("started_at")) if details.get("started_at") else None,
                    normalize_device_event_time(details.get("ended_at")) if details.get("ended_at") else None,
                    duration_seconds,
                    event_at,
                ),
            )
            persisted += 1
    return persisted


def fetch_device_events(limit=12, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT id, device_id, event_kind, severity, message, details_json, source_table,
               source_row_id, started_at, ended_at, duration_seconds, event_at, created_at, updated_at
        FROM device_events
        WHERE 1 = 1
    """
    params = []
    if normalized_device_id:
        query += " AND device_id = ?"
        params.append(normalized_device_id)
    query += " ORDER BY event_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit or 12)))

    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    events = []
    for row in rows:
        details = {}
        if row["details_json"]:
            try:
                details = json.loads(row["details_json"])
            except (TypeError, ValueError):
                details = {}
        events.append(
            {
                "id": row["id"],
                "device_id": row["device_id"],
                "time": format_timestamp(row["event_at"]),
                "severity": row["severity"],
                "message": row["message"],
                "kind": row["event_kind"],
                "details": details,
                "source_table": row["source_table"],
                "source_row_id": row["source_row_id"],
                "started_at": format_timestamp(row["started_at"]),
                "ended_at": format_timestamp(row["ended_at"]),
                "duration_seconds": row["duration_seconds"],
            }
        )
    return events


def sync_device_events(device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    generated_events = build_generated_device_events(limit=320, device_id=normalized_device_id)
    persist_device_events(generated_events, default_device_id=normalized_device_id)
    return generated_events


def build_events(limit=12, device_id=None):
    sync_device_events(device_id=device_id)
    return fetch_device_events(limit=limit, device_id=device_id)


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
            text = f"*SaleWell Smart Tank Alert*\nSeverity: {payload.get('severity', 'info')}\nKind: {payload.get('kind', 'alert')}\nMessage: {payload.get('message', '')}"
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
                    "title": "SaleWell Smart Tank Alert",
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
    except Exception as exc:
        if best_effort and database_is_locked_error(exc):
            note_alert_touch(kind, severity, message, device_id=normalized_device_id, active=active)
            logger.warning(
                "Skipping alert bookkeeping for %s/%s because the database is busy.",
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


def fetch_filtered_alerts(limit=20, severity=None, device_id=None, updated_since=None):
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
    if updated_since:
        query += " AND updated_at >= ?"
        params.append(updated_since)
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
    source_clause, source_params = device_source_where_clause(column="candidate.device_source")
    query = """
        SELECT *
        FROM tank_data
        WHERE id IN (
            SELECT latest.id
            FROM tank_data latest
            WHERE latest.id = (
                SELECT candidate.id
                FROM tank_data candidate
                WHERE COALESCE(candidate.device_id, '') = COALESCE(latest.device_id, '')
                  AND 
    """
    query += source_clause
    query += """
                ORDER BY candidate.created_at DESC, candidate.id DESC
                LIMIT 1
            )
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
                ) recent_motor_rows
                ORDER BY id
            ) motor_transitions
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
                ) recent_leak_rows
                ORDER BY id
            ) leak_transitions
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


def is_private_device_base_url(value):
    text = normalize_device_base_url(value)
    if not text:
        return False
    parsed = urlparse(text)
    host = (parsed.hostname or "").strip().lower()
    if host in {"localhost"}:
        return True
    if host.endswith(".local") or host.endswith(".lan"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def local_device_status_url(base_url):
    normalized = normalize_device_base_url(base_url)
    if not normalized:
        return None
    return f"{normalized}/status"


def fetch_local_device_status(base_url, device_id=None):
    if not is_private_device_base_url(base_url):
        raise ValueError("local device URL must be a private LAN address")

    status_url = local_device_status_url(base_url)
    if not status_url:
        raise ValueError("local device URL is not configured")

    username = (
        os.environ.get("SWT_LOCAL_WEB_AUTH_USERNAME", "").strip()
        or normalize_device_id(device_id)
        or normalize_device_id(os.environ.get("SWT_DEVICE_ID", ""))
    )
    password = os.environ.get("SWT_LOCAL_WEB_AUTH_PASSWORD", "").strip()
    timeout = max(0.5, env_float("LOCAL_DEVICE_STATUS_TIMEOUT_SECONDS", 1.5))
    auth = (username, password) if username and password else None
    response = requests.get(status_url, auth=auth, timeout=timeout)
    if response.status_code in {401, 403} and username and password:
        session_client = requests.Session()
        session_client.post(
            f"{normalize_device_base_url(base_url)}/login",
            data={"username": username, "password": password},
            timeout=timeout,
        )
        response = session_client.get(status_url, auth=auth, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("local device returned invalid status")
    expected_device_id = normalize_device_id(device_id)
    returned_device_id = normalize_device_id(payload.get("device_id"))
    if expected_device_id and returned_device_id and returned_device_id != expected_device_id:
        raise ValueError("local device_id does not match requested device")
    payload["device_local_url"] = normalize_device_base_url(base_url)
    return payload


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


def ensure_firmware_artifact_dir():
    FIRMWARE_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    return FIRMWARE_ARTIFACT_DIR


def sanitize_firmware_filename(filename):
    return sanitize_firmware_filename_value(filename)


def firmware_artifact_storage_path(stored_filename):
    return firmware_artifact_storage_path_for_dir(ensure_firmware_artifact_dir(), stored_filename)


def extract_firmware_version_label(payload):
    return extract_firmware_version_label_from_payload(payload)


def fetch_firmware_artifact(artifact_id, device_id=None):
    try:
        normalized_artifact_id = int(artifact_id)
    except (TypeError, ValueError):
        return None

    if normalized_artifact_id <= 0:
        return None

    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT id, target_device, original_filename, stored_filename, version_label, notes,
               md5, size_bytes, content_type, uploaded_by, created_at
        FROM firmware_artifacts
        WHERE id = ?
    """
    params = [normalized_artifact_id]
    if normalized_device_id:
        query += " AND target_device = ?"
        params.append(normalized_device_id)

    with get_db() as db:
        row = db.execute(query, tuple(params)).fetchone()
    if not row:
        return None

    artifact = dict(row)
    artifact["storage_path"] = str(firmware_artifact_storage_path(artifact.get("stored_filename")))
    return artifact


def fetch_latest_firmware_artifact(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    with get_db() as db:
        row = db.execute(
            """
            SELECT id
            FROM firmware_artifacts
            WHERE target_device = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
    if not row:
        return None
    return fetch_firmware_artifact(row["id"], device_id=normalized_device_id)


def build_firmware_artifact_payload(artifact, target_device=None, download_endpoint=None):
    return build_firmware_artifact_response_payload(
        artifact,
        target_device=target_device,
        download_endpoint=download_endpoint,
        normalize_device_id=normalize_device_id,
    )


def create_firmware_artifact(device_id, uploaded_file, notes="", uploaded_by="admin"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("Choose a valid device before uploading firmware.")

    upload = read_uploaded_firmware(uploaded_file, FIRMWARE_ARTIFACT_MAX_BYTES)
    payload = upload["payload"]
    stored_filename = make_stored_firmware_filename(normalized_device_id)
    storage_path = firmware_artifact_storage_path(stored_filename)
    notes_text = str(notes or "").strip() or None

    try:
        storage_path.write_bytes(payload)
    except OSError as exc:
        raise ValueError("Unable to store the uploaded firmware on disk.") from exc

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO firmware_artifacts(
                    target_device, original_filename, stored_filename, version_label, notes,
                    md5, size_bytes, content_type, uploaded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_device_id,
                    upload["original_filename"],
                    stored_filename,
                    upload["version_label"],
                    notes_text,
                    upload["md5"],
                    len(payload),
                    upload["content_type"],
                    str(uploaded_by or "admin").strip() or "admin",
                ),
            )
            artifact_id = int(cursor.lastrowid or 0)
    except Exception as exc:
        try:
            storage_path.unlink()
        except OSError:
            pass
        raise ValueError("Unable to register the uploaded firmware artifact.") from exc

    artifact = fetch_firmware_artifact(artifact_id, device_id=normalized_device_id)
    if not artifact:
        raise ValueError("Uploaded firmware artifact could not be loaded after it was saved.")
    return artifact


def create_global_firmware_artifact(uploaded_file, notes="", uploaded_by="admin"):
    upload = read_uploaded_firmware(uploaded_file, FIRMWARE_ARTIFACT_MAX_BYTES)
    payload = upload["payload"]
    stored_filename = make_stored_firmware_filename(GLOBAL_FIRMWARE_TARGET)
    storage_path = firmware_artifact_storage_path(stored_filename)
    notes_text = str(notes or "").strip() or None

    try:
        storage_path.write_bytes(payload)
    except OSError as exc:
        raise ValueError("Unable to store the uploaded firmware on disk.") from exc

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO firmware_artifacts(
                    target_device, original_filename, stored_filename, version_label, notes,
                    md5, size_bytes, content_type, uploaded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    GLOBAL_FIRMWARE_TARGET,
                    upload["original_filename"],
                    stored_filename,
                    upload["version_label"],
                    notes_text,
                    upload["md5"],
                    len(payload),
                    upload["content_type"],
                    str(uploaded_by or "admin").strip() or "admin",
                ),
            )
            artifact_id = int(cursor.lastrowid or 0)
    except Exception as exc:
        try:
            storage_path.unlink()
        except OSError:
            pass
        raise ValueError("Unable to register the uploaded firmware artifact.") from exc

    artifact = fetch_firmware_artifact(artifact_id)
    if not artifact:
        raise ValueError("Uploaded firmware artifact could not be loaded after it was saved.")
    return artifact


def remove_old_global_firmware_artifacts():
    latest = fetch_latest_firmware_artifact(GLOBAL_FIRMWARE_TARGET)
    if not latest:
        return {"removed": 0, "files_removed": 0, "latest": None}

    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, stored_filename
            FROM firmware_artifacts
            WHERE target_device = ? AND id <> ?
            """,
            (GLOBAL_FIRMWARE_TARGET, latest["id"]),
        ).fetchall()
        db.execute(
            """
            DELETE FROM firmware_artifacts
            WHERE target_device = ? AND id <> ?
            """,
            (GLOBAL_FIRMWARE_TARGET, latest["id"]),
        )
        remaining_rows = db.execute(
            "SELECT DISTINCT stored_filename FROM firmware_artifacts"
        ).fetchall()

    remaining_filenames = {str(row["stored_filename"] or "") for row in remaining_rows}
    files_removed = 0
    for row in rows:
        stored_filename = str(row["stored_filename"] or "")
        if not stored_filename or stored_filename in remaining_filenames:
            continue
        try:
            firmware_artifact_storage_path(stored_filename).unlink()
            files_removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Unable to remove old firmware artifact file %s: %s", stored_filename, exc)

    return {"removed": len(rows), "files_removed": files_removed, "latest": latest}


def ensure_android_release_dir():
    ANDROID_RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    return ANDROID_RELEASE_DIR


def android_release_storage_path(stored_filename):
    return android_release_storage_path_for_dir(ensure_android_release_dir(), stored_filename)


def android_release_payload_is_available(release):
    if not release:
        return False
    try:
        if int(release.get("apk_blob_size") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return android_release_storage_path(release.get("stored_filename")).is_file()


def fetch_android_app_release(release_id=None, include_payload=False):
    payload_select = "apk_blob" if include_payload else "CASE WHEN apk_blob IS NULL THEN 0 ELSE length(apk_blob) END AS apk_blob_size"
    query = """
        SELECT id, original_filename, stored_filename, version_name, version_code, notes,
               md5, size_bytes, content_type, uploaded_by, created_at, {payload_select}
        FROM android_app_releases
    """.format(payload_select=payload_select)
    params = []
    if release_id is not None:
        try:
            normalized_release_id = int(release_id)
        except (TypeError, ValueError):
            return None
        if normalized_release_id <= 0:
            return None
        query += " WHERE id = ?"
        params.append(normalized_release_id)
    query += " ORDER BY version_code DESC, created_at DESC, id DESC LIMIT 1"

    with get_db() as db:
        row = db.execute(query, tuple(params)).fetchone()
    if not row:
        return None

    release = dict(row)
    release["storage_path"] = str(android_release_storage_path(release.get("stored_filename")))
    return release


def fetch_latest_android_app_release():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, original_filename, stored_filename, version_name, version_code, notes,
                   md5, size_bytes, content_type, uploaded_by, created_at,
                   CASE WHEN apk_blob IS NULL THEN 0 ELSE length(apk_blob) END AS apk_blob_size
            FROM android_app_releases
            ORDER BY version_code DESC, created_at DESC, id DESC
            LIMIT 20
            """
        ).fetchall()

    for row in rows:
        release = dict(row)
        release["storage_path"] = str(android_release_storage_path(release.get("stored_filename")))
        if android_release_payload_is_available(release):
            return release

    if rows:
        missing_ids = ", ".join(str(row["id"]) for row in rows[:5])
        logger.warning("Android release rows exist but no downloadable APK payload was found. Checked release IDs: %s", missing_ids)
    return None


def create_android_app_release(uploaded_file, notes="", uploaded_by="admin"):
    upload = read_uploaded_android_apk(uploaded_file, ANDROID_RELEASE_MAX_BYTES)
    normalized_version_name = upload["version_name"]
    normalized_version_code = upload["version_code"]
    payload = upload["payload"]
    stored_filename = make_stored_android_apk_filename(normalized_version_code)
    storage_path = android_release_storage_path(stored_filename)
    notes_text = str(notes or "").strip() or None

    try:
        storage_path.write_bytes(payload)
    except OSError as exc:
        raise ValueError("Unable to store the uploaded Android app on disk.") from exc

    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO android_app_releases(
                    original_filename, stored_filename, version_name, version_code, notes,
                    md5, size_bytes, content_type, apk_blob, uploaded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload["original_filename"],
                    stored_filename,
                    normalized_version_name,
                    normalized_version_code,
                    notes_text,
                    upload["md5"],
                    len(payload),
                    upload["content_type"],
                    payload,
                    str(uploaded_by or "admin").strip() or "admin",
                ),
            )
            release_id = int(cursor.lastrowid or 0)
    except Exception as exc:
        try:
            storage_path.unlink()
        except OSError:
            pass
        raise ValueError("Unable to register the uploaded Android app release.") from exc

    release = fetch_android_app_release(release_id)
    if not release:
        raise ValueError("Uploaded Android app release could not be loaded after it was saved.")
    return release


def remove_all_android_app_releases():
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, stored_filename
            FROM android_app_releases
            """
        ).fetchall()
        db.execute(
            """
            DELETE FROM android_app_releases
            """
        )

    release_dir = ensure_android_release_dir()
    candidate_paths = set()
    for row in rows:
        stored_filename = str(row["stored_filename"] or "").strip()
        if stored_filename:
            candidate_paths.add(android_release_storage_path(stored_filename))
    candidate_paths.update(release_dir.glob("*.apk"))

    removed_paths = set()
    files_removed = 0
    for path in candidate_paths:
        resolved_path = path.resolve()
        if resolved_path in removed_paths:
            continue
        if release_dir.resolve() not in (resolved_path, *resolved_path.parents):
            logger.warning("Skipping Android release cleanup outside release directory: %s", resolved_path)
            continue
        try:
            resolved_path.unlink()
            removed_paths.add(resolved_path)
            files_removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Unable to remove Android release file %s: %s", resolved_path, exc)

    return {"removed": len(rows), "files_removed": files_removed, "latest": None}


def build_android_apk_file_response(release, storage_path):
    return build_android_apk_file_response_payload(send_file, release, storage_path)


def build_android_apk_blob_response(release, payload):
    return build_android_apk_blob_response_payload(send_file, release, payload)


def fetch_android_app_release_blob(release_id):
    release = fetch_android_app_release(release_id, include_payload=True)
    if not release:
        return None
    payload = release.get("apk_blob")
    if not payload:
        return None
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    return bytes(payload)


def build_android_update_manifest_for_request(release, apk_url):
    manifest = build_android_release_manifest(release, apk_url)
    current_version_code = max(0, request.args.get("currentVersionCode", default=0, type=int) or 0)
    latest_version_code = int(manifest.get("versionCode") or 0)
    update_available = bool(apk_url and latest_version_code > current_version_code)
    manifest["updateAvailable"] = update_available
    if current_version_code > 0:
        manifest["currentVersionCode"] = current_version_code
    if not update_available:
        manifest["apkUrl"] = ""
    return manifest


def build_firmware_artifact_file_response(artifact, storage_path):
    return build_firmware_artifact_file_response_payload(send_file, artifact, storage_path)


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
    relay_urls = relay_urls_for_current_request(RELAY_STATUS_URL_LIST, "/status")
    if not relay_urls:
        set_alert("relay_failure", "warning", "Cloud relay is failing.", active=False)
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
    for url in relay_urls:
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
                    resolve_transient_relay_alerts(device_id)
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
                if response.status_code < 500:
                    relay_http_message = f"Cloud relay returned HTTP {response.status_code}."
                    set_alert("relay_failure", "warning", relay_http_message, active=True)
            except requests.RequestException as exc:
                relay_state["last_error_at"] = now_utc().strftime(TIMESTAMP_FORMAT)
                relay_state["last_error"] = str(exc)
                relay_state["last_status_code"] = None
                logger.warning("Relay telemetry failed (%s): %s", url, exc)
                set_alert("relay_failure", "warning", f"Cloud relay request failed: {exc}", active=True)
    return "retry"


def resolve_transient_relay_alerts(device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    with get_db() as db:
        if normalized_device_id:
            db.execute(
                """
                UPDATE ops_alerts
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE kind = 'relay_failure'
                  AND active = 1
                  AND COALESCE(device_id, '') = ?
                  AND message LIKE 'Cloud relay returned HTTP 5%'
                """,
                (normalized_device_id,),
            )
        else:
            db.execute(
                """
                UPDATE ops_alerts
                SET active = 0, updated_at = CURRENT_TIMESTAMP
                WHERE kind = 'relay_failure'
                  AND active = 1
                  AND message LIKE 'Cloud relay returned HTTP 5%'
                """
            )


def enqueue_relay_payload(payload):
    if not relay_urls_for_current_request(RELAY_STATUS_URL_LIST, "/status"):
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
    relay_urls = relay_urls_for_current_request(RELAY_COMMAND_URL_LIST, "/device/command")
    if not relay_urls:
        return None
    for url in relay_urls:
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
    relay_urls = relay_urls_for_current_request(RELAY_COMMAND_URL_LIST, "/device/command")
    if not relay_urls:
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

    for url in relay_urls:
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
    if not relay_urls_for_current_request(RELAY_STATUS_URL_LIST, "/status"):
        set_alert("relay_failure", "warning", "Cloud relay is failing.", active=False, best_effort=True)
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


def resolve_relay_alert_when_disabled():
    if RELAY_STATUS_URL_LIST:
        return
    set_alert("relay_failure", "warning", "Cloud relay is failing.", active=False, best_effort=True)


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
    capacity_liters = request.values.get("capacity_liters", type=float)
    target_device = current_scope_device_id(request.values.get("device_id", type=str))

    if capacity_liters is None:
        return jsonify({"error": "capacity_liters is required"}), 400

    if capacity_liters < 50 or capacity_liters > 50000:
        return jsonify({"error": "capacity_liters must be between 50 and 50000"}), 400

    command = f"CONFIG_CAPACITY:{capacity_liters:.1f}"
    payload = queue_command(command, target_device=target_device)
    payload["capacity_liters"] = round(capacity_liters, 1)
    payload["message"] = "Tank capacity command queued. Calibrate to learn tank height."
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


def resolve_simulator_command(payload):
    payload = payload or {}
    raw_target = str(
        payload.get("target")
        or payload.get("scope")
        or payload.get("tank")
        or "all"
    ).strip().lower().replace("-", "_")
    raw_state = (
        payload.get("enabled")
        if "enabled" in payload
        else payload.get("state", payload.get("value", payload.get("action", "on")))
    )
    if raw_state is None:
        enabled = True
    elif isinstance(raw_state, bool):
        enabled = raw_state
    else:
        normalized_state = str(raw_state).strip().lower()
        if normalized_state in {"1", "true", "yes", "on", "enable", "enabled"}:
            enabled = True
        elif normalized_state in {"0", "false", "no", "off", "disable", "disabled"}:
            enabled = False
        else:
            raise ValueError("enabled/state must be true, false, on, or off")
    state_suffix = "ON" if enabled else "OFF"
    target_map = {
        "all": "SIMULATOR",
        "dual": "SIMULATOR",
        "dual_tank": "SIMULATOR",
        "tank": "SIMULATOR",
        "simulator": "SIMULATOR",
        "main": "SIMULATOR",
        "upper": "SIMULATOR",
        "upper_tank": "SIMULATOR",
        "main_tank": "SIMULATOR",
        "source": "SIMULATOR",
        "lower": "SIMULATOR",
        "source_tank": "SIMULATOR",
        "lower_tank": "SIMULATOR",
    }
    command_prefix = target_map.get(raw_target)
    if not command_prefix:
        raise ValueError("target must be one of all, upper, main, lower, or source")
    return f"{command_prefix}_{state_suffix}", raw_target, enabled


def mobile_simulator_command_response():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    data = request.get_json(silent=True) or {}
    try:
        command, target, enabled = resolve_simulator_command(data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    target_device = current_mobile_scope_device_id(data.get("device_id") or request.args.get("device_id", type=str))
    result = queue_command(command, target_device=target_device)
    if isinstance(result, tuple):
        payload, status_code = result
        return jsonify(payload), status_code
    payload = dict(result)
    payload.update(
        {
            "message": f"Simulator {'enable' if enabled else 'disable'} command queued.",
            "simulator_target": target,
            "simulator_enabled": enabled,
        }
    )
    user = resolve_mobile_user() or {}
    log_audit_event(
        actor=user.get("username") or current_actor_username(),
        action="queue_simulator_command",
        target_type="device",
        target_id=payload.get("target_device"),
        device_id=payload.get("target_device"),
        details={"command": command, "target": target, "enabled": enabled, "source": "mobile_api"},
    )
    return jsonify(payload)


def build_mobile_auth_response_payload(authenticated_user, message=None):
    payload = {
        "token": issue_mobile_token(authenticated_user),
        "viewer": {
            "role": authenticated_user.get("role"),
            "username": authenticated_user.get("username"),
            "device_id": authenticated_user.get("device_id"),
            "display_name": authenticated_user.get("display_name"),
            "source_tank_monitoring_enabled": authenticated_user.get("source_tank_monitoring_enabled", True),
            "cloud_feed_enabled": authenticated_user.get("cloud_feed_enabled", True),
            "cloud_feed_mode": authenticated_user.get("cloud_feed_mode", DEVICE_SERVICE_CLOUD_FEED_FULL),
            "ai_analysis_enabled": authenticated_user.get("ai_analysis_enabled", True),
        },
        "expires_in_seconds": MOBILE_TOKEN_MAX_AGE_SECONDS,
    }
    if message:
        payload["message"] = message
    return payload


@app.route("/api/mobile/auth/login", methods=["POST"])
def mobile_auth_login():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "")
    password = data.get("password", "")
    authenticated_user = authenticate_dashboard_user(username, password)
    if not authenticated_user:
        time.sleep(0.5)
        return jsonify({"error": "invalid username or password"}), 401
    return jsonify(build_mobile_auth_response_payload(authenticated_user))


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
    public_snapshot = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    service_config = resolve_device_service_config(scoped_device_id, snapshot=public_snapshot)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    payload = {
        "snapshot": public_snapshot,
        "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
        "events": build_events(event_limit, device_id=scoped_device_id),
        "guidance": build_shared_guidance_payload(snapshot, None),
        "generated_at": now_utc().strftime(TIMESTAMP_FORMAT),
        "viewer": viewer,
        "service_config": service_config,
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


@app.route("/api/mobile/local-sync", methods=["POST"])
@mobile_auth_required
def mobile_local_sync():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid json"}), 400

    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str) or data.get("device_id"))
    payload_device_id = normalize_device_id(data.get("device_id"))
    if not scoped_device_id:
        return jsonify({"error": "device_id is required"}), 400
    if payload_device_id and payload_device_id != scoped_device_id:
        return jsonify({"error": "device_id does not match authenticated device"}), 403

    data["device_id"] = scoped_device_id
    data["device_source"] = normalize_device_source(data.get("device_source"), default=DEVICE_SOURCE_REAL)
    cleaned = process_telemetry_payload(data, source_ip="android_local_wifi", transport="android_local_wifi")
    snapshot = load_dashboard_snapshot(scoped_device_id)
    refresh_operational_alerts(snapshot if snapshot_has_live_device_data(snapshot) else None)
    return jsonify(
        {
            "result": "saved",
            "device_id": scoped_device_id,
            "snapshot": strip_ip_address_fields(snapshot or cleaned, keep_device_local_url=True),
        }
    )


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
        updated_user = {
            "role": "admin",
            "username": username or LOGIN_USERNAME,
            "device_id": None,
            "display_name": "Administrator",
            "cloud_feed_enabled": True,
            "cloud_feed_mode": DEVICE_SERVICE_CLOUD_FEED_FULL,
            "ai_analysis_enabled": True,
            "auth_marker": current_dashboard_auth_marker(),
        }
        log_audit_event(
            actor=username or current_actor_username(),
            action="reset_dashboard_password",
            target_type="dashboard_account",
            target_id=LOGIN_USERNAME,
            details={"username": LOGIN_USERNAME, "source": "mobile_api"},
        )
        return jsonify(build_mobile_auth_response_payload(updated_user, message="Dashboard password updated successfully."))

    if role == "customer" and device_id:
        account = fetch_customer_account(device_id)
        if not account or int(account.get("active", 0)) != 1:
            return jsonify({"error": "Customer account not found."}), 404
        if not check_password_hash(account.get("password_hash", ""), current_password):
            return jsonify({"error": "Current password is incorrect."}), 400
        updated_account = update_customer_password(device_id, new_password)
        updated_service_config = fetch_device_service_config(device_id, account=updated_account)
        updated_user = {
            "role": role,
            "username": username or device_id,
            "device_id": device_id,
            "display_name": updated_account.get("display_name") or device_id,
            "slave_device_enabled": updated_service_config.get("slave_device_enabled", True),
            "source_tank_monitoring_enabled": updated_service_config.get("source_tank_monitoring_enabled", True),
            "cloud_feed_enabled": updated_service_config.get("cloud_feed_enabled", True),
            "cloud_feed_mode": updated_service_config.get("cloud_feed_mode"),
            "ai_analysis_enabled": updated_service_config.get("effective_ai_analysis_enabled", True),
            "auth_marker": current_auth_marker_for_identity("customer", device_id=device_id, account=updated_account),
        }
        log_audit_event(
            actor=username or device_id,
            action="reset_customer_password",
            target_type="customer_account",
            target_id=updated_account["device_id"],
            device_id=updated_account["device_id"],
            details={
                "display_name": updated_account.get("display_name"),
                "source": "mobile_api_self_service",
                "password_scope": "cloud_only",
            },
        )
        payload = build_mobile_auth_response_payload(
            updated_user,
            message="Cloud password updated successfully.",
        )
        return jsonify(payload)

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
        capacity_liters = float(capacity_liters)
    except (TypeError, ValueError):
        return jsonify({"error": "capacity_liters is required"}), 400
    if capacity_liters < 50 or capacity_liters > 50000:
        return jsonify({"error": "capacity_liters must be between 50 and 50000"}), 400
    if height_cm is None:
        command = f"CONFIG_CAPACITY:{capacity_liters:.1f}"
    else:
        try:
            height_cm = float(height_cm)
        except (TypeError, ValueError):
            return jsonify({"error": "height_cm must be a number when provided"}), 400
        if height_cm < 30 or height_cm > 500:
            return jsonify({"error": "height_cm must be between 30 and 500"}), 400
        command = f"CONFIG:{height_cm:.1f}:{capacity_liters:.1f}"
    result = queue_command(command, target_device=target_device)
    if isinstance(result, tuple):
        payload, status_code = result
        return jsonify(payload), status_code
    payload = dict(result)
    if height_cm is not None:
        payload["height_cm"] = round(height_cm, 1)
    payload["capacity_liters"] = round(capacity_liters, 1)
    payload["message"] = "Tank capacity command queued. Calibrate to learn tank height."
    return jsonify(payload)


@app.route("/api/mobile/simulator", methods=["POST"])
@app.route("/api/mobile/device/simulator", methods=["POST"])
@mobile_auth_required
def mobile_device_simulator():
    return mobile_simulator_command_response()


@app.route("/api/mobile/device/status")
@mobile_auth_required
def mobile_device_status():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    service_config = resolve_device_service_config(scoped_device_id, snapshot=snapshot)
    return jsonify({
        "snapshot": strip_ip_address_fields(snapshot, keep_device_local_url=True),
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
                    "ota_enabled": bool(
                        snapshot and str(snapshot.get("ota_service") or "").upper() == "ON"
                    ),
                    "local_firmware_upload_enabled": bool(
                        snapshot and str(snapshot.get("local_firmware_upload_service") or "").upper() == "ON"
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
        main_sensor_enabled=source_payload.get("main_sensor_enabled"),
        slave_device_enabled=source_payload.get("slave_device_enabled"),
        source_tank_monitoring_enabled=source_payload.get("source_tank_monitoring_enabled"),
        ai_analysis_enabled=source_payload.get("ai_analysis_enabled"),
        cloud_feed_mode=source_payload.get("cloud_feed_mode"),
        ota_enabled=source_payload.get("ota_enabled"),
        local_firmware_upload_enabled=source_payload.get("local_firmware_upload_enabled"),
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


register_mobile_firmware_routes(
    app,
    mobile_auth_required=mobile_auth_required,
    current_mobile_scope_device_id=current_mobile_scope_device_id,
    fetch_latest_firmware_artifact=fetch_latest_firmware_artifact,
    fetch_firmware_artifact=fetch_firmware_artifact,
    build_firmware_artifact_payload=build_firmware_artifact_payload,
    firmware_artifact_storage_path=firmware_artifact_storage_path,
    build_firmware_artifact_file_response=build_firmware_artifact_file_response,
    logger=logger,
)


@app.route("/api/mobile/app/update")
@mobile_auth_required
def mobile_android_version_manifest():
    release = fetch_latest_android_app_release()
    apk_url = url_for("mobile_android_app_download", _external=True) if release else ""
    manifest = build_android_update_manifest_for_request(release, apk_url)
    manifest["requiresAuth"] = True
    manifest["viewer"] = resolve_mobile_user()
    response = jsonify(manifest)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/mobile/app/latest.apk")
@mobile_auth_required
def mobile_android_app_download():
    release = fetch_latest_android_app_release()
    if not release:
        return Response("Android app release has not been uploaded yet.", status=404, mimetype="text/plain")

    storage_path = android_release_storage_path(release.get("stored_filename"))
    if storage_path.is_file():
        return build_android_apk_file_response(release, storage_path)

    payload = fetch_android_app_release_blob(release.get("id"))
    if payload:
        logger.warning("Android app release %s is missing on disk, serving database copy: %s", release.get("id"), storage_path)
        return build_android_apk_blob_response(release, payload)

    logger.warning("Android app release %s is registered but missing on disk and in database storage: %s", release.get("id"), storage_path)
    return Response("Android app release file is missing. Re-upload the Android app from admin.", status=404, mimetype="text/plain")


@app.route("/device/command")
def get_command():
    try:
        request_source = resolve_request_device_source()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

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


CUSTOMER_PASSWORD_RESET_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }}</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:"Segoe UI",sans-serif;background:#edf4fb;color:#0f172a}
.card{width:min(440px,calc(100% - 24px));background:#fff;border:1px solid #d8e2ee;border-radius:18px;padding:26px;box-shadow:0 18px 40px rgba(15,23,42,.12)}
h1{margin:0 0 8px;font-size:30px}p{line-height:1.55;color:#475569}.group{display:grid;gap:6px;margin:14px 0}
label{font-size:12px;text-transform:uppercase;letter-spacing:.12em;font-weight:800;color:#64748b}
input{padding:12px 14px;border-radius:12px;border:1px solid #cbd5e1;font:inherit}
button,.link{display:inline-flex;align-items:center;justify-content:center;min-height:42px;border-radius:12px;padding:10px 16px;font-weight:800;text-decoration:none}
button{border:0;background:#1769e0;color:#fff}.link{border:1px solid #cbd5e1;color:#0f172a;margin-left:8px}
.message{padding:12px 14px;border-radius:12px;margin:12px 0}.error{background:#fee2e2;color:#991b1b}.success{background:#dcfce7;color:#166534}
</style>
</head>
<body>
<main class="card">
<h1>{{ title }}</h1>
<p>{{ description }}</p>
{% if error %}<div class="message error">{{ error }}</div>{% endif %}
{% if success %}<div class="message success">{{ success }}</div>{% endif %}
{% if mode == "request" %}
<form method="post" action="{{ url_for('customer_forgot_password') }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<div class="group"><label for="identifier">Device ID or Email</label><input id="identifier" name="identifier" type="text" autocomplete="username email" required></div>
<button type="submit">Send Reset Link</button><a class="link" href="{{ url_for('customer_login') }}">Back to Login</a>
</form>
{% elif mode == "reset" %}
<form method="post" action="{{ url_for('customer_reset_password', token=token) }}">
<input type="hidden" name="csrf_token" value="{{ csrf_token }}">
<div class="group"><label for="password">New Password</label><input id="password" name="password" type="password" autocomplete="new-password" minlength="6" required></div>
<div class="group"><label for="confirm_password">Confirm Password</label><input id="confirm_password" name="confirm_password" type="password" autocomplete="new-password" minlength="6" required></div>
<button type="submit">Reset Password</button><a class="link" href="{{ url_for('customer_login') }}">Back to Login</a>
</form>
{% else %}
<a class="link" href="{{ url_for('customer_login') }}">Back to Customer Login</a>
{% endif %}
</main>
</body>
</html>"""


@app.route("/login/customer/forgot-password", methods=["GET", "POST"])
@csrf_protect
def customer_forgot_password():
    error = None
    success = None
    if request.method == "POST":
        identifier = str(request.form.get("identifier", "")).strip()
        account = None
        if not smtp_email_configured():
            error = (
                "Password reset email is not fully configured yet. "
                "Add the support mailbox password in cPanel, then restart the Python app."
            )
        else:
            if "@" in identifier:
                try:
                    account = fetch_customer_account_by_email(identifier)
                except ValueError:
                    account = None
            else:
                account = fetch_customer_account(identifier)
            if account and account.get("email") and int(account.get("active", 1) or 0) == 1:
                token, _expires_at = create_customer_password_reset(account)
                try:
                    sent = send_customer_password_reset(account, token)
                except Exception as exc:
                    logger.warning("Customer password reset email failed for %s: %s", account["device_id"], exc)
                    error = "Unable to send the reset email right now. Please try again later or contact support@salewell.co.in."
                else:
                    if sent:
                        log_audit_event(
                            actor="customer-self-service",
                            action="request_customer_password_reset",
                            target_type="customer_account",
                            target_id=account["device_id"],
                            device_id=account["device_id"],
                            details={"email": account.get("email")},
                        )
                    else:
                        error = "Unable to send the reset email right now. Please contact support@salewell.co.in."
            if not error:
                success = "If that customer account has an email on file, a reset link has been sent."
    return render_template_string(
        CUSTOMER_PASSWORD_RESET_TEMPLATE,
        mode="request",
        title="Forgot Password",
        description="Enter your registered device ID or email. We will send a reset link to the customer email on file.",
        error=error,
        success=success,
    )


@app.route("/login/customer/reset-password/<token>", methods=["GET", "POST"])
@csrf_protect
def customer_reset_password(token):
    reset_row = fetch_customer_password_reset(token)
    if not customer_password_reset_valid(reset_row):
        return render_template_string(
            CUSTOMER_PASSWORD_RESET_TEMPLATE,
            mode="done",
            title="Reset Link Expired",
            description="This password reset link is invalid or expired. Request a new reset link from the customer login page.",
            error=None,
            success=None,
        ), 400

    error = None
    success = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if password != confirm_password:
            error = "New password and confirm password do not match."
        elif len(password) < 6:
            error = "Use at least 6 characters for the new password."
        else:
            update_customer_password(reset_row["device_id"], password)
            mark_customer_password_reset_used(reset_row["id"])
            log_audit_event(
                actor="customer-self-service",
                action="complete_customer_password_reset",
                target_type="customer_account",
                target_id=reset_row["device_id"],
                device_id=reset_row["device_id"],
            )
            success = "Password reset successfully. You can now sign in with the new password."
            return render_template_string(
                CUSTOMER_PASSWORD_RESET_TEMPLATE,
                mode="done",
                title="Password Updated",
                description="Your customer password has been updated.",
                error=None,
                success=success,
            )
    return render_template_string(
        CUSTOMER_PASSWORD_RESET_TEMPLATE,
        mode="reset",
        token=token,
        title="Reset Password",
        description="Choose a new customer dashboard password.",
        error=error,
        success=success,
    )


@app.route("/pricing")
def pricing_page():
    return render_template("pricing.html")


@app.route("/sales/enquiry", methods=["POST"])
@csrf_protect
def sales_enquiry():
    landing_mode = "admin" if request.form.get("landing_mode") == "admin" else "customer"
    next_url = resolve_next_url(dashboard_home_url("customer"))
    cleaned, errors = validate_sales_enquiry_payload(request.form)

    if errors:
        return render_login_page(
            mode=landing_mode,
            next_url=next_url,
            sales_error=" ".join(errors),
            sales_form=cleaned,
        ), 400

    lead_details = dict(cleaned)
    lead_details["landing_mode"] = landing_mode
    lead_details["next_url"] = next_url
    lead_details["remote_addr"] = request.headers.get("X-Forwarded-For", request.remote_addr)
    log_audit_event(
        actor="public-lead",
        action="sales_enquiry_submitted",
        target_type="sales_enquiry",
        details=lead_details,
    )
    support_email_sent = send_sales_enquiry_email(cleaned, lead_details)
    confirmation_email_sent = send_sales_enquiry_confirmation_email(cleaned)
    append_sales_enquiry_backup(
        cleaned,
        lead_details,
        support_email_sent=support_email_sent,
        confirmation_email_sent=confirmation_email_sent,
    )
    logger.info(
        "Sales enquiry submitted for %s (%s). support_email_sent=%s confirmation_email_sent=%s backup=%s",
        cleaned["name"],
        cleaned["phone"],
        support_email_sent,
        confirmation_email_sent,
        SALES_ENQUIRY_BACKUP_PATH,
    )
    enquiry_status = "success" if support_email_sent else "saved_email_pending"
    return redirect(url_for("dashboard", enquiry=enquiry_status))


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
            refresh_current_session_auth_marker()
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
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        try:
            account = upsert_customer_account(
                device_id,
                password,
                display_name=display_name,
                email=email,
                service_updates_enabled=form_flag("service_updates_enabled", default=True),
                marketing_emails_enabled=form_flag("marketing_emails_enabled", default=False),
            )
            log_audit_event(
                actor=current_actor_username(),
                action="upsert_customer_account",
                target_type="customer_account",
                target_id=account["device_id"],
                device_id=account["device_id"],
                details={
                    "display_name": account.get("display_name"),
                    "email": account.get("email"),
                    "service_updates_enabled": bool(account.get("service_updates_enabled")),
                    "marketing_emails_enabled": bool(account.get("marketing_emails_enabled")),
                    "password_scope": "cloud_only",
                },
            )
            success = (
                f"Customer account saved for {account['device_id']}. "
                "Local firmware password changes are handled from the Android app on the LAN."
            )
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/devices/register", methods=["POST"])
@admin_required
@csrf_protect
def admin_register_device_credentials():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    device_id = request.form.get("device_id", "")
    device_key = request.form.get("device_key", "")
    display_name = request.form.get("display_name", "")
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    try:
        normalized_device_id = register_device_credentials(
            device_id,
            device_key,
            registration_source="admin_manual",
            remote_addr=request.remote_addr,
        )
        log_audit_event(
            actor=current_actor_username(),
            action="register_device_credentials",
            target_type="device_auth_key",
            target_id=normalized_device_id,
            device_id=normalized_device_id,
            details={"registration_source": "admin_manual"},
        )
        customer_saved = False
        customer_profile_requested = any(str(value or "").strip() for value in (display_name, email))
        if customer_profile_requested and not str(password or "").strip():
            raise ValueError("Customer password is required when customer name or email is entered.")
        if str(password or "").strip():
            account = upsert_customer_account(
                normalized_device_id,
                password,
                display_name=display_name,
                email=email,
                service_updates_enabled=form_flag("service_updates_enabled", default=True),
                marketing_emails_enabled=form_flag("marketing_emails_enabled", default=False),
            )
            customer_saved = True
            log_audit_event(
                actor=current_actor_username(),
                action="upsert_customer_account",
                target_type="customer_account",
                target_id=account["device_id"],
                device_id=account["device_id"],
                details={
                    "display_name": account.get("display_name"),
                    "email": account.get("email"),
                    "service_updates_enabled": bool(account.get("service_updates_enabled")),
                    "marketing_emails_enabled": bool(account.get("marketing_emails_enabled")),
                    "password_scope": "cloud_only",
                    "source": "device_registration_panel",
                },
            )
        success = (
            f"Device credentials registered for {normalized_device_id}. "
            + ("Customer login was also saved. " if customer_saved else "")
            + "The device can connect immediately."
        )
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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
                details={
                    "display_name": updated_account.get("display_name"),
                    "password_scope": "cloud_only",
                },
            )
            success = (
                f"Customer cloud password reset for {updated_account['device_id']}. "
                "Local firmware password changes are handled from the Android app on the LAN."
            )
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/customers/<device_id>/firmware", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_firmware_upload(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    normalized_device_id = normalize_device_id(device_id)

    if not normalized_device_id:
        error = "Choose a valid device before uploading firmware."
    else:
        firmware_file = request.files.get("firmware_file")
        notes = request.form.get("notes", "")
        try:
            artifact = create_firmware_artifact(
                normalized_device_id,
                firmware_file,
                notes=notes,
                uploaded_by=current_actor_username(),
            )
            log_audit_event(
                actor=current_actor_username(),
                action="upload_device_firmware_artifact",
                target_type="device",
                target_id=normalized_device_id,
                device_id=normalized_device_id,
                details={
                    "artifact_id": artifact["id"],
                    "version_label": artifact.get("version_label"),
                    "original_filename": artifact.get("original_filename"),
                    "md5": artifact.get("md5"),
                    "size_bytes": artifact.get("size_bytes"),
                    "notes": artifact.get("notes"),
                    "delivery": "android_local_wifi",
                },
            )
            version_suffix = f" ({artifact['version_label']})" if artifact.get("version_label") else ""
            success = (
                f"Firmware uploaded for {normalized_device_id}. "
                f"{artifact['original_filename']}{version_suffix} is now available to the Android app for local Wi-Fi upgrades."
            )
        except ValueError as exc:
            error = str(exc)

    if request.form.get("return_to") == "device_detail":
        return redirect(
            url_for(
                "device_detail_page",
                device_id=normalized_device_id or device_id,
                config_error=error or "",
                config_message=success or "",
            )
        )

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/releases/firmware", methods=["POST"])
@admin_required
@csrf_protect
def admin_global_firmware_upload():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""

    firmware_file = request.files.get("firmware_file")
    notes = request.form.get("notes", "")
    try:
        artifact = create_global_firmware_artifact(
            firmware_file,
            notes=notes,
            uploaded_by=current_actor_username(),
        )
        log_audit_event(
            actor=current_actor_username(),
            action="upload_global_firmware_artifact",
            target_type="release",
            target_id=str(artifact["id"]),
            details={
                "artifact_id": artifact["id"],
                "version_label": artifact.get("version_label"),
                "original_filename": artifact.get("original_filename"),
                "md5": artifact.get("md5"),
                "size_bytes": artifact.get("size_bytes"),
                "notes": artifact.get("notes"),
                "delivery": "all_customers_android_local_wifi",
            },
        )
        version_suffix = f" ({artifact['version_label']})" if artifact.get("version_label") else ""
        success = (
            f"Global firmware uploaded. {artifact['original_filename']}{version_suffix} "
            "is now available to all customer Android apps for local Wi-Fi upgrades."
        )
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/releases/firmware/prune", methods=["POST"])
@admin_required
@csrf_protect
def admin_global_firmware_prune():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    try:
        result = remove_old_global_firmware_artifacts()
        latest = result.get("latest")
        log_audit_event(
            actor=current_actor_username(),
            action="prune_old_global_firmware_artifacts",
            target_type="release",
            target_id=str(latest["id"]) if latest else "global_firmware",
            details={
                "removed": result["removed"],
                "files_removed": result["files_removed"],
                "kept_artifact_id": latest.get("id") if latest else None,
                "kept_version_label": latest.get("version_label") if latest else None,
            },
        )
        if latest:
            success = (
                f"Removed {result['removed']} old firmware build record"
                f"{'' if result['removed'] == 1 else 's'} and {result['files_removed']} old file"
                f"{'' if result['files_removed'] == 1 else 's'}. Latest firmware stays active."
            )
        else:
            success = "No global firmware build is uploaded yet."
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/releases/android", methods=["POST"])
@admin_required
@csrf_protect
def admin_android_release_upload():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""

    apk_file = request.files.get("apk_file")
    notes = request.form.get("notes", "")
    try:
        release = create_android_app_release(
            apk_file,
            notes=notes,
            uploaded_by=current_actor_username(),
        )
        log_audit_event(
            actor=current_actor_username(),
            action="upload_android_app_release",
            target_type="release",
            target_id=str(release["id"]),
            details={
                "release_id": release["id"],
                "version_name": release.get("version_name"),
                "version_code": release.get("version_code"),
                "original_filename": release.get("original_filename"),
                "md5": release.get("md5"),
                "size_bytes": release.get("size_bytes"),
                "notes": release.get("notes"),
                "download_url": url_for("android_app_download", _external=True),
                "manifest_url": url_for("android_version_manifest", _external=True),
            },
        )
        success = (
            f"Android app {release['version_name']} ({release['version_code']}) uploaded. "
            "It is now available from the website footer and Android update checks."
        )
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/admin/releases/android/prune", methods=["POST"])
@admin_required
@csrf_protect
def admin_android_release_prune():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    try:
        result = remove_all_android_app_releases()
        log_audit_event(
            actor=current_actor_username(),
            action="remove_all_android_app_releases",
            target_type="release",
            target_id="android_app",
            details={
                "removed": result["removed"],
                "files_removed": result["files_removed"],
            },
        )
        success = (
            f"Removed {result['removed']} Android build record"
            f"{'' if result['removed'] == 1 else 's'} and {result['files_removed']} APK file"
            f"{'' if result['files_removed'] == 1 else 's'}. Upload a new Android APK to restore the download link."
        )
    except ValueError as exc:
        error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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


@app.route("/static/version.json")
def android_version_manifest():
    release = fetch_latest_android_app_release()
    apk_url = url_for("android_app_download", _external=True) if release else ""
    response = jsonify(build_android_update_manifest_for_request(release, apk_url))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/downloads/android/latest.apk")
def android_app_download():
    release = fetch_latest_android_app_release()
    if not release:
        return Response("Android app release has not been uploaded yet.", status=404, mimetype="text/plain")

    storage_path = android_release_storage_path(release.get("stored_filename"))
    if storage_path.is_file():
        return build_android_apk_file_response(release, storage_path)

    payload = fetch_android_app_release_blob(release.get("id"))
    if payload:
        logger.warning("Android app release %s is missing on disk, serving database copy: %s", release.get("id"), storage_path)
        return build_android_apk_blob_response(release, payload)

    logger.warning("Android app release %s is registered but missing on disk and in database storage: %s", release.get("id"), storage_path)
    return Response("Android app release file is missing. Re-upload the Android app from admin.", status=404, mimetype="text/plain")


@app.route("/admin/customers/<device_id>/edit", methods=["POST"])
@admin_required
@csrf_protect
def admin_customer_edit(device_id):
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""
    display_name = request.form.get("display_name", "")
    email = request.form.get("email", "")
    account = fetch_customer_account(device_id)

    if not account:
        error = f"Customer account not found for {normalize_device_id(device_id) or 'that device'}."
    else:
        try:
            updated_account = update_customer_account_profile(
                account["device_id"],
                display_name=display_name,
                email=email,
                service_updates_enabled=form_flag("service_updates_enabled", default=False),
                marketing_emails_enabled=form_flag("marketing_emails_enabled", default=False),
            )
            log_audit_event(
                actor=current_actor_username(),
                action="update_customer_profile",
                target_type="customer_account",
                target_id=updated_account["device_id"],
                device_id=updated_account["device_id"],
                details={
                    "display_name": updated_account.get("display_name"),
                    "email": updated_account.get("email"),
                    "service_updates_enabled": bool(updated_account.get("service_updates_enabled")),
                    "marketing_emails_enabled": bool(updated_account.get("marketing_emails_enabled")),
                },
            )
            success = f"Customer profile updated for {updated_account['device_id']}."
        except ValueError as exc:
            error = str(exc)

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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
        ai_analysis_enabled = "ai_analysis_enabled" in request.form
        cloud_feed_mode = request.form.get("cloud_feed_mode")
        if cloud_feed_mode not in {
            DEVICE_SERVICE_CLOUD_FEED_OFF,
            DEVICE_SERVICE_CLOUD_FEED_BASIC,
            DEVICE_SERVICE_CLOUD_FEED_FULL,
        }:
            cloud_feed_mode = (
                DEVICE_SERVICE_CLOUD_FEED_OFF
                if "cloud_feed_disabled" in request.form
                else (
                    DEVICE_SERVICE_CLOUD_FEED_FULL
                    if ai_analysis_enabled
                    else DEVICE_SERVICE_CLOUD_FEED_BASIC
                )
            )
        updated_config = upsert_device_service_config(
            normalized_device_id,
            main_sensor_enabled=("main_sensor_enabled" in request.form),
            slave_device_enabled=("slave_device_enabled" in request.form),
            source_tank_monitoring_enabled=("source_tank_monitoring_enabled" in request.form),
            ai_analysis_enabled=ai_analysis_enabled,
            cloud_feed_mode=cloud_feed_mode,
            ota_enabled=("ota_enabled" in request.form),
            local_firmware_upload_enabled=("local_firmware_upload_enabled" in request.form),
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

    if request.form.get("return_to") == "device_detail":
        return redirect(
            url_for(
                "device_detail_page",
                device_id=normalized_device_id or device_id,
                config_error=error or "",
                config_message=success or "",
            )
        )

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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

    if request.form.get("return_to") == "device_detail":
        return redirect(
            url_for(
                "device_detail_page",
                device_id=normalized_device_id or device_id,
                config_error=error or "",
                config_message=success or "",
            )
        )

    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
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
    return render_login_page(
        mode="customer",
        next_url=resolve_next_url(dashboard_home_url("customer")),
    )


@app.route("/homepage")
def homepage():
    return render_login_page(
        mode="customer",
        next_url=resolve_next_url(dashboard_home_url("customer")),
    )


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
@admin_required
def device_detail_page(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    account = fetch_customer_account(scoped_device_id)
    service_config = fetch_device_service_config(scoped_device_id, account=account)
    snapshot = fetch_device_snapshot(scoped_device_id)
    return render_template(
        "device_detail.html",
        device_id=scoped_device_id,
        is_admin=True,
        customer_account=account,
        service_config=service_config,
        simulator_enabled=device_simulator_enabled(scoped_device_id, snapshot=snapshot),
        latest_firmware_artifact=fetch_latest_firmware_artifact(scoped_device_id),
        config_message=request.args.get("config_message", "", type=str) or "",
        config_error=request.args.get("config_error", "", type=str) or "",
    )


def device_simulator_state_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    return f"{DEVICE_SIMULATOR_STATE_PREFIX}{normalized_device_id}" if normalized_device_id else None


def simulator_payload_enabled(payload):
    if not payload:
        return False
    for key in (
        "simulator",
        "upper_tank_simulator",
        "main_tank_simulator",
        "source_tank_simulator",
        "lower_tank_simulator",
    ):
        value = str(payload.get(key) or "").strip().upper()
        if value in {"ON", "TRUE", "YES", "1"}:
            return True
    return False


def record_device_simulator_state(device_id, enabled, source="telemetry"):
    state_key = device_simulator_state_key(device_id)
    if not state_key:
        return
    try:
        set_app_setting(
            state_key,
            json.dumps(
                {
                    "enabled": bool(enabled),
                    "source": str(source or "unknown"),
                    "updated_at": now_utc().strftime(TIMESTAMP_FORMAT),
                },
                separators=(",", ":"),
            ),
        )
    except Exception as exc:
        logger.warning("Could not persist simulator state for %s: %s", normalize_device_id(device_id), exc)


def device_simulator_enabled(device_id, snapshot=None):
    state_key = device_simulator_state_key(device_id)
    if state_key:
        try:
            raw_state = get_app_setting(state_key)
        except Exception as exc:
            logger.warning("Could not load simulator state for %s: %s", normalize_device_id(device_id), exc)
            raw_state = None
        if raw_state:
            try:
                return bool(json.loads(raw_state).get("enabled"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    return simulator_payload_enabled(snapshot)


@app.route("/devices/<device_id>/configuration", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_configuration(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    configuration_type = str(request.form.get("configuration_type") or "master_only").strip().lower()
    master_slave_enabled = configuration_type == "master_slave"
    main_sensor_enabled = "main_sensor_enabled" in request.form
    lower_sensor_enabled = master_slave_enabled and "source_tank_monitoring_enabled" in request.form
    try:
        updated_config = upsert_device_service_config(
            scoped_device_id,
            main_sensor_enabled=main_sensor_enabled,
            slave_device_enabled=master_slave_enabled,
            source_tank_monitoring_enabled=lower_sensor_enabled,
        )
        queued_command = build_device_service_command(updated_config)
        queue_command(queued_command, target_device=scoped_device_id)
        log_audit_event(
            actor=current_actor_username(),
            action="update_device_detail_configuration",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={"service_config": updated_config, "queued_command": queued_command},
        )
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_message="Configuration saved. Device changes apply on the next command poll."))
    except ValueError as exc:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=str(exc)))


@app.route("/admin/customers/<device_id>/simulator", methods=["POST"])
@app.route("/devices/<device_id>/simulator", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_simulator(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    simulator_enabled = device_simulator_enabled(scoped_device_id, snapshot=snapshot)
    command = "SIMULATOR_OFF" if simulator_enabled else "SIMULATOR_ON"
    result = queue_command(command, target_device=scoped_device_id)
    if isinstance(result, tuple):
        payload, _status_code = result
        error = payload.get("error") or f"Unable to queue simulator command for {scoped_device_id}."
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    record_device_simulator_state(scoped_device_id, not simulator_enabled, source="admin_command")
    log_audit_event(
        actor=current_actor_username(),
        action="queue_device_simulator_toggle",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={
            "command": result.get("command"),
            "previous_simulator_enabled": simulator_enabled,
            "mqtt_delivery": result.get("mqtt_delivery"),
            "queued_at": result.get("queued_at"),
        },
    )
    message = (
        "Simulator disable command queued."
        if simulator_enabled
        else "Simulator enable command queued. The device will apply it using the current service configuration."
    )
    return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_message=message))


@app.route("/devices/<device_id>/customer-profile", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_customer_profile(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    account = fetch_customer_account(scoped_device_id)
    if not account:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error="Customer account not found for this device."))
    try:
        updated_account = update_customer_account_profile(
            scoped_device_id,
            display_name=request.form.get("display_name", ""),
            email=request.form.get("email", ""),
            service_updates_enabled=bool(account.get("service_updates_enabled", True)),
            marketing_emails_enabled=bool(account.get("marketing_emails_enabled", False)),
        )
        log_audit_event(
            actor=current_actor_username(),
            action="update_customer_profile_from_device_detail",
            target_type="customer_account",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={"display_name": updated_account.get("display_name"), "email": updated_account.get("email")},
        )
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_message="Customer profile saved."))
    except ValueError as exc:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=str(exc)))


@app.route("/devices/<device_id>/customer-password", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_customer_password(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    account = fetch_customer_account(scoped_device_id)
    if not account:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error="Customer account not found for this device."))
    try:
        update_customer_password(scoped_device_id, request.form.get("password", ""))
        log_audit_event(
            actor=current_actor_username(),
            action="reset_customer_password_from_device_detail",
            target_type="customer_account",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={"password_scope": "cloud_only"},
        )
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_message="Customer cloud password reset."))
    except ValueError as exc:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=str(exc)))


@app.route("/devices/<device_id>/status")
@admin_required
def device_detail_status(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    if not snapshot:
        return jsonify({"error": "device not found"}), 404
    simulator_enabled = device_simulator_enabled(scoped_device_id, snapshot=snapshot)
    snapshot_payload = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    snapshot_payload["simulator_enabled"] = simulator_enabled
    snapshot_payload["simulator_status"] = "ON" if simulator_enabled else "OFF"
    if simulator_enabled and not simulator_payload_enabled(snapshot_payload):
        snapshot_payload["simulator"] = "ON"
    alerts = fetch_filtered_alerts(limit=10, device_id=scoped_device_id)
    audit = fetch_audit_events(limit=10, device_id=scoped_device_id)
    history = fetch_device_history(scoped_device_id, limit=10)
    events = build_events(limit=10, device_id=scoped_device_id)
    return jsonify(
        {
            "device_id": scoped_device_id,
            "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
            "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
            "snapshot": snapshot_payload,
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
            "server": "SaleWell Smart Tank API",
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
        "server": "SaleWell Smart Tank API",
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
    mysql_config = mysql_connection_config()
    return {
        "status": "ok",
        "database": mysql_config.get("database"),
        "database_backend": DB_BACKEND,
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
    audit_limit = max(1, min(request.args.get("audit_limit", default=5, type=int), 30))
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
            "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
            "events": build_events(event_limit, device_id=scoped_device_id),
            "audit": fetch_audit_events(limit=audit_limit, device_id=scoped_device_id),
            "guidance": build_shared_guidance_payload(snapshot, None),
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


@app.route("/dashboard/local-sync", methods=["POST"])
@login_required
def dashboard_local_sync():
    response = customer_cloud_feed_block_response()
    if response:
        return response

    data = request.get_json(silent=True) or {}
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str) or data.get("device_id"))
    snapshot = load_dashboard_snapshot(scoped_device_id)
    local_base_url = normalize_device_base_url(data.get("device_local_url") or (snapshot or {}).get("device_local_url"))
    if not local_base_url:
        return jsonify({"error": "local device URL is not available"}), 404

    try:
        local_status = fetch_local_device_status(local_base_url, device_id=scoped_device_id or (snapshot or {}).get("device_id"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        logger.info("Local device sync failed for %s via %s: %s", scoped_device_id, local_base_url, exc)
        return jsonify({"error": "local device is not reachable"}), 503

    local_device_id = normalize_device_id(local_status.get("device_id"))
    if scoped_device_id and local_device_id and local_device_id != scoped_device_id:
        return jsonify({"error": "local device_id does not match dashboard device"}), 403
    if scoped_device_id:
        local_status["device_id"] = scoped_device_id
    elif local_device_id:
        local_status["device_id"] = local_device_id

    local_status["device_source"] = normalize_device_source(local_status.get("device_source"), default=DEVICE_SOURCE_REAL)
    cleaned = process_telemetry_payload(local_status, source_ip="dashboard_local_wifi", transport="dashboard_local_wifi")
    refreshed_snapshot = load_dashboard_snapshot(local_status.get("device_id") or scoped_device_id)
    refresh_operational_alerts(refreshed_snapshot if snapshot_has_live_device_data(refreshed_snapshot) else None)
    return jsonify(
        {
            "result": "saved",
            "device_id": local_status.get("device_id") or scoped_device_id,
            "snapshot": strip_ip_address_fields(refreshed_snapshot or cleaned, keep_device_local_url=True),
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
            build_unavailable_level_forecast_payload(
                scoped_device_id,
                "missing_artifact",
                "Level forecast model artifact is not installed.",
                "Train it first with: python scripts/train_level_forecast_model.py --device-id <device-id> --horizon-hours 1",
            )
        ), 200
    except ValueError as exc:
        return jsonify(
            build_unavailable_level_forecast_payload(
                scoped_device_id,
                "insufficient_telemetry",
                str(exc),
                "Collect more real telemetry for this device before using ML prediction.",
            )
        ), 200
    except RuntimeError as exc:
        logger.warning("ML prediction unavailable for %s: %s", scoped_device_id, exc)
        return jsonify(
            build_unavailable_level_forecast_payload(
                scoped_device_id,
                "ml_unavailable",
                str(exc),
                "Verify ML dependencies and retrain or replace the forecast artifact.",
            )
        ), 200
    except Exception:
        logger.exception("Unexpected ML prediction failure for %s", scoped_device_id)
        return jsonify({"error": "ML prediction failed unexpectedly.", "device_id": scoped_device_id}), 500

    return jsonify(payload)


logger.info("Initializing database")
_mysql_config_for_log = mysql_connection_config()
logger.info(
    "Database backend resolved to MySQL: host=%s port=%s database=%s user=%s",
    _mysql_config_for_log.get("host"),
    _mysql_config_for_log.get("port"),
    _mysql_config_for_log.get("database"),
    _mysql_config_for_log.get("user"),
)
validate_runtime_db_configuration()
init_db()
resolve_relay_alert_when_disabled()
ensure_app_secret_key_persisted()
maybe_reset_admin_password_on_boot()
if APP_SECRET_KEY_SOURCE == "env":
    logger.info("APP_SECRET_KEY loaded from environment.")
elif APP_SECRET_KEY_SOURCE == "default":
    logger.warning("APP_SECRET_KEY fallback is active because no persistent secret could be loaded. Set APP_SECRET_KEY before production.")
else:
    logger.info("APP_SECRET_KEY loaded from persistent storage at %s.", APP_SECRET_KEY_SOURCE)
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
    logger.info("Starting SaleWell Smart Tank Server")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)




























