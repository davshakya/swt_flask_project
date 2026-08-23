import atexit
from datetime import datetime, timedelta, timezone
from functools import wraps
import base64
import copy
import random
import csv
import gzip
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
import tempfile
import time
import threading
import math
from email.message import EmailMessage

try:
    import fcntl
except ImportError:  # Windows development/tests; Passenger production is POSIX.
    fcntl = None

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
from urllib.parse import unquote, urlparse
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
    FIRMWARE_ARTIFACT_ROLES,
    build_firmware_artifact_file_response as build_firmware_artifact_file_response_payload,
    build_firmware_artifact_payload as build_firmware_artifact_response_payload,
    firmware_artifact_storage_path as firmware_artifact_storage_path_for_dir,
    make_stored_firmware_filename,
    normalize_firmware_artifact_role,
    read_uploaded_firmware,
    sanitize_firmware_filename as sanitize_firmware_filename_value,
    validate_firmware_binary_role,
    extract_firmware_version_label as extract_firmware_version_label_from_payload,
)
from flask_app.mobile_firmware_routes import register_mobile_firmware_routes
from flask_app.device_key_vault import decrypt_device_key, encrypt_device_key
from flask_app.rag_routes import public_chat_blueprint, rag_blueprint
from flask_app.runtime_utils import (
    env_float,
    env_int,
    env_flag as runtime_env_flag,
    normalize_db_path as runtime_normalize_db_path,
    normalize_http_base_url as runtime_normalize_http_base_url,
    parse_simple_dotenv,
)
from flask_app.mysql_retry import statement_allows_connection_retry
from flask_app.capacity_features import BoundedRequestMetrics, CapacityFeatureRegistry
from flask_app.capacity_schema import ensure_capacity_schema
from flask_app.capacity_ingestion import DeviceIngestionResult, ingest_device_payload
from flask_app.capacity_state import build_alert_flags, upsert_device_latest_state
from flask_app.capacity_history import store_narrow_history_if_due
from flask_app.capacity_rollout import evaluate_device_rollout
from flask_app.capacity_reads import fetch_latest_state_payload, fetch_narrow_history_rows
from flask_app.capacity_security import DeviceTokenBucketLimiter, sequence_status

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
    # The deployed project device.env is the explicit source of device/OTA
    # credentials. cPanel can retain stale application variables across code
    # deploys and otherwise silently sign firmware tickets with an old key.
    "SWT_DEVICE_KEYS",
    "SWT_DEVICE_API_KEY",
    "SWT_TEST_DEVICE_API_KEY",
    "SWT_DEVICE_KEYS_OVERRIDE_VAULT",
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
    # device.env is the single local configuration source for Flask. Real
    # process variables still take precedence except for the explicitly
    # documented project-device overrides handled by the loader below.
    load_workspace_device_env_files(project_root, environ=os.environ)


load_local_env_files()

TELEMETRY_BACKGROUND_MAX_WORKERS = max(
    1,
    env_int("BACKGROUND_DB_MAX_WORKERS", 1),
)
telemetry_background_semaphore = threading.BoundedSemaphore(
    TELEMETRY_BACKGROUND_MAX_WORKERS
)

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
HOMEPAGE_VISITOR_COUNT_SETTING = "homepage_visitor_count"
ACTIVE_SESSION_SETTING_PREFIX = "active_session"
SESSION_PLATFORM_ANDROID = "android"
SESSION_PLATFORM_DASHBOARD = "dashboard"
DEFAULT_ANDROID_SSO_SESSION_LIMIT = 1
MAX_ANDROID_SSO_SESSION_LIMIT = 10
DEVICE_SOURCE_MODE_SETTING = "device_source_mode"
DEVICE_SIMULATOR_STATE_PREFIX = "device_simulator_state:"
DEVICE_AUTOMATION_SETTINGS_PREFIX = "device_automation_settings:"
DEVICE_LOCAL_WEB_PASSWORD_PREFIX = "device_local_web_password:"
DEFAULT_DEVICE_AUTO_START_PCT = 30.0
DEFAULT_DEVICE_AUTO_STOP_PCT = 95.0
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


CAPACITY_FEATURES = CapacityFeatureRegistry()
DEVICE_SYNC_RATE_LIMITER = DeviceTokenBucketLimiter(
    rate_per_second=max(0.1, env_float("DEVICE_SYNC_RATE_PER_SECOND", 2.0)),
    burst=max(1, env_int("DEVICE_SYNC_RATE_BURST", 5)),
    max_devices=max(100, env_int("DEVICE_SYNC_RATE_TRACKED_DEVICES", 5000)),
)
CAPACITY_REQUEST_METRICS = BoundedRequestMetrics(
    max_buckets=max(1, env_int("CAPACITY_METRICS_RETAINED_MINUTES", 60)),
)
for capacity_feature_warning in CAPACITY_FEATURES.warnings:
    logging.getLogger(__name__).warning(capacity_feature_warning)


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

    persisted_secret = str(get_app_setting(APP_SECRET_KEY_SETTING, "") or "").strip()
    if persisted_secret:
        return persisted_secret, "persistent"

    raise RuntimeError("APP_SECRET_KEY must be set explicitly when using the MySQL backend.")


def resolve_device_key_registry():
    registry_items = []
    registry_sources = []

    configured_registry = os.environ.get("DEVICE_KEYS", "").strip()
    if configured_registry:
        registry_items.append(configured_registry)
        registry_sources.append("DEVICE_KEYS")

    shared_registry = os.environ.get("SWT_DEVICE_KEYS", "").strip()
    if shared_registry:
        registry_items.append(shared_registry)
        registry_sources.append("SWT_DEVICE_KEYS")

    shared_device_id = os.environ.get("SWT_DEVICE_ID", "").strip()
    shared_device_key = os.environ.get("SWT_DEVICE_API_KEY", "").strip()
    if shared_device_id and shared_device_key:
        registry_items.append(f"{shared_device_id}:{shared_device_key}")
        registry_sources.append("SWT_DEVICE_ID/SWT_DEVICE_API_KEY")

    # Test controllers use an independent credential so development firmware
    # never shares the production device key. Previously SWT_TEST_DEVICE_API_KEY
    # was loaded but omitted from the active registry, causing every telemetry
    # upload and command poll to fail with HTTP 403.
    test_device_id = os.environ.get("SWT_TEST_DEVICE_ID", "").strip()
    test_device_key = os.environ.get("SWT_TEST_DEVICE_API_KEY", "").strip()
    if test_device_id and test_device_key:
        registry_items.append(f"{test_device_id}:{test_device_key}")
        registry_sources.append("SWT_TEST_DEVICE_ID/SWT_TEST_DEVICE_API_KEY")

    if registry_items:
        return ",".join(registry_items), ",".join(registry_sources)

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
app.register_blueprint(rag_blueprint)
app.register_blueprint(public_chat_blueprint)
if os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes", "on"}:
    # Enable only when the app is reachable exclusively through one trusted proxy.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
cors_allowed_origins = [
    origin.strip()
    for origin in os.environ.get("SWT_CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if cors_allowed_origins:
    CORS(app, resources={
        r"/status": {"origins": cors_allowed_origins},
        r"/device/command": {"origins": cors_allowed_origins},
        r"/health": {"origins": cors_allowed_origins},
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
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(days=30)
COMPRESSIBLE_RESPONSE_MIMETYPES = {
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
    "text/css",
    "text/html",
    "text/javascript",
    "text/plain",
}

DYNAMIC_HTML_CACHE_SECONDS = max(0, env_int("DYNAMIC_HTML_CACHE_SECONDS", 15))
DYNAMIC_HTML_STALE_WHILE_REVALIDATE_SECONDS = max(
    DYNAMIC_HTML_CACHE_SECONDS,
    env_int("DYNAMIC_HTML_STALE_WHILE_REVALIDATE_SECONDS", 60),
)
NO_STORE_ROUTE_PREFIXES = (
    "/api/",
    "/analytics",
    "/dashboard/bootstrap",
    "/dashboard/local-sync",
    "/device/command",
    "/device/status",
    "/events",
    "/health",
    "/history",
    "/last",
    "/ml/",
    "/monitoring/",
    "/motor/",
    "/relay/",
    "/sensor/",
    "/status",
    "/system/",
)
NO_STORE_ROUTE_SUFFIXES = (
    "/status",
    "/local-sync",
)


@app.route("/manifest.webmanifest")
def web_manifest():
    response = send_from_directory(str(STATIC_DIR), "manifest.webmanifest")
    response.headers["Content-Type"] = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=86400"
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
    request_started_at = getattr(g, "capacity_request_started_at", None)
    if request_started_at is not None:
        route_rule = request.url_rule.rule if request.url_rule is not None else "unmatched"
        CAPACITY_REQUEST_METRICS.record(
            route_rule,
            response.status_code,
            (time.perf_counter() - request_started_at) * 1000.0,
        )
    if request.path == "/static/marketing/water_flow_animation.html" or (
        request.path == "/" and request.args.get("chat_embed") == "1"
    ):
        # These first-party embeds are protected from cross-site framing while
        # remaining available inside SaleWell pages on the same origin.
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    else:
        response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.method == "GET" and request.path.startswith("/static/"):
        # Standalone HTML pages must be revalidated. Caching them as immutable can
        # reopen an obsolete dashboard after navigation until the user refreshes.
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=2592000, immutable")
    elif request.method == "GET":
        apply_dynamic_cache_headers(response)
    if should_gzip_response(response):
        gzip_response(response)
    return response


@app.before_request
def start_capacity_request_timing():
    if CAPACITY_FEATURES.enabled("request_timing") or CAPACITY_FEATURES.enabled("capacity_metrics"):
        g.capacity_request_started_at = time.perf_counter()


def route_should_not_store(path):
    normalized_path = str(path or request.path or "")
    if normalized_path in {"/", "/homepage", "/service-worker.js", "/static/version.json"}:
        return True
    if normalized_path.startswith(NO_STORE_ROUTE_PREFIXES):
        return True
    return any(normalized_path.endswith(suffix) for suffix in NO_STORE_ROUTE_SUFFIXES)


def apply_dynamic_cache_headers(response):
    if route_should_not_store(request.path):
        response.headers.setdefault("Cache-Control", "no-store")
        return

    if response.status_code < 200 or response.status_code >= 300:
        response.headers.setdefault("Cache-Control", "no-store")
        return

    if response.mimetype == "text/html":
        response.headers.setdefault(
            "Cache-Control",
            (
                f"private, max-age={DYNAMIC_HTML_CACHE_SECONDS}, "
                f"stale-while-revalidate={DYNAMIC_HTML_STALE_WHILE_REVALIDATE_SECONDS}"
            ),
        )
        response.headers["Vary"] = append_vary_header(response.headers.get("Vary"), "Cookie")
        try:
            response.add_etag(weak=True)
            response.make_conditional(request)
        except RuntimeError:
            pass
        return

    response.headers.setdefault("Cache-Control", "no-store")


def should_gzip_response(response):
    if request.method != "GET":
        return False
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return False
    if response.status_code < 200 or response.status_code >= 300:
        return False
    if response.direct_passthrough:
        return False
    if response.headers.get("Content-Encoding"):
        return False
    if response.mimetype not in COMPRESSIBLE_RESPONSE_MIMETYPES:
        return False
    content_length = response.calculate_content_length()
    return content_length is None or content_length >= 1024


def gzip_response(response):
    payload = response.get_data()
    if len(payload) < 1024:
        return response
    compressed_payload = gzip.compress(payload, compresslevel=6)
    if len(compressed_payload) >= len(payload):
        return response
    response.set_data(compressed_payload)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(compressed_payload))
    response.headers["Vary"] = append_vary_header(response.headers.get("Vary"), "Accept-Encoding")
    return response


def append_vary_header(current_value, header_name):
    values = [value.strip() for value in str(current_value or "").split(",") if value.strip()]
    if header_name.lower() not in {value.lower() for value in values}:
        values.append(header_name)
    return ", ".join(values)


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
OTA_CONTRACT_VERSION = 2
DEPLOY_MARKER = "device-detail-recovery-2026-07-26-v2"
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
SHOW_PRICING_LINKS = env_flag("SWT_SHOW_PRICING_LINKS", default=False)
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
STALE_AFTER_SECONDS = env_int("DATA_STALE_AFTER_SECONDS", 300)
CLOUD_POLL_INTERVAL_SECONDS = max(30, env_int("SWT_CLOUD_POLL_INTERVAL_SECONDS", 30))
DIRECT_PEER_STALE_AFTER_SECONDS = max(1, env_int("DIRECT_PEER_STALE_AFTER_SECONDS", 15))
DATA_RETENTION_DAYS = max(1, env_int("DATA_RETENTION_DAYS", 30))
DEVICE_EVENT_RETENTION_DAYS = max(1, env_int("DEVICE_EVENT_RETENTION_DAYS", DATA_RETENTION_DAYS))
TELEMETRY_HISTORY_ENABLED = env_flag("TELEMETRY_HISTORY_ENABLED", default=True)
TELEMETRY_LOCAL_SYNC_DEDUPE_WINDOW_SECONDS = max(1, env_int("TELEMETRY_LOCAL_SYNC_DEDUPE_WINDOW_SECONDS", 10))
LOCAL_SYNC_TRANSPORTS = {"android_local_wifi", "dashboard_local_wifi"}
MAX_TELEMETRY_ROWS_PER_DEVICE = max(0, env_int("MAX_TELEMETRY_ROWS_PER_DEVICE", 65000))
DEVICE_COMMAND_RETENTION_DAYS = max(1, env_int("DEVICE_COMMAND_RETENTION_DAYS", 7))
RUNTIME_SYNC_COMMAND_MIN_INTERVAL_SECONDS = max(30, env_int("RUNTIME_SYNC_COMMAND_MIN_INTERVAL_SECONDS", 600))
RUNTIME_SYNC_MANUAL_COMMAND_COOLDOWN_SECONDS = max(
    30,
    env_int("RUNTIME_SYNC_MANUAL_COMMAND_COOLDOWN_SECONDS", 180),
)
OPS_ALERT_RETENTION_DAYS = max(1, env_int("OPS_ALERT_RETENTION_DAYS", 30))
OPS_AUDIT_RETENTION_DAYS = max(1, env_int("OPS_AUDIT_RETENTION_DAYS", 30))
DB_MAINTENANCE_ENABLED = env_flag("DB_MAINTENANCE_ENABLED", default=True)
MYSQL_OPTIMIZE_ENABLED = env_flag("MYSQL_OPTIMIZE_ENABLED", default=True)
DB_TARGET_SIZE_MB = max(0.0, env_float("DB_TARGET_SIZE_MB", 256.0 if IS_RENDER else 0.0))
DB_TARGET_SIZE_BYTES = int(DB_TARGET_SIZE_MB * 1024 * 1024)
DB_MAINTENANCE_MIN_INTERVAL_SECONDS = max(60, env_int("DB_MAINTENANCE_MIN_INTERVAL_SECONDS", 900 if IS_RENDER else 3600))
DB_WAL_AUTOCHECKPOINT_PAGES = max(100, env_int("DB_WAL_AUTOCHECKPOINT_PAGES", 1000))
DB_PRUNE_MIN_INTERVAL_SECONDS = max(0, env_int("DB_PRUNE_MIN_INTERVAL_SECONDS", 30 if IS_RENDER else 15))
DB_RETENTION_DELETE_BATCH_ROWS = max(100, env_int("DB_RETENTION_DELETE_BATCH_ROWS", 5000))
DB_RETENTION_DELETE_MAX_BATCHES = max(1, env_int("DB_RETENTION_DELETE_MAX_BATCHES", 4))
DB_RETENTION_DELETE_FORCE_MAX_BATCHES = max(
    DB_RETENTION_DELETE_MAX_BATCHES,
    env_int("DB_RETENTION_DELETE_FORCE_MAX_BATCHES", 24),
)
DB_OPTIMIZE_AFTER_PRUNE_ROWS = max(0, env_int("DB_OPTIMIZE_AFTER_PRUNE_ROWS", 5000))
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
analytics_build_locks = {}
analytics_build_locks_guard = threading.Lock()
fixed_ai_dashboard_cache = {}
dashboard_snapshot_cache = {}
dashboard_summary_cache = {}
dashboard_summary_refresh_pending = set()
dashboard_summary_refresh_lock = threading.Lock()
dashboard_summary_last_refresh_at = {}
level_forecast_model_cache = {}
SNAPSHOT_CACHE_TTL_SECONDS = max(0.0, env_float("SNAPSHOT_CACHE_TTL_SECONDS", 2.0))
DASHBOARD_SUMMARY_RECONCILE_SECONDS = max(
    600,
    min(900, env_int("DASHBOARD_SUMMARY_RECONCILE_SECONDS", 600)),
)
DASHBOARD_SUMMARY_RECONCILIATION_ENABLED = env_flag(
    "DASHBOARD_SUMMARY_RECONCILIATION_ENABLED", default=False
)
DASHBOARD_SUMMARY_MIN_REFRESH_SECONDS = max(
    5, min(300, env_int("DASHBOARD_SUMMARY_MIN_REFRESH_SECONDS", 30))
)
DASHBOARD_SUMMARY_RECONCILE_BATCH_SIZE = max(
    1, min(50, env_int("DASHBOARD_SUMMARY_RECONCILE_BATCH_SIZE", 10))
)
ANALYTICS_MAX_GAP_MINUTES = env_int("ANALYTICS_MAX_GAP_MINUTES", 20)
ANALYTICS_MAX_LEVEL_DELTA_PCT = env_float("ANALYTICS_MAX_LEVEL_DELTA_PCT", 25.0)
ANALYTICS_MIN_USAGE_DELTA_PCT = max(0.05, env_float("ANALYTICS_MIN_USAGE_DELTA_PCT", 0.15))
ANALYTICS_MIN_REFILL_DELTA_PCT = max(0.5, env_float("ANALYTICS_MIN_REFILL_DELTA_PCT", 3.0))
ANALYTICS_MIN_FULL_FILL_DELTA_PCT = max(10.0, env_float("ANALYTICS_MIN_FULL_FILL_DELTA_PCT", 69.0))
ANALYTICS_FILL_CONFIRM_INTERVALS = max(2, env_int("ANALYTICS_FILL_CONFIRM_INTERVALS", 2))
ANALYTICS_FILL_STOP_INTERVALS = max(2, env_int("ANALYTICS_FILL_STOP_INTERVALS", 2))
ANALYTICS_STOP_THRESHOLD_TOLERANCE_PCT = max(
    0.0, env_float("ANALYTICS_STOP_THRESHOLD_TOLERANCE_PCT", 2.0)
)
ANALYTICS_MIN_BASELINE_USAGE_PCT = env_float("ANALYTICS_MIN_BASELINE_USAGE_PCT", 1.0)
ANALYTICS_MAX_DAILY_TANK_TURNOVERS = max(
    1.0, env_float("ANALYTICS_MAX_DAILY_TANK_TURNOVERS", 8.0)
)
ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR = env_float("ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR", 0.05)
AI_LEAK_ALERT_MIN_CONFIDENCE = max(90.0, min(99.0, env_float("AI_LEAK_ALERT_MIN_CONFIDENCE", 90.0)))
ANALYTICS_CACHE_TTL_SECONDS = max(30.0, env_float("ANALYTICS_CACHE_TTL_SECONDS", 300.0))
ANALYTICS_SYNC_EVENTS_ON_REQUEST = env_flag("ANALYTICS_SYNC_EVENTS_ON_REQUEST", default=False)
FIXED_AI_CACHE_TTL_SECONDS = max(30.0, env_float("FIXED_AI_CACHE_TTL_SECONDS", 300.0))
ANALYTICS_CACHE_MAX_ENTRIES = max(1, env_int("ANALYTICS_CACHE_MAX_ENTRIES", 8 if IS_RENDER else 24))
ANALYTICS_LAST_VALID_SETTING_PREFIX = "analytics:last-valid:"
ANALYTICS_LAST_VALID_SCHEMA_VERSION = 5
ANALYTICS_ALGORITHM_VERSION = "relay-observed-minimum-to-90-ai7-v4"
ANALYTICS_LEVEL_SERIES_MAX_POINTS = max(60, env_int("ANALYTICS_LEVEL_SERIES_MAX_POINTS", 240 if IS_RENDER else 480))
ANALYTICS_MOTOR_SERIES_MAX_POINTS = max(40, env_int("ANALYTICS_MOTOR_SERIES_MAX_POINTS", 120 if IS_RENDER else 240))
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
ANDROID_RELEASE_DIR = normalize_db_path(
    os.environ.get("ANDROID_RELEASE_DIR", str(DATA_DIR / "android_releases"))
)
ANDROID_RELEASE_MAX_BYTES = max(1024 * 1024, env_int("ANDROID_RELEASE_MAX_MB", 128) * 1024 * 1024)
ALERT_WEBHOOK_URL = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
WHATSAPP_WEBHOOK_URL = os.environ.get("WHATSAPP_WEBHOOK_URL", "").strip()
WHATSAPP_TEAM_PHONE = os.environ.get("WHATSAPP_TEAM_PHONE", "918796452878").strip()
WHATSAPP_WEBHOOK_SECRET = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "").strip()
WHATSAPP_PROVIDER = os.environ.get("WHATSAPP_PROVIDER", "meta").strip().lower()
WHATSAPP_META_GRAPH_VERSION = os.environ.get("WHATSAPP_META_GRAPH_VERSION", "v24.0").strip()
WHATSAPP_META_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_META_PHONE_NUMBER_ID", "").strip()
WHATSAPP_META_ACCESS_TOKEN = os.environ.get("WHATSAPP_META_ACCESS_TOKEN", "").strip()
WHATSAPP_META_CONFIRMATION_TEMPLATE = os.environ.get(
    "WHATSAPP_META_CONFIRMATION_TEMPLATE",
    "salewell_demo_confirmation",
).strip()
WHATSAPP_META_TEAM_TEMPLATE = os.environ.get(
    "WHATSAPP_META_TEAM_TEMPLATE",
    "salewell_team_new_enquiry",
).strip()
WHATSAPP_META_TEMPLATE_LANGUAGE = os.environ.get("WHATSAPP_META_TEMPLATE_LANGUAGE", "en").strip()
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


class SwtColorFormatter(logging.Formatter):
    # Keep the timezone converter on this formatter only. Assigning it to
    # logging.Formatter globally turns a plain function into a bound method on
    # some Python deployments, which passes ``self`` plus the timestamp and
    # makes logging fail while handling another exception.
    converter = staticmethod(logging_ist_converter)

    RESET = "\033[0m"
    TIMESTAMP_COLOR = "\033[36m"
    LEVEL_COLORS = {
        logging.DEBUG: "\033[34m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[31;1m",
    }

    def __init__(self, color_enabled=True):
        super().__init__(
            fmt="[%(asctime)s+05:30] [%(levelname)s] %(message)s",
            datefmt=TIMESTAMP_FORMAT,
        )
        self.color_enabled = color_enabled

    def format(self, record):
        rendered = super().format(record)
        if not self.color_enabled:
            return rendered
        timestamp = self.formatTime(record, self.datefmt)
        timestamp_token = f"[{timestamp}+05:30]"
        level_token = f"[{record.levelname}]"
        rendered = rendered.replace(
            timestamp_token,
            f"{self.TIMESTAMP_COLOR}{timestamp_token}{self.RESET}",
            1,
        )
        level_color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        return rendered.replace(level_token, f"{level_color}{level_token}{self.RESET}", 1)


SWT_LOG_COLOR_ENABLED = os.environ.get("SWT_LOG_COLOR", "false").strip().lower() in {"1", "true", "yes", "on"}
app_log_handler = logging.StreamHandler()
app_log_handler.setFormatter(SwtColorFormatter(color_enabled=SWT_LOG_COLOR_ENABLED))
logging.basicConfig(level=APP_LOG_LEVEL, handlers=[app_log_handler], force=True)

logger = logging.getLogger("tank_server")
device_connection_logger = logging.getLogger("tank_server.device_connection")
device_connection_logger.setLevel(logging.INFO)
DEVICE_CONNECTION_LOG_HEARTBEAT_SECONDS = max(
    30, env_int("DEVICE_CONNECTION_LOG_HEARTBEAT_SECONDS", 300)
)
device_connection_log_lock = threading.Lock()
device_connection_log_state = {}


def log_device_connection_status(device_id, reachable, *, telemetry_status=None, seconds_since_sync=None):
    """Log connection transitions plus a bounded heartbeat for each device."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    status = "reachable" if bool(reachable) else "unreachable"
    now_monotonic = time.monotonic()
    with device_connection_log_lock:
        previous = device_connection_log_state.get(normalized_device_id)
        changed = previous is None or previous["status"] != status
        heartbeat_due = previous is None or (
            now_monotonic - previous["logged_at"] >= DEVICE_CONNECTION_LOG_HEARTBEAT_SECONDS
        )
        if not changed and not heartbeat_due:
            return
        device_connection_log_state[normalized_device_id] = {
            "status": status,
            "logged_at": now_monotonic,
        }
        if len(device_connection_log_state) > 1000:
            oldest_device_id = min(
                device_connection_log_state,
                key=lambda key: device_connection_log_state[key]["logged_at"],
            )
            if oldest_device_id != normalized_device_id:
                device_connection_log_state.pop(oldest_device_id, None)
    device_connection_logger.info(
        "Device connection status: device=%s status=%s telemetry=%s seconds_since_sync=%s",
        normalized_device_id,
        status,
        str(telemetry_status or "unknown").strip().lower() or "unknown",
        seconds_since_sync if seconds_since_sync is not None else "--",
    )


relay_lock = threading.Lock()
level_forecast_model_lock = threading.Lock()
db_maintenance_lock = threading.Lock()
db_prune_lock = threading.Lock()
telemetry_postprocess_lock = threading.Lock()
telemetry_postprocess_pending = {}
telemetry_postprocess_running = set()
telemetry_postprocess_last_run_at = {}
homepage_visitor_count_lock = threading.Lock()
homepage_visitor_count_cached = None
homepage_visitor_count_pending = 0
homepage_visitor_count_worker_running = False
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
authenticated_device_key_lock = threading.Lock()
authenticated_device_key_cache = {}
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
    "last_action": None,
    "last_pruned_rows": 0,
    "last_tables": [],
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
    env_reference = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", text)
    if env_reference:
        return os.environ.get(env_reference.group(1), "").strip()
    # Keep backward compatibility with existing registries that use a bare
    # SWT_*_API_KEY variable name instead of $VAR or ${VAR} syntax.
    if re.fullmatch(r"SWT_[A-Za-z0-9_]*API_KEY", text):
        return os.environ.get(text, "").strip()
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
    normalized_device_id = normalize_device_id(device_id)
    matched_rule = find_matching_device_key_rule(device_id)
    configured_registry_overrides_vault = env_flag("SWT_DEVICE_KEYS_OVERRIDE_VAULT", default=False)
    if configured_registry_overrides_vault and matched_rule:
        return matched_rule.get("key")

    with authenticated_device_key_lock:
        cached_key = authenticated_device_key_cache.get(normalized_device_id)
    if cached_key:
        return cached_key
    persisted_key = fetch_persisted_device_key(normalized_device_id)
    if persisted_key:
        return persisted_key

    # Environment rules are bootstrap/fallback credentials. A key observed in
    # a successful device check-in is authoritative after rotation and must be
    # preferred for OTA signing.
    if matched_rule:
        return matched_rule.get("key")

    shared_swt_key = os.environ.get("SWT_DEVICE_API_KEY", "").strip()
    if (
        normalized_device_id.startswith("swt-")
        and shared_swt_key
        and not device_config_value_is_placeholder(shared_swt_key)
    ):
        return shared_swt_key
    return None


def remember_authenticated_device_key(device_id, device_key):
    """Keep a successfully verified raw key available for short-lived OTA signing.

    Persisted device credentials intentionally contain only a SHA-256 hash.  The
    running process still needs the raw key to sign an artifact-scoped MCU OTA
    ticket, so retain it only in memory after normal device authentication.
    """
    normalized_device_id = normalize_device_id(device_id)
    normalized_device_key = str(device_key or "").strip()
    if not normalized_device_id or not normalized_device_key:
        return
    with authenticated_device_key_lock:
        if authenticated_device_key_cache.get(normalized_device_id) == normalized_device_key:
            return
        authenticated_device_key_cache[normalized_device_id] = normalized_device_key
    try:
        encrypted_key = encrypt_device_key(normalized_device_key, APP_SECRET_KEY)
        with get_db() as db:
            db.execute(
                """
                INSERT INTO device_auth_keys(
                    device_id, device_key_hash, device_key_ciphertext, registration_source,
                    first_seen_at, last_seen_at, updated_at
                )
                VALUES (?, ?, ?, 'authenticated_checkin', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(device_id) DO UPDATE SET
                    device_key_ciphertext=excluded.device_key_ciphertext,
                    last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (normalized_device_id, hash_device_api_key(normalized_device_key), encrypted_key),
            )
    except Exception:
        # Credential persistence augments an already successful device login.
        # A pending schema migration or transient DB failure must never turn
        # telemetry/command authentication into HTTP 500. The in-memory key
        # remains usable for OTA in this worker and a later check-in retries.
        logger.exception("Could not persist the OTA signing key for %s", normalized_device_id)


def fetch_persisted_device_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT device_key_ciphertext FROM device_auth_keys WHERE device_id = ? LIMIT 1",
                (normalized_device_id,),
            ).fetchone()
    except Exception:
        logger.exception("Could not load the OTA signing key for %s", normalized_device_id)
        return None
    raw_key = decrypt_device_key(row["device_key_ciphertext"], APP_SECRET_KEY) if row else ""
    if raw_key:
        with authenticated_device_key_lock:
            authenticated_device_key_cache[normalized_device_id] = raw_key
    return raw_key or None


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


def registered_device_auth_rule_matches(device_id, device_key):
    registered_rule = fetch_auto_registered_device_auth_rule(device_id)
    if not registered_rule:
        return None
    expected_hash = str(registered_rule.get("key_hash") or "")
    provided_hash = hash_device_api_key(device_key)
    if hmac.compare_digest(provided_hash, expected_hash):
        return registered_rule
    return None


def remember_auto_registered_device_key(device_id, device_key, remote_addr=None, registration_source="auto_activation"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    device_key_hash = hash_device_api_key(device_key)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO device_auth_keys(device_id, device_key_hash, device_key_ciphertext, registration_source, first_seen_at, last_seen_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                device_key_hash=excluded.device_key_hash,
                device_key_ciphertext=excluded.device_key_ciphertext,
                registration_source=excluded.registration_source,
                last_seen_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP
            """,
            (normalized_device_id, device_key_hash, encrypt_device_key(device_key, APP_SECRET_KEY), registration_source),
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
    remember_authenticated_device_key(normalized_device_id, device_key)
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
    if mysql_is_lock_error(exc):
        return True
    message = str(exc or "").strip().lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "database is busy" in message
        or "lock wait timeout" in message
        or "deadlock found" in message
    )


def run_with_database_lock_retries(
    operation,
    *,
    operation_name="database operation",
    attempts=6,
    initial_delay_s=0.5,
):
    last_exc = None
    for attempt in range(max(1, int(attempts or 1))):
        try:
            return operation()
        except Exception as exc:
            if not database_is_locked_error(exc) or attempt >= max(1, int(attempts or 1)) - 1:
                raise
            last_exc = exc
            # Back off with jitter so simultaneous web workers do not retry the
            # same conflicting write in lockstep.
            base_delay_s = max(0.0, float(initial_delay_s or 0.0))
            delay_s = base_delay_s * (2 ** attempt)
            if delay_s > 0:
                delay_s += random.uniform(0.0, min(0.25, delay_s * 0.25))
            logger.warning(
                "Retrying %s after database lock/deadlock (%s/%s): %s",
                operation_name,
                attempt + 1,
                max(1, int(attempts or 1)),
                exc,
            )
            if delay_s > 0:
                time.sleep(delay_s)
    if last_exc is not None:
        raise last_exc


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
        "device_mobile_action_queue": 0,
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
            deleted_counts["device_mobile_action_queue"] += int(
                db.execute(
                    "DELETE FROM device_mobile_action_queue WHERE target_device = ?",
                    (normalized_device_id,),
                ).rowcount or 0
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


DEVICE_PURGE_DIRECT_COLUMNS = ("device_id", "target_device")
DEVICE_PURGE_TARGET_TABLES = {"ops_audit_log"}
DEVICE_PURGE_JSON_DEVICE_COLUMNS = {"relay_queue": ("payload",)}


def sql_like_escape(value, escape_char="="):
    text = str(value or "")
    return (
        text.replace(escape_char, escape_char + escape_char)
        .replace("%", escape_char + "%")
        .replace("_", escape_char + "_")
    )


def json_device_id_like_patterns(device_id):
    escaped_device_id = sql_like_escape(device_id)
    return (
        f'%"device_id":"{escaped_device_id}"%',
        f'%"device_id": "{escaped_device_id}"%',
    )


def list_database_table_names(cursor):
    if USING_MYSQL:
        rows = cursor.execute(
            """
            SELECT TABLE_NAME AS table_name
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """
        ).fetchall()
        return [str(row["table_name"] if isinstance(row, dict) else row[0]) for row in rows]

    rows = cursor.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """
    ).fetchall()
    return [str(row["name"] if isinstance(row, dict) else row[0]) for row in rows]


def table_column_names(cursor, table_name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(table_name or "")):
        return set()

    def inspect_columns():
        try:
            rows = cursor.execute(f"PRAGMA table_info({table_name})").fetchall()
        except Exception as exc:
            logger.warning("Could not inspect database table %s for device purge: %s", table_name, exc)
            return set()
        return {str(row[1]) for row in rows}

    try:
        return run_with_database_lock_retries(
            inspect_columns,
            operation_name=f"inspect database columns for {table_name}",
            attempts=6,
            initial_delay_s=0.5,
        )
    except Exception as exc:
        if database_is_locked_error(exc):
            logger.warning("Could not inspect database table %s for device purge after retries: %s", table_name, exc)
            return set()
        raise


def device_scoped_app_setting_keys(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []

    keys = [
        f"{DEVICE_SIMULATOR_STATE_PREFIX}{normalized_device_id}",
        f"{DEVICE_AUTOMATION_SETTINGS_PREFIX}{normalized_device_id}",
        f"{DEVICE_LOCAL_WEB_PASSWORD_PREFIX}{normalized_device_id}",
        mobile_session_epoch_setting_key(normalized_device_id),
        active_session_setting_key(
            SESSION_PLATFORM_ANDROID,
            "customer",
            username=normalized_device_id,
            device_id=normalized_device_id,
        ),
        active_session_setting_key(
            SESSION_PLATFORM_DASHBOARD,
            "customer",
            username=normalized_device_id,
            device_id=normalized_device_id,
        ),
    ]
    return list(dict.fromkeys(key for key in keys if key))


def purge_device_app_settings(cursor, device_id):
    normalized_device_id = normalize_device_id(device_id)

    def delete_app_settings():
        deleted_rows = 0
        key_identifier = quote_mysql_identifier("key")
        for setting_key in device_scoped_app_setting_keys(device_id):
            deleted_rows += int(
                cursor.execute(f"DELETE FROM app_settings WHERE {key_identifier} = ?", (setting_key,)).rowcount or 0
            )
        if normalized_device_id:
            deleted_rows += int(
                cursor.execute(
                    f"DELETE FROM app_settings WHERE {key_identifier} LIKE ?",
                    (f"{ANALYTICS_LAST_VALID_SETTING_PREFIX}{normalized_device_id}:%",),
                ).rowcount or 0
            )
        return deleted_rows

    return run_with_database_lock_retries(
        delete_app_settings,
        operation_name=f"purge app settings for {normalized_device_id or device_id}",
        attempts=6,
        initial_delay_s=0.5,
    )


def purge_device_table_rows(cursor, table_name, normalized_device_id):
    def delete_rows():
        columns = table_column_names(cursor, table_name)
        if not columns:
            return None

        conditions = []
        params = []
        for column_name in DEVICE_PURGE_DIRECT_COLUMNS:
            if column_name in columns:
                conditions.append(f"{quote_mysql_identifier(column_name)} = ?")
                params.append(normalized_device_id)

        if (
            table_name in DEVICE_PURGE_TARGET_TABLES
            and "target_type" in columns
            and "target_id" in columns
        ):
            conditions.append(
                f"({quote_mysql_identifier('target_type')} = ? AND {quote_mysql_identifier('target_id')} = ?)"
            )
            params.extend(("device", normalized_device_id))

        for column_name in DEVICE_PURGE_JSON_DEVICE_COLUMNS.get(table_name, ()):
            if column_name in columns:
                for pattern in json_device_id_like_patterns(normalized_device_id):
                    conditions.append(f"{quote_mysql_identifier(column_name)} LIKE ? ESCAPE '='")
                    params.append(pattern)

        if not conditions:
            return None

        cursor.execute(
            f"DELETE FROM {quote_mysql_identifier(table_name)} WHERE {' OR '.join(conditions)}",
            tuple(params),
        )
        return int(cursor.rowcount or 0)

    return run_with_database_lock_retries(
        delete_rows,
        operation_name=f"purge table rows for {table_name}",
        attempts=6,
        initial_delay_s=0.5,
    )

def add_deleted_device_marker(cursor, normalized_device_id, note="admin_delete"):
    def insert_marker():
        return int(
            cursor.execute(
                """
                INSERT INTO ignored_devices(device_id, note, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(device_id) DO UPDATE SET
                    note=excluded.note,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (normalized_device_id, note),
            ).rowcount or 0
        )

    return run_with_database_lock_retries(
        insert_marker,
        operation_name=f"mark deleted device {normalized_device_id}",
        attempts=6,
        initial_delay_s=0.5,
    )


def purge_device_data_fallback(cursor, normalized_device_id):
    deleted_counts = {}
    direct_deletes = [
        ("tank_data", "device_id"),
        ("device_events", "device_id"),
        ("ops_alerts", "device_id"),
        ("registered_devices", "device_id"),
        ("device_auth_keys", "device_id"),
        ("device_service_configs", "device_id"),
        ("device_command_queue", "target_device"),
        ("device_mobile_action_queue", "target_device"),
        ("customer_accounts", "device_id"),
        ("customer_password_reset_tokens", "device_id"),
        ("firmware_artifacts", "target_device"),
    ]
    for table_name, column_name in direct_deletes:
        try:
            deleted_rows = int(
                cursor.execute(
                    f"DELETE FROM {quote_mysql_identifier(table_name)} WHERE {quote_mysql_identifier(column_name)} = ?",
                    (normalized_device_id,),
                ).rowcount or 0
            )
        except Exception as exc:
            logger.warning("Fallback purge skipped %s for %s: %s", table_name, normalized_device_id, exc)
            continue
        if deleted_rows:
            deleted_counts[table_name] = deleted_rows

    try:
        app_settings_count = purge_device_app_settings(cursor, normalized_device_id)
    except Exception as exc:
        logger.warning("Fallback purge skipped app_settings for %s: %s", normalized_device_id, exc)
        app_settings_count = 0
    if app_settings_count:
        deleted_counts["app_settings"] = app_settings_count

    return deleted_counts


def purge_device_data(device_id, remember_deleted_device=False):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    def execute_purge():
        deleted_counts = {}
        if remember_deleted_device:
            # Mark the device as ignored before the main purge so new telemetry
            # check-ins are rejected instead of racing with row deletion.
            with get_db() as db:
                add_deleted_device_marker(db.cursor(), normalized_device_id)

        try:
            with get_db() as db:
                cursor = db.cursor()
                for table_name in list_database_table_names(cursor):
                    if remember_deleted_device and table_name == "ignored_devices":
                        continue
                    deleted_rows = purge_device_table_rows(cursor, table_name, normalized_device_id)
                    if deleted_rows is not None:
                        deleted_counts[table_name] = deleted_rows

                app_settings_count = purge_device_app_settings(cursor, normalized_device_id)
                if app_settings_count or "app_settings" not in deleted_counts:
                    deleted_counts["app_settings"] = app_settings_count

            # Commit source deletions before the final pass so generated event rows cannot
            # be recreated from pre-purge telemetry seen by another request/thread.
            with get_db() as db:
                cursor = db.cursor()
                for table_name in list_database_table_names(cursor):
                    if remember_deleted_device and table_name == "ignored_devices":
                        continue
                    deleted_rows = purge_device_table_rows(cursor, table_name, normalized_device_id)
                    if deleted_rows is not None:
                        deleted_counts[table_name] = int(deleted_counts.get(table_name) or 0) + deleted_rows

                app_settings_count = purge_device_app_settings(cursor, normalized_device_id)
                if app_settings_count:
                    deleted_counts["app_settings"] = int(deleted_counts.get("app_settings") or 0) + app_settings_count
        except Exception as exc:
            if not database_is_locked_error(exc):
                raise
            logger.warning(
                "Primary device purge hit lock pressure for %s; falling back to direct row cleanup before marking ignored: %s",
                normalized_device_id,
                exc,
            )
            deleted_counts = {}
            with get_db() as db:
                cursor = db.cursor()
                deleted_counts.update(purge_device_data_fallback(cursor, normalized_device_id))
                if remember_deleted_device:
                    marker_count = add_deleted_device_marker(cursor, normalized_device_id)
                    deleted_counts["ignored_devices"] = int(deleted_counts.get("ignored_devices") or 0) + marker_count
                else:
                    deleted_counts["ignored_devices"] = 0

        if remember_deleted_device:
            with get_db() as db:
                marker_count = add_deleted_device_marker(db.cursor(), normalized_device_id)
                deleted_counts["ignored_devices"] = int(deleted_counts.get("ignored_devices") or 0) + marker_count

        forget_registered_device_touch(normalized_device_id)
        clear_runtime_caches(normalized_device_id)
        return deleted_counts

    return run_with_database_lock_retries(
        execute_purge,
        operation_name=f"purge device data for {normalized_device_id}",
    )


def deleted_row_total(deleted_counts):
    return sum(int(value or 0) for value in (deleted_counts or {}).values() if isinstance(value, (int, float)))


def delete_known_device(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")
    return purge_device_data(normalized_device_id, remember_deleted_device=True)


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


def invalidate_dashboard_summary_memory(device_id=None):
    """Drop only the disposable in-process copy; the database remains the fallback."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        dashboard_summary_cache.clear()
        return
    for mode in (DEVICE_SOURCE_REAL, DEVICE_SOURCE_VIRTUAL):
        dashboard_summary_cache.pop(f"{mode}:{normalized_device_id}", None)


def dashboard_identity_prefix(role):
    return "admin" if role == "admin" else "customer"


def normalize_session_platform(platform):
    normalized = str(platform or "").strip().lower()
    return normalized if normalized in {SESSION_PLATFORM_ANDROID, SESSION_PLATFORM_DASHBOARD} else ""


def user_session_identity(role, username=None, device_id=None):
    resolved_role = "admin" if str(role or "").strip() == "admin" else "customer"
    resolved_identity = normalize_device_id(device_id or username) if resolved_role == "customer" else str(username or LOGIN_USERNAME).strip()
    if not resolved_identity:
        return None
    return f"{resolved_role}:{resolved_identity}"


def active_session_setting_key(platform, role, username=None, device_id=None):
    normalized_platform = normalize_session_platform(platform)
    identity = user_session_identity(role, username=username, device_id=device_id)
    if not normalized_platform or not identity:
        return None
    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"{ACTIVE_SESSION_SETTING_PREFIX}:{normalized_platform}:{identity_hash}"


def mobile_session_epoch_setting_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    identity_hash = hashlib.sha256(f"customer:{normalized_device_id}".encode("utf-8")).hexdigest()
    return f"{ACTIVE_SESSION_SETTING_PREFIX}:android_epoch:{identity_hash}"


def current_mobile_session_epoch(device_id):
    key = mobile_session_epoch_setting_key(device_id)
    if not key:
        return ""
    return str(get_app_setting(key, "") or "").strip()


def rotate_mobile_session_epoch(device_id):
    key = mobile_session_epoch_setting_key(device_id)
    if not key:
        return ""
    epoch = secrets.token_urlsafe(18)
    set_app_setting(key, epoch)
    return epoch


def new_platform_session_id():
    return secrets.token_urlsafe(32)


def normalize_android_sso_session_limit(value, default=DEFAULT_ANDROID_SSO_SESSION_LIMIT):
    try:
        resolved = int(str(value).strip())
    except (TypeError, ValueError):
        resolved = default
    return max(1, min(resolved, MAX_ANDROID_SSO_SESSION_LIMIT))


def active_platform_session_limit(platform, role, username=None, device_id=None):
    if normalize_session_platform(platform) != SESSION_PLATFORM_ANDROID:
        return 1
    if str(role or "").strip() != "customer":
        return 1
    normalized_device_id = normalize_device_id(device_id or username)
    if not normalized_device_id:
        return 1
    service_config = fetch_device_service_config(normalized_device_id)
    return normalize_android_sso_session_limit(service_config.get("android_sso_session_limit"))


def parse_active_platform_sessions(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = []
        if isinstance(decoded, list):
            return [str(item).strip() for item in decoded if str(item or "").strip()]
    return [raw]


def serialize_active_platform_sessions(session_ids):
    normalized = [str(item).strip() for item in (session_ids or []) if str(item or "").strip()]
    return json.dumps(normalized, separators=(",", ":"))


def active_platform_sessions(platform, role, username=None, device_id=None):
    setting_key = active_session_setting_key(platform, role, username=username, device_id=device_id)
    if not setting_key:
        return []
    return parse_active_platform_sessions(get_app_setting(setting_key, ""))


def active_platform_session_count(platform, role, username=None, device_id=None):
    return len(active_platform_sessions(platform, role, username=username, device_id=device_id))


def register_active_platform_session(platform, role, username=None, device_id=None, session_id=None):
    setting_key = active_session_setting_key(platform, role, username=username, device_id=device_id)
    if not setting_key:
        return ""
    next_session_id = str(session_id or new_platform_session_id()).strip()
    active_sessions = parse_active_platform_sessions(get_app_setting(setting_key, ""))
    active_sessions = [item for item in active_sessions if item != next_session_id]
    active_sessions.append(next_session_id)
    set_app_setting(setting_key, serialize_active_platform_sessions(active_sessions))
    return next_session_id


def active_platform_session_matches(platform, role, username=None, device_id=None, session_id=None):
    supplied_session_id = str(session_id or "").strip()
    if not supplied_session_id:
        return False
    setting_key = active_session_setting_key(platform, role, username=username, device_id=device_id)
    if not setting_key:
        return False
    active_sessions = active_platform_sessions(platform, role, username=username, device_id=device_id)
    return any(secrets.compare_digest(active_session_id, supplied_session_id) for active_session_id in active_sessions)


def clear_active_platform_session(platform, role, username=None, device_id=None, session_id=None):
    setting_key = active_session_setting_key(platform, role, username=username, device_id=device_id)
    if not setting_key:
        return
    supplied_session_id = str(session_id or "").strip()
    if supplied_session_id:
        active_sessions = parse_active_platform_sessions(get_app_setting(setting_key, ""))
        if active_sessions and not any(secrets.compare_digest(active_session_id, supplied_session_id) for active_session_id in active_sessions):
            return
        remaining_sessions = [
            active_session_id
            for active_session_id in active_sessions
            if not secrets.compare_digest(active_session_id, supplied_session_id)
        ]
        if remaining_sessions:
            set_app_setting(setting_key, serialize_active_platform_sessions(remaining_sessions))
            return
    delete_app_setting(setting_key)


def clear_dashboard_identity(role=None):
    prefix = dashboard_identity_prefix(role or session.get("role") or "customer")
    for key in ("logged_in", "username", "role", "device_id", "auth_marker", "platform_session_id"):
        session.pop(key, None)
    for key in ("logged_in", "username", "device_id", "auth_marker", "platform_session_id"):
        session.pop(f"{prefix}_{key}", None)


def set_active_dashboard_identity(role, username, device_id=None, auth_marker=None, platform_session_id=None):
    session["logged_in"] = True
    session["username"] = username
    session["role"] = role
    session["device_id"] = device_id
    session["auth_marker"] = auth_marker
    session["platform_session_id"] = platform_session_id


def store_dashboard_identity(authenticated_user):
    role = authenticated_user["role"]
    prefix = dashboard_identity_prefix(role)
    platform_session_id = register_active_platform_session(
        SESSION_PLATFORM_DASHBOARD,
        role,
        username=authenticated_user["username"],
        device_id=authenticated_user.get("device_id"),
    )
    session[f"{prefix}_logged_in"] = True
    session[f"{prefix}_username"] = authenticated_user["username"]
    session[f"{prefix}_device_id"] = authenticated_user.get("device_id")
    session[f"{prefix}_auth_marker"] = authenticated_user.get("auth_marker")
    session[f"{prefix}_platform_session_id"] = platform_session_id
    set_active_dashboard_identity(
        role,
        authenticated_user["username"],
        authenticated_user.get("device_id"),
        authenticated_user.get("auth_marker"),
        platform_session_id,
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
        session.get(f"{prefix}_platform_session_id"),
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
    return {
        "csrf_token": get_csrf_token(),
        "cloud_poll_interval_ms": CLOUD_POLL_INTERVAL_SECONDS * 1000,
    }

def is_logged_in():
    if not bool(session.get("logged_in")):
        return False
    stored_auth_marker = str(session.get("auth_marker") or "").strip()
    expected_auth_marker = current_session_auth_marker()
    platform_session_id = str(session.get("platform_session_id") or "").strip()
    if (
        stored_auth_marker
        and expected_auth_marker
        and secrets.compare_digest(stored_auth_marker, expected_auth_marker)
        and active_platform_session_matches(
            SESSION_PLATFORM_DASHBOARD,
            session.get("role"),
            username=session.get("username"),
            device_id=session.get("device_id"),
            session_id=platform_session_id,
        )
    ):
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
    # The public homepage must never wait for database-backed session validation.
    # Protected destinations validate the identity when the user follows a link.
    if not bool(session.get("logged_in")):
        return None
    role = str(session.get("role") or "admin").strip().lower()
    if role == "admin":
        return {"role": "admin", "display_name": "Admin"}
    return {
        "role": "customer",
        "display_name": session.get("display_name") or session.get("username") or "Customer",
    }


def stored_dashboard_identity_is_valid(role):
    prefix = dashboard_identity_prefix(role)
    if not session.get(f"{prefix}_logged_in"):
        return False
    username = session.get(f"{prefix}_username")
    device_id = session.get(f"{prefix}_device_id")
    stored_auth_marker = str(session.get(f"{prefix}_auth_marker") or "").strip()
    expected_auth_marker = current_auth_marker_for_identity(role, username=username, device_id=device_id)
    platform_session_id = str(session.get(f"{prefix}_platform_session_id") or "").strip()
    return bool(
        stored_auth_marker
        and expected_auth_marker
        and secrets.compare_digest(stored_auth_marker, expected_auth_marker)
        and active_platform_session_matches(
            SESSION_PLATFORM_DASHBOARD,
            role,
            username=username,
            device_id=device_id,
            session_id=platform_session_id,
        )
    )


def homepage_auth_status():
    active_user = homepage_login_status()
    active_role = (active_user or {}).get("role")
    return {
        "active_user": active_user,
        "admin_logged_in": active_role == "admin" or bool(session.get("admin_logged_in")),
        "customer_logged_in": active_role == "customer" or bool(session.get("customer_logged_in")),
        "customer_display_name": (
            session.get("customer_display_name") or session.get("customer_username")
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


def render_login_page(
    mode="customer",
    error=None,
    next_url="/",
    sales_error=None,
    sales_success=None,
    sales_form=None,
    show_login_modal=None,
    homepage_visitor_count=None,
):
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
    if homepage_visitor_count is None:
        homepage_visitor_count = ensure_homepage_visitor_count_loaded()
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
        homepage_visitor_count=format_count_label(homepage_visitor_count),
        show_pricing_links=SHOW_PRICING_LINKS,
        on_dedicated_login_route=not is_landing_page,
        show_login_modal=(bool(error) or not is_landing_page) if show_login_modal is None else bool(show_login_modal),
    )


def validate_sales_enquiry_payload(form):
    valid_segments = {
        "Home / Villa",
        "Apartment / Hostel",
        "Hotel / Institution",
        "Dealer / Installer",
        "Commercial Site",
    }
    valid_source_configurations = {
        "Upper tank only",
        "Underground / source tank",
        "Borewell",
        "Municipal supply",
        "Multiple sources",
    }
    valid_pump_types = {
        "Not sure / site check needed",
        "Surface / monoblock pump",
        "Submersible pump",
        "No pump control needed",
    }
    valid_upper_layouts = {
        "Same level and interconnected",
        "Separate levels or not interconnected",
    }
    valid_maintenance_preferences = {
        "No maintenance contract",
        "Monthly maintenance quote",
        "Annual maintenance quote",
    }
    cleaned = {
        "name": str(form.get("name", "")).strip(),
        "phone": str(form.get("phone", "")).strip(),
        "email": str(form.get("email", "")).strip(),
        "city": str(form.get("city", "")).strip(),
        "segment": str(form.get("segment", "")).strip(),
        "device_count": str(form.get("device_count", "")).strip(),
        "upper_tank_count": str(form.get("upper_tank_count", "1")).strip() or "1",
        "source_tank_count": str(form.get("source_tank_count", "0")).strip() or "0",
        "upper_layout": str(form.get("upper_layout", "Same level and interconnected")).strip() or "Same level and interconnected",
        "source_configuration": str(form.get("source_configuration", "Upper tank only")).strip() or "Upper tank only",
        "pump_type": str(form.get("pump_type", "Not sure / site check needed")).strip() or "Not sure / site check needed",
        "maintenance_preference": str(form.get("maintenance_preference", "No maintenance contract")).strip() or "No maintenance contract",
        "message": str(form.get("message", "")).strip(),
    }
    errors = []

    if len(cleaned["name"]) < 2:
        errors.append("Please enter your name.")
    elif len(cleaned["name"]) > 80 or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", cleaned["name"]):
        errors.append("Name can use letters, spaces, dot, apostrophe or hyphen only.")

    if len(cleaned["email"]) > 120:
        errors.append("Email address must stay under 120 characters.")
    elif cleaned["email"]:
        try:
            cleaned["email"] = normalize_customer_email(cleaned["email"])
        except ValueError:
            errors.append("Please enter a valid email address.")

    phone_digits = re.sub(r"\D", "", cleaned["phone"])
    if not cleaned["phone"]:
        errors.append("Please enter a valid phone or WhatsApp number.")
    elif not re.fullmatch(r"\+?[0-9][0-9 ()-]*[0-9]", cleaned["phone"]) or len(phone_digits) < 10 or len(phone_digits) > 15:
        errors.append("Please enter a valid phone or WhatsApp number.")

    if len(cleaned["city"]) < 2:
        errors.append("Please enter your city or service area.")
    elif len(cleaned["city"]) > 80 or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", cleaned["city"]):
        errors.append("Please enter a valid city or service area.")

    if not cleaned["segment"]:
        errors.append("Please choose the project type.")
    elif cleaned["segment"] not in valid_segments:
        errors.append("Please choose a valid project type.")

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

    try:
        upper_tank_count = int(cleaned["upper_tank_count"])
    except (TypeError, ValueError):
        errors.append("Upper tank quantity must be a whole number.")
    else:
        if upper_tank_count < 1 or upper_tank_count > 1000:
            errors.append("Upper tank quantity must be between 1 and 1000.")
        else:
            cleaned["upper_tank_count"] = str(upper_tank_count)

    try:
        source_tank_count = int(cleaned["source_tank_count"])
    except (TypeError, ValueError):
        errors.append("Source tank quantity must be a whole number.")
    else:
        if source_tank_count < 0 or source_tank_count > 1000:
            errors.append("Source tank quantity must be between 0 and 1000.")
        else:
            cleaned["source_tank_count"] = str(source_tank_count)

    if cleaned["upper_layout"] not in valid_upper_layouts:
        errors.append("Please choose a valid overhead tank layout.")

    if cleaned["source_configuration"] not in valid_source_configurations:
        errors.append("Please choose a valid water source configuration.")
    if cleaned["pump_type"] not in valid_pump_types:
        errors.append("Please choose a valid pump type.")
    if cleaned["maintenance_preference"] not in valid_maintenance_preferences:
        errors.append("Please choose a valid maintenance preference.")

    if len(cleaned["message"]) > 800:
        errors.append("Project notes must stay under 800 characters.")

    if not errors:
        required_upper_mcus = 1 if cleaned["upper_layout"] == "Same level and interconnected" else int(cleaned["upper_tank_count"])
        configuration = (
            f"Water configuration: {cleaned['upper_tank_count']} upper/overhead tank(s), "
            f"{cleaned['source_tank_count']} source tank(s), {required_upper_mcus} required upper MCU(s); "
            f"layout: {cleaned['upper_layout']}; "
            f"source: {cleaned['source_configuration']}; pump: {cleaned['pump_type']}."
        )
        maintenance = f"Maintenance preference: {cleaned['maintenance_preference']}."
        visitor_note = cleaned["message"] or "Demo booking requested; project details will be confirmed during follow-up."
        cleaned["message"] = f"{configuration}\n{maintenance}\n{visitor_note}"

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


def append_sales_enquiry_backup(
    cleaned,
    lead_details,
    support_email_sent=False,
    confirmation_email_sent=False,
    whatsapp_team_sent=False,
    whatsapp_customer_sent=False,
):
    record = {
        "saved_at": datetime.now(timezone.utc).astimezone(IST_TIMEZONE).isoformat(),
        "support_email_sent": bool(support_email_sent),
        "confirmation_email_sent": bool(confirmation_email_sent),
        "whatsapp_team_sent": bool(whatsapp_team_sent),
        "whatsapp_customer_sent": bool(whatsapp_customer_sent),
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
        "SaleWell IoT Solutions\n"
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


def normalize_whatsapp_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return f"91{digits}"
    return digits


def whatsapp_template_text_values(payload):
    lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else {}
    if payload.get("recipient_type") == "team":
        return [
            lead.get("name") or "--",
            lead.get("phone") or payload.get("to") or "--",
            lead.get("city") or "--",
            lead.get("segment") or "--",
            lead.get("device_count") or "--",
            lead.get("selected_plan") or "--",
        ]
    return [
        lead.get("name") or "there",
        lead.get("selected_plan") or "SaleWell Smart Tank",
        "pricing, setup scope and installation planning",
    ]


def build_meta_whatsapp_template_payload(payload):
    template_name = (
        WHATSAPP_META_TEAM_TEMPLATE
        if payload.get("recipient_type") == "team"
        else WHATSAPP_META_CONFIRMATION_TEMPLATE
    )
    parameters = [
        {"type": "text", "text": str(value)[:1024]}
        for value in whatsapp_template_text_values(payload)
    ]
    return {
        "messaging_product": "whatsapp",
        "to": normalize_whatsapp_phone(payload.get("to")),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": WHATSAPP_META_TEMPLATE_LANGUAGE},
            "components": [
                {
                    "type": "body",
                    "parameters": parameters,
                }
            ],
        },
    }


def build_meta_whatsapp_text_payload(payload):
    return {
        "messaging_product": "whatsapp",
        "to": normalize_whatsapp_phone(payload.get("to")),
        "type": "text",
        "text": {
            "preview_url": False,
            "body": str(payload.get("message") or payload.get("title") or "")[:4096],
        },
    }


def send_meta_whatsapp_payload(payload):
    if WHATSAPP_PROVIDER != "meta":
        return False, "unsupported_provider", 400
    if not WHATSAPP_META_PHONE_NUMBER_ID or not WHATSAPP_META_ACCESS_TOKEN:
        return False, "missing_meta_credentials", 503
    recipient = normalize_whatsapp_phone(payload.get("to"))
    if not recipient:
        return False, "missing_recipient", 400

    use_text_message = payload.get("message_mode") == "text"
    meta_payload = build_meta_whatsapp_text_payload(payload) if use_text_message else build_meta_whatsapp_template_payload(payload)
    url = f"https://graph.facebook.com/{WHATSAPP_META_GRAPH_VERSION}/{WHATSAPP_META_PHONE_NUMBER_ID}/messages"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {WHATSAPP_META_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=meta_payload,
            timeout=(3, 12),
        )
    except requests.RequestException as exc:
        logger.warning("Meta WhatsApp send failed before response: %s", exc)
        return False, "meta_request_failed", 502

    if response.status_code >= 400:
        logger.warning("Meta WhatsApp send failed with HTTP %s: %s", response.status_code, response.text[:500])
        return False, "meta_rejected_message", response.status_code
    return True, "sent", response.status_code


def build_sales_enquiry_whatsapp_messages(cleaned, lead_details):
    selected_plan = selected_pricing_plan_from_notes(cleaned.get("message"))
    plan_line = f"\nPlan: {selected_plan}" if selected_plan else ""
    customer_phone = normalize_whatsapp_phone(cleaned.get("phone"))
    submitted_at = datetime.now(timezone.utc).astimezone(IST_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S %Z")
    team_message = (
        "New SaleWell Smart Tank demo/enquiry\n"
        f"Time: {submitted_at}\n"
        f"Name: {cleaned.get('name') or '--'}\n"
        f"Phone: {cleaned.get('phone') or '--'}\n"
        f"Email: {cleaned.get('email') or '--'}\n"
        f"City: {cleaned.get('city') or '--'}\n"
        f"Project: {cleaned.get('segment') or '--'}\n"
        f"Devices: {cleaned.get('device_count') or '--'}"
        f"{plan_line}\n"
        f"Notes: {cleaned.get('message') or '--'}"
    )
    customer_message = (
        f"Hi {cleaned.get('name') or 'there'}, thanks for booking a SaleWell Smart Tank demo/enquiry. "
        "We received your details and our team will contact you soon on this WhatsApp number for pricing, setup scope and installation planning."
    )
    return {
        "team": {
            "channel": "whatsapp",
            "kind": "sales_enquiry",
            "recipient_type": "team",
            "to": normalize_whatsapp_phone(WHATSAPP_TEAM_PHONE),
            "title": "New SaleWell Smart Tank demo/enquiry",
            "message": team_message,
            "lead": {
                "name": cleaned.get("name") or "",
                "phone": cleaned.get("phone") or "",
                "whatsapp_phone": customer_phone,
                "email": cleaned.get("email") or "",
                "city": cleaned.get("city") or "",
                "segment": cleaned.get("segment") or "",
                "device_count": cleaned.get("device_count") or "",
                "selected_plan": selected_plan,
            },
            "metadata": {
                "landing_mode": lead_details.get("landing_mode") or "",
                "remote_addr": lead_details.get("remote_addr") or "",
            },
        },
        "customer": {
            "channel": "whatsapp",
            "kind": "sales_enquiry_confirmation",
            "recipient_type": "customer",
            "to": customer_phone,
            "title": "SaleWell Smart Tank booking received",
            "message": customer_message,
            "lead": {
                "name": cleaned.get("name") or "",
                "phone": cleaned.get("phone") or "",
                "whatsapp_phone": customer_phone,
                "selected_plan": selected_plan,
            },
        },
    }


def post_whatsapp_webhook(payload, log_label):
    if not WHATSAPP_WEBHOOK_URL:
        return False
    try:
        headers = {}
        if WHATSAPP_WEBHOOK_SECRET:
            headers["X-SaleWell-Webhook-Secret"] = WHATSAPP_WEBHOOK_SECRET
        response = requests.post(WHATSAPP_WEBHOOK_URL, json=payload, headers=headers, timeout=(3, 8))
        if response.status_code >= 400:
            logger.warning("%s WhatsApp webhook returned HTTP %s.", log_label, response.status_code)
            return False
        return True
    except requests.RequestException as exc:
        logger.warning("%s WhatsApp webhook failed: %s", log_label, exc)
        return False


def send_sales_enquiry_whatsapp_messages(cleaned, lead_details):
    if not WHATSAPP_WEBHOOK_URL:
        return False, False
    payloads = build_sales_enquiry_whatsapp_messages(cleaned, lead_details)
    team_sent = post_whatsapp_webhook(payloads["team"], "Sales enquiry team")
    customer_sent = False
    if payloads["customer"].get("to"):
        customer_sent = post_whatsapp_webhook(payloads["customer"], "Sales enquiry customer confirmation")
    return team_sent, customer_sent


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
    return str(device.get("telemetry_status") or "").strip().lower() in {"live", "recent", "fresh", "online"}


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


def admin_municipal_sensor_status_fields(entry, service_config=None):
    service_config = service_config or {}
    online = admin_device_is_online(entry)
    enabled = boolish_enabled(
        entry.get("municipal_sensor_enabled"),
        default=service_config.get("municipal_sensor_enabled", False),
    )
    state = str(entry.get("municipal_sensor_state") or "").strip().lower()
    simulated = boolish_enabled(entry.get("municipal_sensor_simulated"), default=False)
    reachable = boolish_enabled(entry.get("municipal_sensor_reachable"), default=state in {"available", "unavailable"})
    flow_selected = boolish_enabled(
        entry.get("water_flow_sensor_enabled"),
        default=service_config.get("water_flow_sensor_enabled", False),
    )
    pressure_selected = boolish_enabled(
        entry.get("water_pressure_sensor_enabled"),
        default=service_config.get("water_pressure_sensor_enabled", False),
    )
    if not enabled:
        return "Disabled", "clear"
    if not online:
        return "Offline", "offline"
    if simulated:
        reachable = True
    elif flow_selected:
        reachable = boolish_enabled(entry.get("water_flow_detected"), default=False)
    elif pressure_selected:
        reachable = boolish_enabled(entry.get("water_pressure_detected"), default=False)
    if reachable:
        return "Available", "online"
    return "Waiting", "warning"


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

    relay_service = str(entry.get("relay_service") or entry.get("command_service") or "").strip().upper()
    relay_enabled = bool(service_config.get("relay_enabled", True)) and relay_service not in {"DISABLED", "OFF"}
    if bool_flag(entry.get("pump_failure")) or bool_flag(entry.get("dry_run")):
        relay_label, relay_tone = "No level rise", "warning"
    else:
        relay_label, relay_tone = admin_reachable_status_fields(relay_enabled, online and relay_enabled)

    slave_upper_enabled = bool(service_config.get("slave_upper_sensor_enabled")) and bool(service_config.get("slave_device_enabled", True))
    peer_packet_fresh = direct_peer_packet_is_fresh(entry)
    tank_level_is_valid = (
        boolish_enabled(entry.get("level_valid"), default=False)
        if entry.get("level_valid") is not None
        else safe_float(entry.get("level"), -1) >= 0
    )
    upper_sensor = entry.get("upper_sensor") or entry.get("main_sensor") or entry.get("sensor")
    upper_data_fresh = entry.get("upper_data_fresh")
    upper_simulated = boolish_enabled(entry.get("upper_tank_simulator"), default=False)
    upper_pulse_us = safe_float(entry.get("upper_sensor_pulse_us"), None)
    upper_has_live_input = admin_sensor_reachable(upper_sensor)
    if upper_data_fresh is not None:
        upper_has_live_input = upper_has_live_input and boolish_enabled(upper_data_fresh, default=False)
    # Simulator readings intentionally have a zero echo pulse. Once simulation
    # is OFF, a zero pulse is cached simulator data rather than physical sensor
    # evidence, so it must not keep the sensor online until the cache expires.
    if upper_pulse_us is not None and upper_pulse_us <= 0 and not upper_simulated:
        upper_has_live_input = False
    upper_enabled = bool(service_config.get("main_sensor_enabled", True))
    if str(upper_sensor or "").strip().upper() in {"DISABLED", "OFF"}:
        upper_enabled = False
    if slave_upper_enabled:
        upper_enabled = True
        # A fresh slave packet only proves that the slave node is reachable. It
        # does not prove that its upper-tank sensor is producing valid data.
        # Require the sensor health reported by firmware as well, otherwise a
        # cached level can incorrectly keep the dashboard badge online after
        # both the physical sensor and simulator become unavailable.
        upper_reachable = (
            online
            and peer_packet_fresh is True
            and tank_level_is_valid
            and upper_has_live_input
        )
    else:
        upper_reachable = online and upper_enabled and upper_has_live_input
    upper_label, upper_tone = admin_reachable_status_fields(
        upper_enabled,
        upper_reachable,
    )

    lower_sensor = entry.get("lower_sensor") or entry.get("source_sensor")
    lower_enabled = bool(service_config.get("source_tank_monitoring_enabled", True))
    lower_label, lower_tone = admin_reachable_status_fields(
        lower_enabled,
        online and lower_enabled and admin_sensor_reachable(lower_sensor),
    )
    municipal_label, municipal_tone = admin_municipal_sensor_status_fields(entry, service_config)

    return {
        "relay_status_label": relay_label,
        "relay_status_tone": relay_tone,
        "upper_sensor_status_label": upper_label,
        "upper_sensor_status_tone": upper_tone,
        "lower_sensor_status_label": lower_label,
        "lower_sensor_status_tone": lower_tone,
        "municipal_sensor_status_label": municipal_label,
        "municipal_sensor_status_tone": municipal_tone,
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
    query = f"""
        SELECT alert.device_id, alert.severity, alert.message, alert.updated_at, alert.id
        FROM ops_alerts AS alert
        WHERE alert.active = 1
          AND COALESCE(alert.device_id, '') != ''
          AND {latest_active_alert_filter("alert")}
    """
    params = []
    if updated_since:
        query += " AND alert.updated_at >= ?"
        params.append(updated_since)
    if normalized_device_ids:
        placeholders = ",".join("?" for _ in normalized_device_ids)
        query += f" AND alert.device_id IN ({placeholders})"
        params.extend(normalized_device_ids)
    query += " ORDER BY alert.updated_at DESC, alert.id DESC"
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
    sensor_distance_raw = payload.get("sensor_distance_cm")
    sensor_distance_value = safe_float(sensor_distance_raw, None)
    sensor_distance_label = (
        f"{sensor_distance_value:.1f} cm"
        if sensor_distance_value is not None and sensor_distance_value >= 0
        else "--"
    )
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
        "level_valid": payload.get("level_valid"),
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
        "direct_peer_remote_mac": payload.get("direct_peer_remote_mac"),
        "direct_peer_config_channel": payload.get("direct_peer_config_channel"),
        "direct_peer_wifi_channel": payload.get("direct_peer_wifi_channel"),
        "direct_peer_last_packet_age_s": payload.get("direct_peer_last_packet_age_s"),
        "direct_peer_last_packet_bytes": payload.get("direct_peer_last_packet_bytes"),
        "direct_peer_last_sequence": payload.get("direct_peer_last_sequence"),
        "direct_peer_duplicate_packets": payload.get("direct_peer_duplicate_packets"),
        "direct_peer_out_of_order_packets": payload.get("direct_peer_out_of_order_packets"),
        "direct_peer_estimated_lost_packets": payload.get("direct_peer_estimated_lost_packets"),
        "direct_peer_last_pong_age_s": payload.get("direct_peer_last_pong_age_s"),
        "direct_peer_last_pong_nonce": payload.get("direct_peer_last_pong_nonce"),
        "direct_peer_sync_pending": payload.get("direct_peer_sync_pending"),
        "direct_peer_sync_channel": payload.get("direct_peer_sync_channel"),
        "direct_peer_sync_last_ok_age_s": payload.get("direct_peer_sync_last_ok_age_s"),
        "last_ping_target": payload.get("last_ping_target"),
        "last_ping_status": payload.get("last_ping_status"),
        "last_ping_response_ms": payload.get("last_ping_response_ms"),
        "last_ping_age_s": payload.get("last_ping_age_s"),
        "last_ping_nonce": payload.get("last_ping_nonce"),
        "controller_state": payload.get("controller_state"),
        "upper_high_float_enabled": payload.get("upper_high_float_enabled"),
        "upper_high_float_active": payload.get("upper_high_float_active"),
        "source_low_float_enabled": payload.get("source_low_float_enabled"),
        "source_low_float_active": payload.get("source_low_float_active"),
        "wifi": payload.get("wifi"),
        "wifi_rssi": payload.get("wifi_rssi"),
        "sensor": payload.get("sensor"),
        "sensor_distance_cm": payload.get("sensor_distance_cm"),
        "sensor_distance_label": sensor_distance_label,
        "water_depth_cm": payload.get("water_depth_cm"),
        "water_depth_label": payload.get("water_depth_label"),
        "upper_sensor": payload.get("upper_sensor") or payload.get("main_sensor") or payload.get("sensor"),
        "upper_data_fresh": payload.get("upper_data_fresh"),
        "upper_tank_simulator": payload.get("upper_tank_simulator"),
        "upper_sensor_pulse_us": (
            payload.get("upper_sensor_pulse_us")
            if payload.get("upper_sensor_pulse_us") is not None
            else payload.get("sensor_pulse_us")
        ),
        "lower_sensor": payload.get("lower_sensor") or payload.get("source_sensor"),
        "municipal_sensor_enabled": payload.get("municipal_sensor_enabled"),
        "municipal_sensor_state": payload.get("municipal_sensor_state"),
        "municipal_sensor_simulated": payload.get("municipal_sensor_simulated"),
        "municipal_sensor_reachable": payload.get("municipal_sensor_reachable"),
        "municipal_sensor_last_updated": payload.get("municipal_sensor_last_updated"),
        "water_flow_sensor_enabled": payload.get("water_flow_sensor_enabled"),
        "water_flow_detected": payload.get("water_flow_detected"),
        "water_pressure_sensor_enabled": payload.get("water_pressure_sensor_enabled"),
        "water_pressure_detected": payload.get("water_pressure_detected"),
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
    critical_devices = sum(1 for device in available_devices if str(device.get("latest_alert_severity") or "").lower() == "danger")
    warning_devices = sum(1 for device in available_devices if str(device.get("latest_alert_severity") or "").lower() == "warning")
    maintenance_devices = sum(1 for device in available_devices if not bool(device.get("service_updates_enabled", True)))
    healthy_devices = max(0, total_registered_devices - critical_devices - warning_devices - maintenance_devices)
    average_health = round(
        sum(safe_float(device.get("admin_health_score"), 0) for device in available_devices) / total_registered_devices
    ) if total_registered_devices else 0

    return {
        "total_registered_devices": total_registered_devices,
        "online_devices": online_devices,
        "offline_devices": offline_devices,
        "warning_alert_devices": warning_alert_devices,
        "healthy_devices": healthy_devices,
        "critical_devices": critical_devices,
        "warning_devices": warning_devices,
        "maintenance_devices": maintenance_devices,
        "online_percent": round((online_devices / total_registered_devices) * 100) if total_registered_devices else 0,
        "offline_percent": round((offline_devices / total_registered_devices) * 100) if total_registered_devices else 0,
        "average_health": average_health,
    }


def admin_device_health_fields(entry):
    online = admin_device_is_online(entry)
    telemetry = str(entry.get("telemetry_status") or "no-data").strip().lower()
    score = 100.0
    if not online:
        score -= 35
    if telemetry in {"stale", "offline", "no-data"}:
        score -= 20
    rssi = safe_float(entry.get("wifi_rssi"), None)
    if rssi is None:
        score -= 5
    elif rssi <= -80:
        score -= 18
    elif rssi <= -70:
        score -= 10
    for sensor_key in ("upper_sensor_status_tone", "lower_sensor_status_tone"):
        if str(entry.get(sensor_key) or "").lower() in {"danger", "bad", "offline"}:
            score -= 10
    severity = str(entry.get("latest_alert_severity") or "").lower()
    if severity == "danger":
        score -= 20
    elif severity == "warning":
        score -= 10
    score = int(round(max(0, min(100, score))))
    tone = "online" if score >= 85 else "warning" if score >= 60 else "danger"
    age_seconds = safe_float(entry.get("seconds_since_sync"), None)
    if age_seconds is None and entry.get("last_sync_at"):
        parsed = parse_timestamp(entry.get("last_sync_at"))
        age_seconds = max(0, (now_utc() - parsed).total_seconds()) if parsed else None
    return {
        "admin_health_score": score,
        "admin_health_tone": tone,
        "last_seen_age_seconds": int(age_seconds) if age_seconds is not None else None,
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
    service_configs = list_device_service_configs(
        merged.keys(),
        accounts_by_device=accounts_by_device,
        snapshots_by_device=merged,
    )
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
        entry.update(admin_device_health_fields(entry))
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
                item.get("cloud_device_id"),
                item.get("firmware_version"),
                item.get("swt_version"),
            )
        )
        if normalized_query in haystack:
            filtered.append(item)
    return filtered


def load_admin_known_devices(accounts, inventory_limit=100):
    return build_admin_known_devices(
        accounts=accounts,
        # The admin registry includes up to 200 persisted devices. Load at
        # least that many live snapshots so registered devices with fresh
        # telemetry are not rendered offline merely because of pagination.
        available_devices=fetch_device_inventory(limit=max(250, int(inventory_limit))),
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
        # Admin registration is the authoritative per-device credential. Let
        # it recover a device even when an exact or wildcard environment rule
        # is stale after a deliberate key rotation.
        registered_rule = registered_device_auth_rule_matches(normalized_device_id, device_key)
        if registered_rule:
            matched_rule = registered_rule
        else:
            logger.warning("Rejected device auth for %s", normalized_device_id)
            return False, None, "invalid device credentials", 403

    remember_registered_device(
        normalized_device_id,
        registration_source=f"device_keys_{matched_rule['kind']}",
        key_rule=matched_rule.get("pattern"),
        best_effort=True,
    )
    remember_authenticated_device_key(normalized_device_id, device_key)
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

    def relay_state_label(value):
        if isinstance(value, bool):
            return "ON" if value else "OFF"
        normalized = str(value or "").strip().upper()
        if normalized in {"1", "TRUE", "YES", "ON", "RUNNING", "ACTIVE"}:
            return "ON"
        if normalized in {"0", "FALSE", "NO", "OFF", "STOPPED", "INACTIVE"}:
            return "OFF"
        return None

    if not cleaned.get("device_local_url") and cleaned.get("device_ip_url"):
        cleaned["device_local_url"] = cleaned.get("device_ip_url")
    if cleaned.get("level") is None and cleaned.get("main_tank_level") is not None:
        cleaned["level"] = cleaned.get("main_tank_level")
    # Firmware's logical pump state is authoritative. In simulator builds the
    # logical pump can be ON while the physical relay output intentionally
    # remains OFF, so relay_on must only be a legacy fallback.
    pump_state = relay_state_label(cleaned.get("pump"))
    if pump_state is None:
        pump_state = relay_state_label(cleaned.get("motor"))
    if pump_state is None:
        pump_state = relay_state_label(cleaned.get("swt_relay"))
    if pump_state is None:
        pump_state = relay_state_label(cleaned.get("relay_on"))
    if pump_state is None:
        pump_state = relay_state_label(cleaned.get("relay"))
    if pump_state is not None:
        cleaned["motor"] = pump_state
    if cleaned.get("sensor") is None and cleaned.get("upper_sensor") is not None:
        cleaned["sensor"] = cleaned.get("upper_sensor")
    return cleaned


SOURCE_TANK_ALIAS_FIELDS = {
    "lower_tank_level": ("source_tank_level", "source_level"),
    "lower_sensor": ("source_tank_sensor", "source_sensor"),
    "lower_sensor_info": ("source_tank_sensor_info", "source_sensor_info"),
    "lower_sensor_distance_cm": ("source_tank_sensor_distance_cm", "source_sensor_distance_cm"),
    "lower_tank_service": ("source_tank_service", "source_service"),
    "lower_tank_height_cm": ("source_tank_height_cm", "source_height_cm"),
    "lower_tank_capacity_liters": ("source_tank_capacity_liters", "source_capacity_liters"),
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


def prune_delete_batches(cursor, delete_sql, params=(), batch_rows=None, max_batches=None):
    batch_limit = max(1, int(batch_rows or DB_RETENTION_DELETE_BATCH_ROWS))
    batch_count = max(1, int(max_batches or DB_RETENTION_DELETE_MAX_BATCHES))
    total_deleted = 0
    base_sql = str(delete_sql).strip().rstrip(";")
    for _ in range(batch_count):
        cursor.execute(
            f"{base_sql}\nLIMIT ?",
            tuple(params or ()) + (batch_limit,),
        )
        deleted = max(0, int(cursor.rowcount or 0))
        total_deleted += deleted
        if deleted < batch_limit:
            break
    return total_deleted


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
            ) AS old_rows
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


def prune_tank_data_retention(cursor, max_batches=None):
    return prune_delete_batches(
        cursor,
        """
        DELETE FROM tank_data
        WHERE created_at < datetime('now', ?)
        """,
        (f"-{DATA_RETENTION_DAYS} day",),
        max_batches=max_batches,
    )


def prune_device_events_retention(cursor, max_batches=None):
    return prune_delete_batches(
        cursor,
        """
        DELETE FROM device_events
        WHERE event_at < datetime('now', ?)
        """,
        (f"-{DEVICE_EVENT_RETENTION_DAYS} day",),
        max_batches=max_batches,
    )


def prune_retained_rows(cursor, device_id=None, latest_row_id=None, force=False):
    pruned = {}
    max_batches = DB_RETENTION_DELETE_FORCE_MAX_BATCHES if force else DB_RETENTION_DELETE_MAX_BATCHES

    pruned["tank_data_retention"] = prune_tank_data_retention(cursor, max_batches=max_batches)
    pruned["device_events_retention"] = prune_device_events_retention(cursor, max_batches=max_batches)

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
        # Avoid DELETE ... IN (SELECT ... LIMIT/OFFSET), which is rejected by
        # some MariaDB releases used by shared hosts. Select a bounded batch of
        # old IDs first, then delete those exact rows with a portable query.
        cursor.execute(
            """
            SELECT id
            FROM tank_data
            WHERE COALESCE(device_id, '') = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (
                normalized_device_id,
                DB_RETENTION_DELETE_BATCH_ROWS,
                MAX_TELEMETRY_ROWS_PER_DEVICE,
            ),
        )
        capped_row_ids = [int(row["id"]) for row in cursor.fetchall()]
        if capped_row_ids:
            placeholders = ", ".join("?" for _ in capped_row_ids)
            cursor.execute(
                f"DELETE FROM tank_data WHERE id IN ({placeholders})",
                tuple(capped_row_ids),
            )
        pruned["tank_data_device_cap"] = max(0, int(cursor.rowcount or 0)) if capped_row_ids else 0

    pruned["device_command_queue_retention"] = prune_delete_batches(
        cursor,
        """
        DELETE FROM device_command_queue
        WHERE delivered_at IS NOT NULL
          AND delivered_at < datetime('now', ?)
        """,
        (f"-{DEVICE_COMMAND_RETENTION_DAYS} day",),
        max_batches=max_batches,
    )

    pruned["ops_audit_log_retention"] = prune_delete_batches(
        cursor,
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
        max_batches=max_batches,
    )

    pruned["ops_alerts_retention"] = prune_delete_batches(
        cursor,
        """
        DELETE FROM ops_alerts
        WHERE active = 0
          AND COALESCE(resolved_at, updated_at, created_at) < datetime('now', ?)
        """,
        (f"-{OPS_ALERT_RETENTION_DAYS} day",),
        max_batches=max_batches,
    )
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

        def prune_operation():
            with get_db() as db:
                cursor = db.cursor()
                return prune_retained_rows(
                    cursor,
                    device_id=device_id,
                    latest_row_id=latest_row_id,
                    force=force,
                )

        pruned = run_with_database_lock_retries(
            prune_operation,
            operation_name="prune retained rows",
            attempts=3,
            initial_delay_s=0.5,
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
        maybe_maintain_database(reason="telemetry-retention", pruned_rows=pruned_rows, force=force)
        return pruned
    except Exception as exc:
        # A failed prune must still observe the normal cooldown. Without this,
        # every telemetry request retries the same bad query and can exhaust
        # web workers through an exception/logging storm.
        db_prune_state.update(
            {
                "last_run_at": time.time(),
                "last_error_at": time.time(),
                "last_error": str(exc),
            }
        )
        logger.warning("Retention prune failed: %s", exc)
        return {}
    finally:
        db_prune_lock.release()


def maybe_maintain_database(reason="periodic", pruned_rows=0, force=False):
    now = time.time()
    if not DB_MAINTENANCE_ENABLED:
        db_maintenance_state.update(
            {
                "last_skip_at": now,
                "last_skip_reason": "maintenance-disabled",
            }
        )
        return False

    if USING_MYSQL and not MYSQL_OPTIMIZE_ENABLED:
        db_maintenance_state.update(
            {
                "last_skip_at": now,
                "last_skip_reason": "mysql-optimize-disabled",
            }
        )
        return False

    if (
        not force
        and DB_MAINTENANCE_MIN_INTERVAL_SECONDS > 0
        and (now - float(db_maintenance_state.get("last_run_at") or 0.0)) < DB_MAINTENANCE_MIN_INTERVAL_SECONDS
    ):
        db_maintenance_state.update(
            {
                "last_skip_at": now,
                "last_skip_reason": "maintenance-interval",
            }
        )
        return False

    if not force and int(pruned_rows or 0) <= 0:
        db_maintenance_state.update(
            {
                "last_skip_at": now,
                "last_skip_reason": "maintenance-no-pruned-rows",
            }
        )
        return False

    if (
        not force
        and DB_OPTIMIZE_AFTER_PRUNE_ROWS > 0
        and int(pruned_rows or 0) < DB_OPTIMIZE_AFTER_PRUNE_ROWS
    ):
        db_maintenance_state.update(
            {
                "last_skip_at": now,
                "last_skip_reason": "maintenance-pruned-rows-under-threshold",
            }
        )
        return False

    if not db_maintenance_lock.acquire(blocking=False):
        return False

    optimized_tables = []
    try:
        with get_db() as db:
            cursor = db.cursor()
            if USING_MYSQL:
                for table_name in (
                    "tank_data",
                    "device_events",
                    "device_command_queue",
                    "ops_audit_log",
                    "ops_alerts",
                ):
                    cursor.execute(f"OPTIMIZE TABLE {quote_mysql_identifier(table_name)}")
                    optimized_tables.append(table_name)
            else:
                cursor.execute("PRAGMA optimize")
                optimized_tables.append("sqlite")

        file_sizes = collect_database_file_sizes()
        db_maintenance_state.update(
            {
                "last_run_at": now,
                "last_reason": reason,
                "last_error": None,
                "last_total_bytes": int(file_sizes.get("total_bytes") or 0),
                "last_action": "optimize",
                "last_pruned_rows": int(pruned_rows or 0),
                "last_tables": optimized_tables,
                "last_skip_reason": None,
            }
        )
        return True
    except Exception as exc:
        db_maintenance_state.update(
            {
                "last_run_at": now,
                "last_reason": reason,
                "last_error": str(exc),
                "last_action": "optimize-failed",
                "last_pruned_rows": int(pruned_rows or 0),
                "last_tables": optimized_tables,
            }
        )
        logger.warning("Database maintenance failed: %s", exc)
        return False
    finally:
        db_maintenance_lock.release()


def postprocess_telemetry_payload(cleaned, raw_firmware_logs=None, source_ip=None, transport="http", latest_row_id=None):
    try:
        # Retention performs multi-row DELETE operations and must never run in
        # a telemetry/page worker. On shared MariaDB it can hold locks until the
        # web-server timeout and starve every LSAPI child. Cleanup remains
        # available through the explicit admin/cron endpoint.
        try:
            saved_telemetry_config = fetch_device_service_config(cleaned.get("device_id"), snapshot=None)
            # Debug: record reported capacity and simulator flags to help diagnose
            # cases where firmware reports config changes but Flask does not reflect them.
            try:
                logger.debug(
                    "Telemetry postprocess capacities for %s: tank=%s upper=%s source=%s upper_sim=%s lower_sim=%s simulator=%s",
                    cleaned.get("device_id") or "unknown",
                    cleaned.get("tank_capacity_liters"),
                    cleaned.get("upper_tank_capacity_liters"),
                    cleaned.get("source_tank_capacity_liters") or cleaned.get("lower_tank_capacity_liters"),
                    cleaned.get("upper_tank_simulator"),
                    cleaned.get("lower_tank_simulator") or cleaned.get("source_tank_simulator"),
                    cleaned.get("simulator"),
                )
            except Exception:
                pass
            reported_peer_channel = (
                cleaned.get("direct_peer_config_channel")
                if cleaned.get("direct_peer_config_channel") not in (None, "")
                else cleaned.get("direct_peer_wifi_channel")
            )
            upsert_device_service_config(
                cleaned.get("device_id"),
                tank_height_cm=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "tank_height_cm",
                    cleaned.get("tank_height_cm"),
                ),
                tank_capacity_liters=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "tank_capacity_liters",
                    cleaned.get("tank_capacity_liters"),
                ),
                upper_tank_height_cm=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "upper_tank_height_cm",
                    cleaned.get("tank_height_cm"),
                ),
                upper_tank_capacity_liters=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "upper_tank_capacity_liters",
                    cleaned.get("tank_capacity_liters"),
                ),
                lower_tank_height_cm=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "lower_tank_height_cm",
                    cleaned.get("lower_tank_height_cm"),
                ),
                lower_tank_capacity_liters=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "lower_tank_capacity_liters",
                    cleaned.get("source_tank_capacity_liters") or cleaned.get("lower_tank_capacity_liters"),
                ),
                auto_start_pct=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "auto_start_pct",
                    cleaned.get("auto_start_pct"),
                ),
                auto_stop_pct=telemetry_config_float_seed(
                    saved_telemetry_config,
                    "auto_stop_pct",
                    cleaned.get("auto_stop_pct"),
                ),
                telemetry_service_state=cleaned.get("telemetry_service"),
                command_service_state=cleaned.get("command_service"),
                relay_service_state=cleaned.get("relay_service"),
                ota_service_state=cleaned.get("ota_service"),
                local_firmware_upload_service_state=cleaned.get("local_firmware_upload_service"),
                buzzer_service_state=cleaned.get("buzzer_service"),
                led_display_service_state=cleaned.get("led_display_service"),
                lower_tank_service_state=cleaned.get("lower_tank_service"),
                slave_device_service_state=cleaned.get("slave_device_service"),
                direct_peer_wifi_channel=telemetry_config_peer_channel_seed(
                    saved_telemetry_config,
                    reported_peer_channel,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to upsert persisted device configuration for %s: %s",
                cleaned.get("device_id") or "unknown device",
                exc,
            )
        clear_runtime_caches(cleaned.get("device_id"))
        record_device_simulator_state(
            cleaned.get("device_id"),
            simulator_payload_enabled(cleaned),
            source=transport,
        )
        try:
            firmware_log_payload = dict(cleaned)
            if isinstance(raw_firmware_logs, list):
                firmware_log_payload["firmware_logs"] = raw_firmware_logs
            firmware_log_events = build_firmware_log_events_from_payload(
                firmware_log_payload,
                limit=24,
                device_id=cleaned.get("device_id"),
            )
            if firmware_log_events:
                persist_device_events(firmware_log_events, default_device_id=cleaned.get("device_id"))
        except Exception as exc:
            logger.debug("Firmware log event sync issue for %s: %s", cleaned.get("device_id") or "unknown device", exc)
        try:
            sync_device_events(device_id=cleaned.get("device_id"))
        except Exception as exc:
            logger.debug("Device event sync issue for %s: %s", cleaned.get("device_id") or "unknown device", exc)

        if cleaned.get("device_source") == get_device_source_mode():
            alert_snapshot = dict(cleaned)
            alert_snapshot["telemetry_status"] = "fresh"
            evaluate_snapshot_alerts(alert_snapshot)
            # Coalesce telemetry bursts and materialize the dashboard outside
            # the ingestion path, never while a customer opens the page.
            schedule_dashboard_summary_refresh(cleaned.get("device_id"))
            if cleaned.get("device_source") == DEVICE_SOURCE_REAL:
                relay_status_async(cleaned)
    except Exception as exc:
        logger.warning(
            "Telemetry postprocess failed for %s via %s: %s",
            cleaned.get("device_id") or "unknown device",
            transport,
            exc,
        )


def schedule_telemetry_postprocess(cleaned, raw_firmware_logs=None, source_ip=None, transport="http", latest_row_id=None):
    normalized_device_id = normalize_device_id((cleaned or {}).get("device_id"))
    if not normalized_device_id:
        return
    work_item = (dict(cleaned), raw_firmware_logs, source_ip, transport, latest_row_id)
    with telemetry_postprocess_lock:
        # Keep only the newest payload for a device. A five-second telemetry
        # interval must not create an unbounded number of database threads.
        telemetry_postprocess_pending[normalized_device_id] = work_item
        if normalized_device_id in telemetry_postprocess_running:
            return
        telemetry_postprocess_running.add(normalized_device_id)

    def worker():
        try:
            while True:
                with telemetry_postprocess_lock:
                    pending = telemetry_postprocess_pending.pop(
                        normalized_device_id,
                        None,
                    )

                    last_run = telemetry_postprocess_last_run_at.get(
                        normalized_device_id,
                        0.0,
                    )

                    if pending is None:
                        return

                remaining = 30.0 - (
                    time.monotonic() - last_run
                )

                if remaining > 0:
                    # Debouncing does not consume the scarce database-work
                    # permit. Otherwise one quiet device can block all other
                    # devices for the full 30-second coalescing interval.
                    time.sleep(remaining)

                    with telemetry_postprocess_lock:
                        pending = telemetry_postprocess_pending.pop(
                            normalized_device_id,
                            pending,
                        )

                acquired = telemetry_background_semaphore.acquire(blocking=False)
                if not acquired:
                    with telemetry_postprocess_lock:
                        telemetry_postprocess_pending[normalized_device_id] = pending
                    # Keep the coalesced newest payload queued and let this
                    # device worker retry. Returning here made processing rely
                    # on another telemetry request to create a replacement
                    # thread, which caused stale summaries during busy bursts.
                    logger.debug(
                        "Telemetry postprocess queued for %s while the "
                        "background worker is busy.",
                        normalized_device_id,
                    )
                    time.sleep(0.5)
                    continue

                try:
                    # Passenger runs several independent Python processes, so
                    # the in-memory per-device set above cannot prevent the
                    # same device being processed concurrently in two workers.
                    # Use a zero-wait filesystem lease instead of GET_LOCK:
                    # holding a MySQL connection for this whole job can exhaust
                    # shared-host connection limits and trigger error 2006.
                    lease_file = None
                    lease_acquired = True
                    if fcntl is not None:
                        lease_digest = hashlib.sha256(normalized_device_id.encode("utf-8")).hexdigest()[:32]
                        lease_path = Path(tempfile.gettempdir()) / f"swt-telemetry-{lease_digest}.lock"
                        lease_file = lease_path.open("a+")
                        try:
                            fcntl.flock(lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            lease_acquired = False
                    try:
                        if lease_acquired:
                            postprocess_telemetry_payload(*pending)
                    finally:
                        if lease_file is not None:
                            if lease_acquired:
                                fcntl.flock(lease_file.fileno(), fcntl.LOCK_UN)
                            lease_file.close()
                finally:
                    telemetry_background_semaphore.release()

                with telemetry_postprocess_lock:
                    telemetry_postprocess_last_run_at[
                        normalized_device_id
                    ] = time.monotonic()

                    if normalized_device_id not in telemetry_postprocess_pending:
                        return
        finally:
            with telemetry_postprocess_lock:
                telemetry_postprocess_running.discard(
                    normalized_device_id
                )

    threading.Thread(
        target=worker,
        name=f"telemetry-postprocess-{normalized_device_id}",
        daemon=True,
    ).start()


def process_telemetry_payload(data, source_ip=None, transport="http", defer_postprocess=False):
    raw_payload = dict(data or {})
    raw_firmware_logs = raw_payload.get("firmware_logs")
    cleaned = sanitize_payload(raw_payload)
    cleaned.pop("device_key", None)
    cleaned.pop("firmware_logs", None)
    apply_device_status_aliases(cleaned)
    apply_source_tank_aliases(cleaned)
    normalized_device_id = normalize_device_id(cleaned.get("device_id"))
    if normalized_device_id and device_is_ignored(normalized_device_id):
        cleaned["device_id"] = normalized_device_id
        cleaned["_telemetry_sync_result"] = "ignored"
        logger.info("Ignored telemetry for deleted device %s via %s", normalized_device_id, transport)
        return cleaned
    cleaned["device_source"] = normalize_device_source(cleaned.get("device_source"), default=DEVICE_SOURCE_REAL)
    telemetry_fingerprint = build_telemetry_sync_fingerprint(cleaned)
    cleaned["telemetry_fingerprint"] = telemetry_fingerprint

    if telemetry_sync_is_recent_duplicate(
        cleaned.get("device_id"),
        telemetry_fingerprint,
        transport=transport,
        source_ip=source_ip,
    ):
        cleaned["_telemetry_sync_result"] = "duplicate"
        logger.info(
            "Skipped duplicate telemetry sync via %s for device %s fingerprint=%s",
            transport,
            cleaned.get("device_id") or "unknown device",
            telemetry_fingerprint[:12],
        )
        return cleaned

    mode = str(cleaned.get("mode", "AUTO")).upper()
    if mode not in {"AUTO", "MANUAL"}:
        mode = "AUTO"
    municipal_enabled = boolish_enabled(cleaned.get("municipal_sensor_enabled"), default=False)
    cleaned["municipal_sensor_enabled"] = municipal_enabled
    if not municipal_enabled:
        cleaned["municipal_sensor_state"] = cleaned.get("municipal_sensor_state") or "unknown"
        cleaned["municipal_sensor_simulated"] = False
        cleaned["municipal_sensor_reachable"] = False
        cleaned["municipal_sensor_last_updated"] = cleaned.get("municipal_sensor_last_updated")
    else:
        cleaned["municipal_sensor_state"] = str(cleaned.get("municipal_sensor_state") or "unknown").strip().lower()
        cleaned["municipal_sensor_simulated"] = boolish_enabled(cleaned.get("municipal_sensor_simulated"), default=False)
        cleaned["municipal_sensor_reachable"] = boolish_enabled(
            cleaned.get("municipal_sensor_reachable"),
            default=cleaned["municipal_sensor_state"] in {"available", "unavailable"},
        )

    latest_row_id = None
    received_at = now_utc().strftime(TIMESTAMP_FORMAT)
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
        cleaned.get("auto_start_pct"),
        cleaned.get("auto_stop_pct"),
        cleaned.get("auto_start_stable_ms"),
        cleaned.get("auto_level_average_samples"),
        cleaned.get("auto_status"),
        cleaned.get("auto_status_tone"),
        cleaned.get("auto_timer"),
        cleaned.get("tank_health"),
        cleaned.get("free_heap"),
        cleaned.get("cpu_utilization_pct"),
        cleaned.get("slave_free_heap"),
        cleaned.get("slave_cpu_utilization_pct"),
        cleaned.get("slave_uptime_s"),
        cleaned.get("uptime_s"),
        cleaned.get("lower_tank_level"),
        cleaned.get("lower_sensor"),
        cleaned.get("lower_sensor_info"),
        cleaned.get("lower_sensor_distance_cm"),
        1 if cleaned.get("municipal_sensor_enabled") else 0,
        cleaned.get("municipal_sensor_state"),
        1 if cleaned.get("municipal_sensor_simulated") else 0,
        1 if cleaned.get("municipal_sensor_reachable") else 0,
        cleaned.get("municipal_sensor_last_updated"),
        1 if boolish_enabled(cleaned.get("municipal_valve_simulated"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("municipal_valve_feature_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("source_outlet_valve_simulated"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("source_pump_fill_feature_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("lower_turbidity_simulated"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("upper_turbidity_simulated"), default=False) else 0,
        cleaned.get("device_id"),
        cleaned.get("firmware_version"),
        cleaned.get("slave_firmware_version"),
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
        cleaned.get("direct_peer_remote_mac"),
        cleaned.get("direct_peer_config_channel"),
        cleaned.get("direct_peer_wifi_channel"),
        cleaned.get("direct_peer_last_packet_age_s"),
        cleaned.get("direct_peer_last_packet_bytes"),
        cleaned.get("direct_peer_last_sequence"),
        cleaned.get("direct_peer_duplicate_packets"),
        cleaned.get("direct_peer_out_of_order_packets"),
        cleaned.get("direct_peer_estimated_lost_packets"),
        cleaned.get("direct_peer_last_pong_age_s"),
        cleaned.get("direct_peer_last_pong_nonce"),
        cleaned.get("direct_peer_sync_pending"),
        cleaned.get("direct_peer_sync_channel"),
        cleaned.get("direct_peer_sync_last_ok_age_s"),
        cleaned.get("last_ping_target"),
        cleaned.get("last_ping_status"),
        cleaned.get("last_ping_response_ms"),
        cleaned.get("last_ping_age_s"),
        cleaned.get("last_ping_nonce"),
        cleaned.get("telemetry_fingerprint"),
        cleaned.get("controller_state"),
        1 if boolish_enabled(cleaned.get("upper_high_float_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("upper_high_float_active"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("source_low_float_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("source_low_float_active"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("starter_contactor_sensor_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("starter_contactor_active"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("motor_current_sensor_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("motor_current_detected"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("water_flow_sensor_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("water_flow_detected"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("water_pressure_sensor_enabled"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("water_pressure_detected"), default=False) else 0,
        1 if boolish_enabled(cleaned.get("physical_pump_running"), default=False) else 0,
        cleaned.get("pump_confirmation_source"), cleaned.get("pump_total_runtime_s"),
        cleaned.get("pump_last_run_runtime_s"), cleaned.get("pump_cycle_count"),
        cleaned.get("pump_run_started_uptime_s"), cleaned.get("pump_runtime_boot_id"),
        received_at,
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
                auto_start_pct, auto_stop_pct,
                auto_start_stable_ms, auto_level_average_samples,
                auto_status, auto_status_tone, auto_timer,
                tank_health, free_heap, cpu_utilization_pct,
                slave_free_heap, slave_cpu_utilization_pct, slave_uptime_s,
                uptime_s,
                lower_tank_level, lower_sensor, lower_sensor_info, lower_sensor_distance_cm,
                municipal_sensor_enabled, municipal_sensor_state, municipal_sensor_simulated,
                municipal_sensor_reachable, municipal_sensor_last_updated,
                municipal_valve_simulated, municipal_valve_feature_enabled,
                source_outlet_valve_simulated, source_pump_fill_feature_enabled,
                lower_turbidity_simulated, upper_turbidity_simulated,
                device_id, firmware_version, slave_firmware_version, reset_reason, source_ip, device_local_url,
                channel_mode, telemetry_service, command_service, ota_service, lower_tank_service,
                buzzer_service, led_display_service, local_firmware_upload_service,
                arch_id, node_role, device_type,
                direct_peer, direct_peer_remote_ip, direct_peer_remote_mac,
                direct_peer_config_channel, direct_peer_wifi_channel,
                direct_peer_last_packet_age_s, direct_peer_last_packet_bytes,
                direct_peer_last_sequence, direct_peer_duplicate_packets,
                direct_peer_out_of_order_packets, direct_peer_estimated_lost_packets,
                direct_peer_last_pong_age_s, direct_peer_last_pong_nonce,
                direct_peer_sync_pending, direct_peer_sync_channel,
                direct_peer_sync_last_ok_age_s,
                last_ping_target, last_ping_status, last_ping_response_ms,
                last_ping_age_s, last_ping_nonce, telemetry_fingerprint,
                controller_state, upper_high_float_enabled, upper_high_float_active,
                source_low_float_enabled, source_low_float_active,
                starter_contactor_sensor_enabled, starter_contactor_active,
                motor_current_sensor_enabled, motor_current_detected,
                water_flow_sensor_enabled, water_flow_detected,
                water_pressure_sensor_enabled, water_pressure_detected,
                physical_pump_running, pump_confirmation_source,
                pump_total_runtime_s, pump_last_run_runtime_s, pump_cycle_count,
                pump_run_started_uptime_s, pump_runtime_boot_id,
                created_at
            )
            VALUES ({placeholders})
            """,
            insert_values,
        )
        latest_row_id = cursor.lastrowid
        if CAPACITY_FEATURES.enabled("latest_state_writes"):
            cleaned["_latest_state_write_result"] = upsert_device_latest_state(
                cursor,
                cleaned,
                received_at,
            )
        if CAPACITY_FEATURES.enabled("narrow_history_writes"):
            cleaned["_narrow_history_write_result"] = store_narrow_history_if_due(
                cursor,
                cleaned,
                received_at,
                adaptive=CAPACITY_FEATURES.enabled("adaptive_history"),
                idle_interval_seconds=max(1, env_int("CAPACITY_HISTORY_IDLE_SECONDS", 300)),
                active_interval_seconds=max(1, env_int("CAPACITY_HISTORY_ACTIVE_SECONDS", 60)),
                level_delta_pct=max(0.0, env_float("CAPACITY_HISTORY_LEVEL_DELTA_PCT", 2.0)),
            )

    clear_runtime_caches(cleaned.get("device_id"))
    log_device_connection_status(
        cleaned.get("device_id"),
        True,
        telemetry_status="live",
        seconds_since_sync=0,
    )
    # Telemetry is intentionally high-frequency (often every few seconds per
    # device).  Logging every successful sample at INFO makes Passenger's
    # stderr log grow without bound on cPanel and can exhaust the account's
    # disk/I/O quota.  Operators can still opt into these records with DEBUG;
    # failures and state-changing events remain visible at higher levels.
    logger.debug(
        "Saved tank level via %s: %s | Motor: %s | Mode: %s | Device: %s",
        transport,
        cleaned.get("level"),
        cleaned.get("motor"),
        mode,
        cleaned.get("device_id"),
    )

    if defer_postprocess:
        schedule_telemetry_postprocess(cleaned, raw_firmware_logs, source_ip, transport, latest_row_id)
    else:
        postprocess_telemetry_payload(cleaned, raw_firmware_logs, source_ip, transport, latest_row_id)
    cleaned["_telemetry_sync_result"] = "saved"
    return cleaned


def ingest_device_sync(
    data,
    *,
    authenticated_device_id=None,
    source_ip=None,
    transport="http",
    defer_postprocess=False,
):
    return ingest_device_payload(
        data,
        processor=process_telemetry_payload,
        authenticated_device_id=authenticated_device_id,
        source_ip=source_ip,
        transport=transport,
        defer_postprocess=defer_postprocess,
    )


def mysql_connection_config():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    configured_database = resolve_mysql_database_name(os.environ.get("MYSQL_DATABASE", ""))
    if database_url:
        parsed = urlparse(database_url)
        return {
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
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


def mysql_auto_create_database_enabled():
    return env_flag("MYSQL_AUTO_CREATE_DATABASE", default=True)


def ensure_mysql_database_exists(config):
    database_name = config["database"]
    if not database_name:
        return
    if not mysql_auto_create_database_enabled():
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
        "telemetry_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "telemetry_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "command_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "command_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "relay_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "relay_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "ota_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "ota_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "local_firmware_upload_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "local_firmware_upload_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "buzzer_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "buzzer_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "led_display_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "led_display_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "lower_tank_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "lower_tank_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
        "slave_device_service_state TEXT NOT NULL DEFAULT 'UNKNOWN'": "slave_device_service_state VARCHAR(32) NOT NULL DEFAULT 'UNKNOWN'",
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
    sql = re.sub(r"\bWHERE\s+key\s+LIKE\b", "WHERE `key` LIKE", sql, flags=re.I)
    sql = re.sub(r"\bLIMIT\s+-1\s+OFFSET\b", "LIMIT 18446744073709551615 OFFSET", sql, flags=re.I)
    sql = sql.replace("?", "%s")
    return sql, tuple(params or ())


def mysql_exception_number(exc):
    return getattr(exc, "args", [None])[0] if isinstance(getattr(exc, "args", None), (list, tuple)) else None


def mysql_exception_message(exc):
    return str(exc or "").strip().lower()


def mysql_is_connection_recoverable_error(exc):
    code = mysql_exception_number(exc)
    message = mysql_exception_message(exc)
    return (
        code in {2006, 2013, 2055}
        or "gone away" in message
        or "lost connection" in message
        or "connection reset by peer" in message
    )


def mysql_is_lock_error(exc):
    code = mysql_exception_number(exc)
    message = mysql_exception_message(exc)
    return (
        code in {1205, 1213}
        or "lock wait timeout" in message
        or "deadlock found" in message
    )


def mysql_is_retryable_error(exc):
    return mysql_is_lock_error(exc) or mysql_is_connection_recoverable_error(exc)


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
    def __init__(self, connection_adapter, cursor):
        self.connection_adapter = connection_adapter
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
        # Connection loss does not prove that MySQL did not apply a write.
        # Replay only plain reads; writes and DDL fail back to the caller.
        max_attempts = 2 if statement_allows_connection_retry(translated_sql) else 1
        attempt = 0
        while attempt < max_attempts:
            try:
                self.cursor.execute(translated_sql, translated_params)
                self.lastrowid = self.cursor.lastrowid
                self.rowcount = self.cursor.rowcount
                return self
            except Exception as exc:
                attempt += 1
                if mysql_is_lock_error(exc):
                    raise
                if not mysql_is_connection_recoverable_error(exc) or attempt >= max_attempts:
                    raise
                logger.warning(
                    "Retrying MySQL execute after recoverable connection error (%s/%s): %s",
                    attempt,
                    max_attempts,
                    exc,
                )
                try:
                    self.connection_adapter.connection.rollback()
                except Exception:
                    pass
                try:
                    self.connection_adapter.reconnect()
                except Exception:
                    raise
                self.cursor = self.connection_adapter.connection.cursor()
                self._buffered_rows = None
                time.sleep(0.5 * attempt)
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
    def __init__(self, connection, pool=None, pool_created_at=None):
        self.connection = connection
        self.pool = pool
        self.pool_created_at = pool_created_at
        self.closed = False

    def cursor(self):
        return MySqlCursorAdapter(self, self.connection.cursor())

    def execute(self, sql, params=None):
        cursor = self.cursor()
        cursor.execute(sql, params)
        return cursor

    def reconnect(self):
        if self.pool is not None:
            self.pool.release(self.connection, self.pool_created_at, discard=True)
            self.connection, self.pool_created_at = self.pool.acquire()
            self.closed = False
            return
        try:
            if self.connection is not None:
                self.connection.close()
        except Exception:
            pass
        adapter = connect_mysql()
        self.connection = adapter.connection

    def commit(self):
        return self.connection.commit()

    def rollback(self):
        return self.connection.rollback()

    def close(self):
        if self.closed:
            return None
        self.closed = True
        if self.pool is not None:
            return self.pool.release(self.connection, self.pool_created_at)
        return self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                self.commit()
            finally:
                self.close()
            return False

        try:
            self.rollback()
        except Exception as rollback_exc:
            # Preserve the exception raised by the database operation. A lost
            # connection can make rollback fail as well, but that cleanup
            # failure must not hide the actionable root cause.
            logger.warning("MySQL rollback failed while handling an earlier error: %s", rollback_exc)
        finally:
            self.close()
        return False


_MYSQL_RESOLVED_LOCAL_PORT = None
_MYSQL_CONNECTION_POOL = None
_MYSQL_CONNECTION_POOL_LOCK = threading.Lock()


def connect_mysql_unpooled():
    global _MYSQL_RESOLVED_LOCAL_PORT
    if pymysql is None:
        raise RuntimeError("DB_BACKEND=mysql requires PyMySQL. Install requirements.txt first.")
    config = mysql_connection_config()
    local_mysql_host = str(config.get("host") or "").strip().lower() in {"localhost", "127.0.0.1", "::1"}
    if local_mysql_host and _MYSQL_RESOLVED_LOCAL_PORT is not None:
        config = dict(config)
        config["port"] = _MYSQL_RESOLVED_LOCAL_PORT
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
        "connect_timeout": max(5, env_int("MYSQL_CONNECT_TIMEOUT_SECONDS", 10)),
        "read_timeout": max(10, env_int("MYSQL_READ_TIMEOUT_SECONDS", 25)),
        "write_timeout": max(10, env_int("MYSQL_WRITE_TIMEOUT_SECONDS", 25)),
        "ssl": {"ca": ssl_ca} if ssl_ca else None,
    }
    try:
        conn = pymysql.connect(**connect_kwargs)
    except Exception as exc:
        error_code = getattr(exc, "args", [None])[0]
        configured_port = int(config["port"])
        if error_code == 2003 and local_mysql_host and configured_port != 3306:
            fallback_kwargs = dict(connect_kwargs)
            fallback_kwargs["port"] = 3306
            logger.warning(
                "MySQL refused localhost port %s; retrying standard port 3306.",
                configured_port,
            )
            try:
                conn = pymysql.connect(**fallback_kwargs)
            except Exception as fallback_exc:
                exc = fallback_exc
                error_code = getattr(fallback_exc, "args", [None])[0]
            else:
                _MYSQL_RESOLVED_LOCAL_PORT = 3306
                connect_kwargs = fallback_kwargs
                error_code = None
        if error_code is None:
            pass
        elif error_code != 1049:
            error_message = str(exc or "MySQL connection failed").strip()
            raise RuntimeError(
                f"MySQL connection failed (error {error_code}): {error_message}. "
                f"Check MYSQL_HOST={config['host']!r}, MYSQL_PORT={config['port']}, "
                f"MYSQL_USER={config['user']!r}, and make sure the MySQL service is running."
            ) from exc
        else:
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
        cursor.execute(
            "SET SESSION innodb_lock_wait_timeout = %s",
            (max(1, min(60, env_int("MYSQL_LOCK_WAIT_TIMEOUT_SECONDS", 10))),),
        )
    return MySqlConnectionAdapter(conn)


def mysql_connection_pool():
    global _MYSQL_CONNECTION_POOL
    if _MYSQL_CONNECTION_POOL is not None:
        return _MYSQL_CONNECTION_POOL
    with _MYSQL_CONNECTION_POOL_LOCK:
        if _MYSQL_CONNECTION_POOL is None:
            from flask_app.capacity_db_pool import MySqlConnectionPool

            def connection_factory():
                return connect_mysql_unpooled().connection

            _MYSQL_CONNECTION_POOL = MySqlConnectionPool(
                connection_factory,
                size=env_int("MYSQL_POOL_SIZE", 2),
                max_overflow=env_int("MYSQL_POOL_MAX_OVERFLOW", 1),
                recycle_seconds=env_int("MYSQL_POOL_RECYCLE_SECONDS", 240),
                wait_seconds=env_int("MYSQL_POOL_WAIT_SECONDS", 5),
            )
    return _MYSQL_CONNECTION_POOL


def connect_mysql():
    if not CAPACITY_FEATURES.enabled("db_connection_pool"):
        return connect_mysql_unpooled()
    pool = mysql_connection_pool()
    connection, created_at = pool.acquire()
    return MySqlConnectionAdapter(connection, pool=pool, pool_created_at=created_at)


def get_db():
    return connect_mysql()


DB_SCHEMA_REVISION = "2026-08-22-survey-approval-registration-v2"


def init_db_serialized():
    """Run schema work once per revision across all Passenger workers."""
    database_name = mysql_connection_config().get("database") or "swt"
    lock_name = f"swt_schema_init_{database_name}"[:64]
    with get_db() as lock_db:
        row = lock_db.execute("SELECT GET_LOCK(?, ?) AS acquired", (lock_name, 60)).fetchone()
        if int((row or {}).get("acquired") or 0) != 1:
            raise RuntimeError("Timed out waiting for the database schema initialization lock")
        try:
            table_row = lock_db.execute(
                """
                SELECT 1 AS present
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name = 'app_settings'
                LIMIT 1
                """
            ).fetchone()
            marker_row = lock_db.execute(
                "SELECT value FROM app_settings WHERE `key` = 'db_schema_revision' LIMIT 1"
            ).fetchone() if table_row else None
            if str((marker_row or {}).get("value") or "") == DB_SCHEMA_REVISION:
                logger.info("MySQL schema already current at revision %s", DB_SCHEMA_REVISION)
                return
            init_db()
            lock_db.execute(
                """
                INSERT INTO app_settings(`key`, value, updated_at)
                VALUES ('db_schema_revision', ?, CURRENT_TIMESTAMP)
                ON CONFLICT(`key`) DO UPDATE SET
                    value=excluded.value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (DB_SCHEMA_REVISION,),
            )
            lock_db.commit()
            logger.info("MySQL schema revision recorded as %s", DB_SCHEMA_REVISION)
        finally:
            lock_db.execute("SELECT RELEASE_LOCK(?) AS released", (lock_name,))


def ensure_tank_data_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(tank_data)").fetchall()}
    required = {
        "device_source": "TEXT",
        "sensor_info": "TEXT",
        "sensor_distance_cm": "REAL",
        "tank_height_cm": "REAL",
        "tank_capacity_liters": "REAL",
        "auto_start_pct": "REAL",
        "auto_stop_pct": "REAL",
        "auto_start_stable_ms": "INTEGER",
        "auto_level_average_samples": "INTEGER",
        "auto_status": "TEXT",
        "auto_status_tone": "TEXT",
        "auto_timer": "TEXT",
        "tank_health": "REAL",
        "free_heap": "INTEGER",
        "cpu_utilization_pct": "REAL",
        "slave_free_heap": "INTEGER",
        "slave_cpu_utilization_pct": "REAL",
        "slave_uptime_s": "INTEGER",
        "uptime_s": "INTEGER",
        "lower_tank_level": "REAL",
        "lower_sensor": "TEXT",
        "lower_sensor_info": "TEXT",
        "lower_sensor_distance_cm": "REAL",
        "municipal_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "municipal_sensor_state": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
        "municipal_sensor_simulated": "INTEGER NOT NULL DEFAULT 0",
        "municipal_sensor_reachable": "INTEGER NOT NULL DEFAULT 0",
        "municipal_sensor_last_updated": "TEXT",
        "municipal_valve_simulated": "INTEGER NOT NULL DEFAULT 0",
        "municipal_valve_feature_enabled": "INTEGER NOT NULL DEFAULT 0",
        "source_outlet_valve_simulated": "INTEGER NOT NULL DEFAULT 0",
        "source_pump_fill_feature_enabled": "INTEGER NOT NULL DEFAULT 0",
        "lower_turbidity_simulated": "INTEGER NOT NULL DEFAULT 0",
        "upper_turbidity_simulated": "INTEGER NOT NULL DEFAULT 0",
        "device_id": "TEXT",
        "firmware_version": "TEXT",
        "slave_firmware_version": "TEXT",
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
        "direct_peer_remote_mac": "TEXT",
        "direct_peer_config_channel": "INTEGER",
        "direct_peer_wifi_channel": "INTEGER",
        "direct_peer_last_packet_age_s": "INTEGER",
        "direct_peer_last_packet_bytes": "INTEGER",
        "direct_peer_last_sequence": "BIGINT",
        "direct_peer_duplicate_packets": "BIGINT",
        "direct_peer_out_of_order_packets": "BIGINT",
        "direct_peer_estimated_lost_packets": "BIGINT",
        "direct_peer_last_pong_age_s": "INTEGER",
        "direct_peer_last_pong_nonce": "INTEGER",
        "direct_peer_sync_pending": "INTEGER",
        "direct_peer_sync_channel": "INTEGER",
        "direct_peer_sync_last_ok_age_s": "INTEGER",
        "last_ping_target": "TEXT",
        "last_ping_status": "TEXT",
        "last_ping_response_ms": "INTEGER",
        "last_ping_age_s": "INTEGER",
        "last_ping_nonce": "INTEGER",
        "telemetry_fingerprint": "VARCHAR(64)",
        "controller_state": "VARCHAR(32)",
        "upper_high_float_enabled": "INTEGER NOT NULL DEFAULT 0",
        "upper_high_float_active": "INTEGER NOT NULL DEFAULT 0",
        "source_low_float_enabled": "INTEGER NOT NULL DEFAULT 0",
        "source_low_float_active": "INTEGER NOT NULL DEFAULT 0",
        "starter_contactor_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "starter_contactor_active": "INTEGER NOT NULL DEFAULT 0",
        "motor_current_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "motor_current_detected": "INTEGER NOT NULL DEFAULT 0",
        "water_flow_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "water_flow_detected": "INTEGER NOT NULL DEFAULT 0",
        "water_pressure_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "water_pressure_detected": "INTEGER NOT NULL DEFAULT 0",
        "physical_pump_running": "INTEGER NOT NULL DEFAULT 0",
        "pump_confirmation_source": "VARCHAR(32)",
        "pump_total_runtime_s": "BIGINT",
        "pump_last_run_runtime_s": "BIGINT",
        "pump_cycle_count": "BIGINT",
        "pump_run_started_uptime_s": "BIGINT",
        "pump_runtime_boot_id": "VARCHAR(64)",
    }

    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE tank_data ADD COLUMN {column} {definition}")


def ensure_tank_data_mysql_column_types(cursor):
    existing_types = {
        row[1]: str(row[2] or "").strip().lower()
        for row in cursor.execute("PRAGMA table_info(tank_data)").fetchall()
    }
    for column in ("runtime", "current_runtime", "last_runtime", "fill_time"):
        if existing_types.get(column) == "text":
            continue
        cursor.execute(f"ALTER TABLE tank_data MODIFY COLUMN {column} TEXT")


def drop_obsolete_columns(cursor, table_name, obsolete_columns):
    existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()}
    for column in obsolete_columns:
        if column not in existing:
            continue
        logger.info("Dropping obsolete database column %s.%s", table_name, column)
        cursor.execute(
            f"ALTER TABLE {quote_mysql_identifier(table_name)} "
            f"DROP COLUMN {quote_mysql_identifier(column)}"
        )


def remove_obsolete_schema_columns(cursor):
    drop_obsolete_columns(cursor, "tank_data", ("simulator", "source_tank_simulator"))


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
            cpu_utilization_pct REAL,
            slave_free_heap INTEGER,
            slave_cpu_utilization_pct REAL,
            slave_uptime_s INTEGER,
            uptime_s INTEGER,
            lower_tank_level REAL,
            lower_sensor TEXT,
            lower_sensor_info TEXT,
            lower_sensor_distance_cm REAL,
            municipal_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            municipal_sensor_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
            municipal_sensor_simulated INTEGER NOT NULL DEFAULT 0,
            municipal_sensor_reachable INTEGER NOT NULL DEFAULT 0,
            municipal_sensor_last_updated TEXT,
            municipal_valve_simulated INTEGER NOT NULL DEFAULT 0,
            municipal_valve_feature_enabled INTEGER NOT NULL DEFAULT 0,
            source_outlet_valve_simulated INTEGER NOT NULL DEFAULT 0,
            source_pump_fill_feature_enabled INTEGER NOT NULL DEFAULT 0,
            lower_turbidity_simulated INTEGER NOT NULL DEFAULT 0,
            upper_turbidity_simulated INTEGER NOT NULL DEFAULT 0,
            device_id TEXT,
            firmware_version TEXT,
            slave_firmware_version TEXT,
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
            direct_peer_remote_mac TEXT,
            direct_peer_config_channel INTEGER,
            direct_peer_wifi_channel INTEGER,
            direct_peer_last_packet_age_s INTEGER,
            direct_peer_last_packet_bytes INTEGER,
            direct_peer_last_pong_age_s INTEGER,
            direct_peer_last_pong_nonce INTEGER,
            direct_peer_sync_pending INTEGER,
            direct_peer_sync_channel INTEGER,
            direct_peer_sync_last_ok_age_s INTEGER,
            last_ping_target TEXT,
            last_ping_status TEXT,
            last_ping_response_ms INTEGER,
            last_ping_age_s INTEGER,
            last_ping_nonce INTEGER,
            telemetry_fingerprint VARCHAR(64),
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
            request_id VARCHAR(64),
            desired_state VARCHAR(16),
            status VARCHAR(24) NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            accepted_at TEXT,
            completed_at TEXT,
            result_reason TEXT,
            result_json LONGTEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            delivered_at TEXT
        )
        """
    )


def ensure_device_command_queue_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(device_command_queue)").fetchall()}
    required = {
        "request_id": "VARCHAR(64)", "desired_state": "VARCHAR(16)",
        "status": "VARCHAR(24) NOT NULL DEFAULT 'queued'", "priority": "INTEGER NOT NULL DEFAULT 0",
        "expires_at": "TEXT", "accepted_at": "TEXT", "completed_at": "TEXT",
        "result_reason": "TEXT", "result_json": "LONGTEXT",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE device_command_queue ADD COLUMN {column} {definition}")


def ensure_dashboard_summary_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_summaries(
            device_id VARCHAR(255) NOT NULL,
            device_source VARCHAR(32) NOT NULL,
            summary_json LONGTEXT NOT NULL,
            source_updated_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(device_id, device_source)
        )
        """
    )


def ensure_device_mobile_action_queue_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS device_mobile_action_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_device TEXT NOT NULL,
            action TEXT NOT NULL,
            payload_json TEXT,
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
            target_role VARCHAR(16) NOT NULL DEFAULT 'master',
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


def ensure_firmware_artifacts_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(firmware_artifacts)").fetchall()}
    required = {
        "target_role": "VARCHAR(16) NOT NULL DEFAULT 'master'",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE firmware_artifacts ADD COLUMN {column} {definition}")


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


def ensure_survey_responses_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS survey_responses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_token VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(160) NOT NULL,
            email VARCHAR(255),
            contact_number VARCHAR(40),
            overall_experience VARCHAR(32) NOT NULL,
            primary_use VARCHAR(64) NOT NULL,
            most_valuable_feature VARCHAR(64) NOT NULL,
            reliability_rating INTEGER NOT NULL,
            ease_of_use_rating INTEGER NOT NULL,
            would_recommend VARCHAR(16) NOT NULL,
            answers_json LONGTEXT NOT NULL,
            comments TEXT,
            submitted_by_role VARCHAR(32),
            submitted_by_username VARCHAR(255),
            submitted_by_device_id VARCHAR(255),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_survey_responses_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(survey_responses)").fetchall()}
    required = {
        "review_status": "VARCHAR(20) NOT NULL DEFAULT 'pending'",
        "reviewed_at": "TEXT",
        "reviewed_by": "VARCHAR(255)",
        "registered_device_id": "VARCHAR(255)",
        "registration_config_json": "LONGTEXT",
        "registered_at": "TEXT",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE survey_responses ADD COLUMN {column} {definition}")


def ensure_device_service_configs_table(cursor):
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS device_service_configs(
            device_id TEXT PRIMARY KEY,
            device_setup_type VARCHAR(32) NOT NULL DEFAULT 'custom',
            main_sensor_enabled INTEGER NOT NULL DEFAULT 1,
            master_upper_sensor_enabled INTEGER NOT NULL DEFAULT 1,
            slave_device_enabled INTEGER NOT NULL DEFAULT 1,
            slave_upper_sensor_enabled INTEGER NOT NULL DEFAULT 1,
            source_tank_monitoring_enabled INTEGER NOT NULL DEFAULT 1,
            municipal_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            municipal_valve_enabled INTEGER NOT NULL DEFAULT 0,
            source_outlet_valve_enabled INTEGER NOT NULL DEFAULT 0,
            starter_contactor_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            motor_current_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            water_flow_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            water_pressure_sensor_enabled INTEGER NOT NULL DEFAULT 0,
            turbidity_monitoring_enabled INTEGER NOT NULL DEFAULT 0,
            master_turbidity_enabled INTEGER NOT NULL DEFAULT 0,
            slave_turbidity_enabled INTEGER NOT NULL DEFAULT 0,
            relay_enabled INTEGER NOT NULL DEFAULT 1,
            ai_analysis_enabled INTEGER NOT NULL DEFAULT 1,
            cloud_feed_mode TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}',
            ota_enabled INTEGER NOT NULL DEFAULT 0,
            local_firmware_upload_enabled INTEGER NOT NULL DEFAULT 1,
            buzzer_enabled INTEGER NOT NULL DEFAULT 1,
            led_display_enabled INTEGER NOT NULL DEFAULT 1,
            auto_mode_enabled INTEGER NOT NULL DEFAULT 0,
            android_sso_session_limit INTEGER NOT NULL DEFAULT 1,
            tank_height_cm REAL,
            tank_capacity_liters REAL,
            upper_tank_height_cm REAL,
            upper_tank_capacity_liters REAL,
            lower_tank_height_cm REAL,
            lower_tank_capacity_liters REAL,
            auto_start_pct REAL,
            auto_stop_pct REAL,
            direct_peer_wifi_channel INTEGER,
            telemetry_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            command_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            relay_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            ota_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            local_firmware_upload_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            buzzer_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            led_display_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            lower_tank_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            slave_device_service_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def ensure_device_service_configs_columns(cursor):
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(device_service_configs)").fetchall()}
    added_auto_mode_enabled = False
    required = {
        "device_setup_type": "VARCHAR(32) NOT NULL DEFAULT 'custom'",
        "main_sensor_enabled": "INTEGER NOT NULL DEFAULT 1",
        "master_upper_sensor_enabled": "INTEGER NOT NULL DEFAULT 1",
        "slave_device_enabled": "INTEGER NOT NULL DEFAULT 1",
        "slave_upper_sensor_enabled": "INTEGER NOT NULL DEFAULT 1",
        "source_tank_monitoring_enabled": "INTEGER NOT NULL DEFAULT 1",
        "municipal_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "municipal_valve_enabled": "INTEGER NOT NULL DEFAULT 0",
        "source_outlet_valve_enabled": "INTEGER NOT NULL DEFAULT 0",
        "starter_contactor_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "motor_current_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "water_flow_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "water_pressure_sensor_enabled": "INTEGER NOT NULL DEFAULT 0",
        "turbidity_monitoring_enabled": "INTEGER NOT NULL DEFAULT 0",
        "master_turbidity_enabled": "INTEGER NOT NULL DEFAULT 0",
        "slave_turbidity_enabled": "INTEGER NOT NULL DEFAULT 0",
        "relay_enabled": "INTEGER NOT NULL DEFAULT 1",
        "ai_analysis_enabled": "INTEGER NOT NULL DEFAULT 1",
        "cloud_feed_mode": f"TEXT NOT NULL DEFAULT '{DEVICE_SERVICE_CLOUD_FEED_FULL}'",
        "ota_enabled": "INTEGER NOT NULL DEFAULT 0",
        "local_firmware_upload_enabled": "INTEGER NOT NULL DEFAULT 1",
        "buzzer_enabled": "INTEGER NOT NULL DEFAULT 1",
        "led_display_enabled": "INTEGER NOT NULL DEFAULT 1",
        "auto_mode_enabled": "INTEGER NOT NULL DEFAULT 0",
        "android_sso_session_limit": "INTEGER NOT NULL DEFAULT 1",
        "tank_height_cm": "REAL",
        "tank_capacity_liters": "REAL",
        "upper_tank_height_cm": "REAL",
        "upper_tank_capacity_liters": "REAL",
        "lower_tank_height_cm": "REAL",
        "lower_tank_capacity_liters": "REAL",
        "auto_start_pct": "REAL",
        "auto_stop_pct": "REAL",
        "direct_peer_wifi_channel": "INTEGER",
        "telemetry_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "command_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "relay_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "ota_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "local_firmware_upload_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "buzzer_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "led_display_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "lower_tank_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "slave_device_service_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
        "created_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "TEXT DEFAULT CURRENT_TIMESTAMP",
    }
    for column, definition in required.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE device_service_configs ADD COLUMN {column} {definition}")
            if column == "auto_mode_enabled":
                added_auto_mode_enabled = True
    if added_auto_mode_enabled:
        cursor.execute(
            """
            UPDATE device_service_configs
            SET auto_mode_enabled = 1
            WHERE auto_mode_enabled IS NULL OR auto_mode_enabled = 0
            """
        )


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
            device_key_ciphertext LONGTEXT,
            registration_source TEXT NOT NULL,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(device_auth_keys)").fetchall()}
    if "device_key_ciphertext" not in existing:
        cursor.execute("ALTER TABLE device_auth_keys ADD COLUMN device_key_ciphertext LONGTEXT")


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


def ensure_performance_indexes(cursor):
    index_statements = (
        "CREATE INDEX idx_created_at ON tank_data(created_at)",
        "CREATE INDEX idx_tank_data_device_created ON tank_data(device_id, created_at DESC, id DESC)",
        "CREATE INDEX idx_tank_data_source_created ON tank_data(device_source(16), created_at DESC, id DESC)",
        "CREATE INDEX idx_tank_data_device_source_created ON tank_data(device_id, device_source(16), created_at DESC, id DESC)",
        "CREATE INDEX idx_tank_data_device_source_id ON tank_data(device_id, device_source(16), id DESC)",
        "CREATE INDEX idx_tank_data_device_fingerprint ON tank_data(device_id, telemetry_fingerprint, created_at DESC, id DESC)",
        "CREATE INDEX idx_alerts_active ON ops_alerts(active, kind, device_id)",
        "CREATE INDEX idx_alerts_device_active_updated ON ops_alerts(device_id, active, updated_at DESC, id DESC)",
        "CREATE INDEX idx_device_events_device_event_at ON device_events(device_id, event_at DESC, id DESC)",
        "CREATE INDEX idx_device_events_kind_event_at ON device_events(event_kind, event_at DESC)",
        "CREATE INDEX idx_device_command_queue_target_pending ON device_command_queue(target_device, delivered_at, id DESC)",
        "CREATE INDEX idx_device_command_queue_target_command ON device_command_queue(target_device, delivered_at, command, id DESC)",
        "CREATE INDEX idx_device_mobile_action_queue_target_pending ON device_mobile_action_queue(target_device, delivered_at, id DESC)",
        "CREATE INDEX idx_device_mobile_action_queue_target_action ON device_mobile_action_queue(target_device, delivered_at, action, id DESC)",
        "CREATE INDEX idx_relay_queue_next_attempt ON relay_queue(next_attempt_at, id)",
        "CREATE INDEX idx_firmware_artifacts_target_created ON firmware_artifacts(target_device, created_at DESC, id DESC)",
        "CREATE INDEX idx_firmware_artifacts_target_role_created ON firmware_artifacts(target_device, target_role, created_at DESC, id DESC)",
        "CREATE INDEX idx_android_app_releases_created ON android_app_releases(created_at DESC, id DESC)",
        "CREATE INDEX idx_android_app_releases_version_created ON android_app_releases(version_code DESC, created_at DESC, id DESC)",
        "CREATE INDEX idx_audit_device_created ON ops_audit_log(device_id, created_at DESC, id DESC)",
        "CREATE INDEX idx_customer_accounts_email_updated ON customer_accounts(email, updated_at DESC)",
        "CREATE INDEX idx_customer_password_reset_expires ON customer_password_reset_tokens(expires_at, used_at)",
        "CREATE INDEX idx_survey_responses_created ON survey_responses(created_at DESC, id DESC)",
        "CREATE INDEX idx_registered_devices_last_seen ON registered_devices(last_seen_at, device_id)",
        "CREATE INDEX idx_device_auth_keys_updated ON device_auth_keys(updated_at, device_id)",
        "CREATE INDEX idx_device_service_configs_updated ON device_service_configs(updated_at, device_id)",
        "CREATE INDEX idx_ignored_devices_updated ON ignored_devices(updated_at, device_id)",
    )
    for statement in index_statements:
        try:
            cursor.execute(statement)
        except Exception as exc:
            if "duplicate" not in str(exc).lower():
                raise


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
                auto_start_pct REAL,
                auto_stop_pct REAL,
                auto_start_stable_ms INTEGER,
                auto_level_average_samples INTEGER,
                auto_status TEXT,
                auto_status_tone TEXT,
                auto_timer TEXT,
                tank_health REAL,
                free_heap INTEGER,
                cpu_utilization_pct REAL,
                slave_free_heap INTEGER,
                slave_cpu_utilization_pct REAL,
                slave_uptime_s INTEGER,
                uptime_s INTEGER,
                lower_tank_level REAL,
                lower_sensor TEXT,
                lower_sensor_info TEXT,
                lower_sensor_distance_cm REAL,
                municipal_sensor_enabled INTEGER NOT NULL DEFAULT 0,
                municipal_sensor_state VARCHAR(32) NOT NULL DEFAULT 'unknown',
                municipal_sensor_simulated INTEGER NOT NULL DEFAULT 0,
                municipal_sensor_reachable INTEGER NOT NULL DEFAULT 0,
                municipal_sensor_last_updated TEXT,
                municipal_valve_simulated INTEGER NOT NULL DEFAULT 0,
                municipal_valve_feature_enabled INTEGER NOT NULL DEFAULT 0,
                source_outlet_valve_simulated INTEGER NOT NULL DEFAULT 0,
                source_pump_fill_feature_enabled INTEGER NOT NULL DEFAULT 0,
                lower_turbidity_simulated INTEGER NOT NULL DEFAULT 0,
                upper_turbidity_simulated INTEGER NOT NULL DEFAULT 0,
                device_id TEXT,
                firmware_version TEXT,
                slave_firmware_version TEXT,
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
                direct_peer_remote_mac TEXT,
            direct_peer_config_channel INTEGER,
            direct_peer_wifi_channel INTEGER,
            direct_peer_last_packet_age_s INTEGER,
            direct_peer_last_packet_bytes INTEGER,
            direct_peer_last_pong_age_s INTEGER,
            direct_peer_last_pong_nonce INTEGER,
            direct_peer_sync_pending INTEGER,
            direct_peer_sync_channel INTEGER,
            direct_peer_sync_last_ok_age_s INTEGER,
            last_ping_target TEXT,
            last_ping_status TEXT,
            last_ping_response_ms INTEGER,
            last_ping_age_s INTEGER,
            last_ping_nonce INTEGER,
            telemetry_fingerprint VARCHAR(64),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
            """
        )
        ensure_tank_data_columns(cursor)
        ensure_tank_data_mysql_column_types(cursor)
        remove_obsolete_schema_columns(cursor)
        ensure_relay_queue_table(cursor)
        ensure_device_command_queue_table(cursor)
        ensure_device_command_queue_columns(cursor)
        ensure_dashboard_summary_table(cursor)
        ensure_device_mobile_action_queue_table(cursor)
        ensure_firmware_artifacts_table(cursor)
        ensure_firmware_artifacts_columns(cursor)
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
        ensure_survey_responses_table(cursor)
        ensure_survey_responses_columns(cursor)
        ensure_device_service_configs_table(cursor)
        ensure_device_service_configs_columns(cursor)
        ensure_registered_devices_table(cursor)
        ensure_device_auth_keys_table(cursor)
        ensure_ignored_devices_table(cursor)
        if CAPACITY_FEATURES.enabled("capacity_schema"):
            ensure_capacity_schema(cursor)
        seed_bootstrap_customer_accounts(cursor)
        seed_default_customer_accounts(cursor)
        ensure_performance_indexes(cursor)

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


def load_homepage_visitor_count_from_db():
    try:
        raw_value = get_app_setting(HOMEPAGE_VISITOR_COUNT_SETTING, "0")
        return max(0, int(str(raw_value or "0").strip()))
    except Exception as exc:
        logger.warning("Unable to load persisted homepage visitor count: %s", exc)
        return 0


def ensure_homepage_visitor_count_loaded():
    global homepage_visitor_count_cached
    if homepage_visitor_count_cached is None:
        loaded_count = load_homepage_visitor_count_from_db()
        with homepage_visitor_count_lock:
            if homepage_visitor_count_cached is None:
                homepage_visitor_count_cached = loaded_count
    return homepage_visitor_count_cached or 0


def default_local_web_auth_password():
    return os.environ.get("SWT_LOCAL_WEB_AUTH_PASSWORD", "").strip() or "lOpbDRMeXBokNcQ4Y7lfgWDzPretehDY"


def device_local_web_password_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    return f"{DEVICE_LOCAL_WEB_PASSWORD_PREFIX}{normalized_device_id}" if normalized_device_id else None


def fetch_device_local_web_password(device_id, default_to_env=True):
    setting_key = device_local_web_password_key(device_id)
    raw_value = get_app_setting(setting_key, "") if setting_key else ""
    password = str(raw_value or "").strip()
    if password:
        return password
    return default_local_web_auth_password() if default_to_env else ""


def save_device_local_web_password(device_id, password):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")
    resolved_password = str(password or "").strip()
    if len(resolved_password) < 6:
        raise ValueError("Local web password must be at least 6 characters.")
    set_app_setting(device_local_web_password_key(normalized_device_id), resolved_password)
    return resolved_password


def increment_homepage_visitor_count():
    global homepage_visitor_count_cached
    try:
        with get_db() as db:
            db.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value=app_settings.value + 1,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (HOMEPAGE_VISITOR_COUNT_SETTING,),
            )
            row = db.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (HOMEPAGE_VISITOR_COUNT_SETTING,),
            ).fetchone()
        display_count = max(0, int(str(row["value"] if row else "0").strip()))
        with homepage_visitor_count_lock:
            homepage_visitor_count_cached = display_count
    except Exception as exc:
        logger.warning("Homepage visitor counter update failed: %s", exc)
        with homepage_visitor_count_lock:
            homepage_visitor_count_cached = (homepage_visitor_count_cached or 0) + 1
            display_count = homepage_visitor_count_cached
    return display_count


def format_count_label(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


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
    if normalized_mode == DEVICE_SOURCE_REAL:
        return f"({column} = ? OR {column} IS NULL)", [DEVICE_SOURCE_REAL]
    return f"{column} = ?", [normalized_mode]


def ensure_app_secret_key_persisted():
    persisted_secret = str(get_app_setting(APP_SECRET_KEY_SETTING, "") or "").strip()
    if persisted_secret:
        if APP_SECRET_KEY_SOURCE == "env" and persisted_secret != app.secret_key:
            # A stable deployment environment is authoritative. Persist it so
            # every future worker (and a later env-file recovery) sees the same
            # session-signing key instead of repeating a misleading warning.
            set_app_setting(APP_SECRET_KEY_SETTING, app.secret_key)
            logger.info("Database-persisted APP_SECRET_KEY synchronized with the environment.")
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
    return build_auth_marker("customer", normalized_device_id, password_hash, current_mobile_session_epoch(normalized_device_id))


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


MASTER_ONLY_FIRMWARE_BUILD_FLAGS = {
    "SWT_FEATURE_MASTER_LOWER_SENSOR": "0",
    "SWT_ARCH_ID": "4",
    "SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT": "1",
    "SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT": "0",
    "SWT_DIRECT_PEER_ENABLED": "0",
}
MASTER_SLAVE_FIRMWARE_BUILD_FLAGS = {
    "SWT_FEATURE_MASTER_LOWER_SENSOR": "1",
    "SWT_ARCH_ID": "1",
    "SWT_MASTER_LOCAL_UPPER_SENSOR_COUNT": "0",
    "SWT_MASTER_REMOTE_UPPER_SENSOR_COUNT": "1",
    "SWT_DIRECT_PEER_ENABLED": "1",
}


def build_device_firmware_install_profile(service_config):
    config = service_config or {}
    master_slave_enabled = bool(config.get("slave_device_enabled", True))
    flags = MASTER_SLAVE_FIRMWARE_BUILD_FLAGS if master_slave_enabled else MASTER_ONLY_FIRMWARE_BUILD_FLAGS
    return {
        "configuration_type": "master_slave" if master_slave_enabled else "master_only",
        "label": "Master + Slave" if master_slave_enabled else "Master Only",
        "description": (
            "Build swt_master for a pump master that receives upper tank level from a slave MCU."
            if master_slave_enabled
            else "Build swt_master for a single MCU that reads the upper tank sensor locally."
        ),
        "requires_slave_firmware": master_slave_enabled,
        "flags": dict(flags),
        "flag_rows": [{"key": key, "value": value} for key, value in flags.items()],
    }


def normalize_optional_config_float(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def normalize_optional_service_state(value, default="UNKNOWN"):
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return normalize_service_state(value)


def serialize_device_service_config(device_id, payload=None, account=None):
    payload = payload or {}
    device_setup_type = str(payload.get("device_setup_type") or "custom").strip().lower()
    normalized_device_id = normalize_device_id(device_id or payload.get("device_id"))
    account_cloud_feed_enabled = True
    if account is not None:
        account_cloud_feed_enabled = int(account.get("cloud_feed_enabled", 1) or 0) == 1

    cloud_feed_mode = normalize_device_service_cloud_mode(
        payload.get("cloud_feed_mode"),
        default=DEVICE_SERVICE_CLOUD_FEED_FULL if account_cloud_feed_enabled else DEVICE_SERVICE_CLOUD_FEED_OFF,
    )
    legacy_slave_enabled = boolish_enabled(payload.get("slave_device_enabled"), default=True)
    slave_upper_sensor_enabled = boolish_enabled(
        payload.get("slave_upper_sensor_enabled"),
        default=legacy_slave_enabled,
    )
    slave_device_enabled = legacy_slave_enabled and slave_upper_sensor_enabled
    master_upper_sensor_enabled = boolish_enabled(
        payload.get("master_upper_sensor_enabled"),
        default=not slave_device_enabled,
    )
    if not slave_device_enabled:
        master_upper_sensor_enabled = True
        slave_upper_sensor_enabled = False
    main_sensor_enabled = master_upper_sensor_enabled or slave_upper_sensor_enabled or boolish_enabled(
        payload.get("main_sensor_enabled"),
        default=True,
    )
    source_tank_monitoring_enabled = boolish_enabled(payload.get("source_tank_monitoring_enabled"), default=True)
    municipal_sensor_enabled = boolish_enabled(payload.get("municipal_sensor_enabled"), default=False)
    municipal_valve_enabled = boolish_enabled(payload.get("municipal_valve_enabled"), default=False)
    source_outlet_valve_enabled = boolish_enabled(payload.get("source_outlet_valve_enabled"), default=False)
    starter_contactor_sensor_enabled = boolish_enabled(payload.get("starter_contactor_sensor_enabled"), default=False)
    motor_current_sensor_enabled = boolish_enabled(payload.get("motor_current_sensor_enabled"), default=False)
    water_flow_sensor_enabled = boolish_enabled(payload.get("water_flow_sensor_enabled"), default=False)
    water_pressure_sensor_enabled = boolish_enabled(payload.get("water_pressure_sensor_enabled"), default=False)
    turbidity_monitoring_enabled = boolish_enabled(payload.get("turbidity_monitoring_enabled"), default=False)
    master_turbidity_enabled = boolish_enabled(payload.get("master_turbidity_enabled"), default=turbidity_monitoring_enabled)
    slave_turbidity_enabled = boolish_enabled(payload.get("slave_turbidity_enabled"), default=turbidity_monitoring_enabled)
    turbidity_monitoring_enabled = master_turbidity_enabled or slave_turbidity_enabled
    relay_enabled = boolish_enabled(payload.get("relay_enabled"), default=True)
    ai_analysis_enabled = boolish_enabled(payload.get("ai_analysis_enabled"), default=True)
    ota_enabled = boolish_enabled(payload.get("ota_enabled"), default=False)
    local_firmware_upload_enabled = boolish_enabled(payload.get("local_firmware_upload_enabled"), default=True)
    buzzer_enabled = boolish_enabled(payload.get("buzzer_enabled"), default=True)
    led_display_enabled = boolish_enabled(payload.get("led_display_enabled"), default=True)
    auto_mode_enabled = boolish_enabled(payload.get("auto_mode_enabled"), default=False)
    android_sso_session_limit = normalize_android_sso_session_limit(payload.get("android_sso_session_limit"))
    tank_height_cm = normalize_optional_config_float(payload.get("tank_height_cm"))
    tank_capacity_liters = normalize_optional_config_float(payload.get("tank_capacity_liters"))
    upper_tank_height_cm = normalize_optional_config_float(
        payload.get("upper_tank_height_cm") if "upper_tank_height_cm" in payload else tank_height_cm
    )
    upper_tank_capacity_liters = normalize_optional_config_float(
        payload.get("upper_tank_capacity_liters") if "upper_tank_capacity_liters" in payload else tank_capacity_liters
    )
    lower_tank_height_cm = normalize_optional_config_float(payload.get("lower_tank_height_cm"))
    lower_tank_capacity_liters = normalize_optional_config_float(payload.get("lower_tank_capacity_liters"))
    auto_start_pct = normalize_optional_config_float(payload.get("auto_start_pct"))
    auto_stop_pct = normalize_optional_config_float(payload.get("auto_stop_pct"))
    direct_peer_wifi_channel = _coerce_optional_peer_channel_value(payload.get("direct_peer_wifi_channel"))
    telemetry_service_state = normalize_optional_service_state(payload.get("telemetry_service_state"))
    command_service_state = normalize_optional_service_state(payload.get("command_service_state"))
    relay_service_state = normalize_optional_service_state(payload.get("relay_service_state"))
    ota_service_state = normalize_optional_service_state(payload.get("ota_service_state"))
    local_firmware_upload_service_state = normalize_optional_service_state(
        payload.get("local_firmware_upload_service_state")
    )
    buzzer_service_state = normalize_optional_service_state(payload.get("buzzer_service_state"))
    led_display_service_state = normalize_optional_service_state(payload.get("led_display_service_state"))
    lower_tank_service_state = normalize_optional_service_state(payload.get("lower_tank_service_state"))
    slave_device_service_state = normalize_optional_service_state(payload.get("slave_device_service_state"))
    effective_cloud_feed_enabled = cloud_feed_mode != DEVICE_SERVICE_CLOUD_FEED_OFF and account_cloud_feed_enabled
    effective_ai_analysis_enabled = (
        ai_analysis_enabled
        and cloud_feed_mode == DEVICE_SERVICE_CLOUD_FEED_FULL
        and effective_cloud_feed_enabled
    )
    hardware_enabled_count = (
        int(relay_enabled)
        + int(ota_enabled)
        + int(local_firmware_upload_enabled)
        + int(buzzer_enabled)
        + int(led_display_enabled)
    )
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
        "device_setup_type": device_setup_type,
        "main_sensor_enabled": main_sensor_enabled,
        "master_upper_sensor_enabled": master_upper_sensor_enabled,
        "slave_device_enabled": slave_device_enabled,
        "slave_upper_sensor_enabled": slave_upper_sensor_enabled,
        "upper_sensor_source": "slave" if slave_device_enabled else "master",
        "source_tank_monitoring_enabled": source_tank_monitoring_enabled,
        "municipal_sensor_enabled": municipal_sensor_enabled,
        "municipal_valve_enabled": municipal_valve_enabled,
        "source_outlet_valve_enabled": source_outlet_valve_enabled,
        "starter_contactor_sensor_enabled": starter_contactor_sensor_enabled,
        "motor_current_sensor_enabled": motor_current_sensor_enabled,
        "water_flow_sensor_enabled": water_flow_sensor_enabled,
        "water_pressure_sensor_enabled": water_pressure_sensor_enabled,
        "turbidity_monitoring_enabled": turbidity_monitoring_enabled,
        "master_turbidity_enabled": master_turbidity_enabled,
        "slave_turbidity_enabled": slave_turbidity_enabled,
        "relay_enabled": relay_enabled,
        "ai_analysis_enabled": ai_analysis_enabled,
        "effective_ai_analysis_enabled": effective_ai_analysis_enabled,
        "cloud_feed_mode": cloud_feed_mode,
        "cloud_feed_mode_label": DEVICE_SERVICE_CLOUD_MODE_LABELS.get(cloud_feed_mode, "Unknown"),
        "cloud_feed_enabled": effective_cloud_feed_enabled,
        "cloud_note": cloud_note,
        "auto_mode_enabled": auto_mode_enabled,
        "auto_mode_label": "Enabled" if auto_mode_enabled else "Disabled",
        "ota_enabled": ota_enabled,
        "local_firmware_upload_enabled": local_firmware_upload_enabled,
        "buzzer_enabled": buzzer_enabled,
        "led_display_enabled": led_display_enabled,
        "android_sso_session_limit": android_sso_session_limit,
        "tank_height_cm": tank_height_cm,
        "tank_capacity_liters": tank_capacity_liters,
        "upper_tank_height_cm": upper_tank_height_cm,
        "upper_tank_capacity_liters": upper_tank_capacity_liters,
        "lower_tank_height_cm": lower_tank_height_cm,
        "lower_tank_capacity_liters": lower_tank_capacity_liters,
        "auto_start_pct": auto_start_pct,
        "auto_stop_pct": auto_stop_pct,
        "direct_peer_wifi_channel": direct_peer_wifi_channel,
        "telemetry_service_state": telemetry_service_state,
        "command_service_state": command_service_state,
        "relay_service_state": relay_service_state,
        "ota_service_state": ota_service_state,
        "local_firmware_upload_service_state": local_firmware_upload_service_state,
        "buzzer_service_state": buzzer_service_state,
        "led_display_service_state": led_display_service_state,
        "lower_tank_service_state": lower_tank_service_state,
        "slave_device_service_state": slave_device_service_state,
        "android_sso_session_limit_label": f"{android_sso_session_limit} Android device{'s' if android_sso_session_limit != 1 else ''}",
        "android_sso_session_limit_max": MAX_ANDROID_SSO_SESSION_LIMIT,
        "hardware_enabled_count": hardware_enabled_count,
        "hardware_enabled_label": f"{hardware_enabled_count}/5 device services active",
        "service_profile_hint": (
            f"Source {'On' if source_tank_monitoring_enabled else 'Off'}"
            f" | Municipal {'On' if municipal_sensor_enabled else 'Off'}"
            f" | Inlet valve {'On' if municipal_valve_enabled else 'Off'}"
            f" | Outlet valve {'On' if source_outlet_valve_enabled else 'Off'}"
            f" | Turbidity {'On' if turbidity_monitoring_enabled else 'Off'}"
            f" | Upper {'Slave' if slave_device_enabled else 'Master'}"
            f" | Relay {'On' if relay_enabled else 'Off'}"
            f" | AI {ai_label}"
            f" | Buzzer {'On' if buzzer_enabled else 'Off'}"
            f" | LED {'On' if led_display_enabled else 'Off'}"
        ),
    }


def default_device_service_config(device_id=None, account=None):
    cloud_feed_enabled = True
    if account is not None:
        cloud_feed_enabled = int(account.get("cloud_feed_enabled", 1) or 0) == 1
    return serialize_device_service_config(
        device_id,
        {
            "device_setup_type": "source_only",
            "main_sensor_enabled": True,
            "master_upper_sensor_enabled": False,
            "slave_device_enabled": True,
            "slave_upper_sensor_enabled": True,
            "source_tank_monitoring_enabled": True,
            "municipal_sensor_enabled": False,
            "municipal_valve_enabled": False,
            "source_outlet_valve_enabled": False,
            "starter_contactor_sensor_enabled": False,
            "motor_current_sensor_enabled": False,
            "water_flow_sensor_enabled": False,
            "water_pressure_sensor_enabled": False,
            "turbidity_monitoring_enabled": False,
            "master_turbidity_enabled": False,
            "slave_turbidity_enabled": False,
            "relay_enabled": True,
            "ai_analysis_enabled": True,
            "auto_mode_enabled": False,
            "cloud_feed_mode": (
                DEVICE_SERVICE_CLOUD_FEED_FULL if cloud_feed_enabled else DEVICE_SERVICE_CLOUD_FEED_OFF
            ),
            "ota_enabled": False,
            "local_firmware_upload_enabled": True,
            "buzzer_enabled": True,
            "led_display_enabled": True,
            "android_sso_session_limit": DEFAULT_ANDROID_SSO_SESSION_LIMIT,
            "telemetry_service_state": "UNKNOWN",
            "command_service_state": "UNKNOWN",
            "relay_service_state": "UNKNOWN",
            "ota_service_state": "UNKNOWN",
            "local_firmware_upload_service_state": "UNKNOWN",
            "buzzer_service_state": "UNKNOWN",
            "led_display_service_state": "UNKNOWN",
            "lower_tank_service_state": "UNKNOWN",
            "slave_device_service_state": "UNKNOWN",
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


def snapshot_device_service_flag(snapshot, *keys):
    if not snapshot:
        return None
    for key in keys:
        value = str(snapshot.get(key) or "").strip().upper()
        if value in {"ON", "TRUE", "YES", "1", "ENABLED"}:
            return True
        if value in {"OFF", "FALSE", "NO", "0", "DISABLED"}:
            return False
    return None


def snapshot_device_auto_mode_enabled(snapshot):
    if not snapshot:
        return None

    direct_flag = snapshot_device_service_flag(snapshot, "auto_mode_enabled", "auto_control_enabled", "automation_enabled")
    if direct_flag is not None:
        return direct_flag

    mode = str(snapshot.get("mode") or "").strip().upper()
    if mode == "AUTO":
        return True
    if mode == "MANUAL":
        return False

    auto_status = str(snapshot.get("auto_status") or "").strip().lower()
    if "manual" in auto_status:
        return False
    if "auto" in auto_status:
        return True
    return None


def snapshot_device_service_config(snapshot, device_id=None, account=None, existing=None):
    if not snapshot_has_live_device_data(snapshot):
        return None
    resolved_device_id = resolve_service_config_device_id(device_id, snapshot=snapshot)
    if not resolved_device_id:
        return None

    base_payload = dict(existing or default_device_service_config(resolved_device_id, account=account))
    upper_sensor_source = str(
        snapshot.get("upper_sensor_source")
        or snapshot.get("upper_sensor_location")
        or base_payload.get("upper_sensor_source")
        or ""
    ).strip().lower()
    if upper_sensor_source in {"master", "slave"}:
        slave_upper_sensor_enabled = upper_sensor_source == "slave"
        base_payload["slave_device_enabled"] = slave_upper_sensor_enabled
        base_payload["slave_upper_sensor_enabled"] = slave_upper_sensor_enabled
        base_payload["master_upper_sensor_enabled"] = not slave_upper_sensor_enabled
        base_payload["main_sensor_enabled"] = True

    for snapshot_key, config_key in (
        ("lower_tank_service", "source_tank_monitoring_enabled"),
        ("relay_service", "relay_enabled"),
        ("buzzer_service", "buzzer_enabled"),
        ("led_display_service", "led_display_enabled"),
        ("ota_service", "ota_enabled"),
        ("local_firmware_upload_service", "local_firmware_upload_enabled"),
        ("municipal_valve_feature_enabled", "municipal_valve_enabled"),
        ("source_pump_fill_feature_enabled", "source_outlet_valve_enabled"),
        ("starter_contactor_sensor_enabled", "starter_contactor_sensor_enabled"),
        ("motor_current_sensor_enabled", "motor_current_sensor_enabled"),
        ("water_flow_sensor_enabled", "water_flow_sensor_enabled"),
        ("water_pressure_sensor_enabled", "water_pressure_sensor_enabled"),
    ):
        live_flag = snapshot_device_service_flag(snapshot, snapshot_key)
        if live_flag is not None:
            base_payload[config_key] = live_flag

    for snapshot_key, config_key in (
        ("tank_height_cm", "tank_height_cm"),
        ("tank_capacity_liters", "tank_capacity_liters"),
        ("tank_height_cm", "upper_tank_height_cm"),
        ("tank_capacity_liters", "upper_tank_capacity_liters"),
        ("lower_tank_height_cm", "lower_tank_height_cm"),
        ("source_tank_capacity_liters", "lower_tank_capacity_liters"),
        ("auto_start_pct", "auto_start_pct"),
        ("auto_stop_pct", "auto_stop_pct"),
    ):
        if snapshot_key in snapshot:
            normalized_value = normalize_optional_config_float(snapshot.get(snapshot_key))
            if normalized_value is not None:
                base_payload[config_key] = normalized_value

    peer_channel_value = snapshot.get("direct_peer_config_channel", snapshot.get("direct_peer_wifi_channel"))
    try:
        normalized_peer_channel = _coerce_optional_peer_channel_value(peer_channel_value)
    except ValueError:
        normalized_peer_channel = None
    if normalized_peer_channel is not None:
        base_payload["direct_peer_wifi_channel"] = normalized_peer_channel

    for snapshot_key, config_key in (
        ("telemetry_service", "telemetry_service_state"),
        ("command_service", "command_service_state"),
        ("relay_service", "relay_service_state"),
        ("ota_service", "ota_service_state"),
        ("local_firmware_upload_service", "local_firmware_upload_service_state"),
        ("buzzer_service", "buzzer_service_state"),
        ("led_display_service", "led_display_service_state"),
        ("lower_tank_service", "lower_tank_service_state"),
        ("slave_device_service", "slave_device_service_state"),
    ):
        if snapshot_key in snapshot:
            base_payload[config_key] = normalize_optional_service_state(snapshot.get(snapshot_key))

    live_auto_mode_enabled = snapshot_device_auto_mode_enabled(snapshot)
    if live_auto_mode_enabled is not None:
        base_payload["auto_mode_enabled"] = live_auto_mode_enabled

    return serialize_device_service_config(resolved_device_id, base_payload, account=account)


def resolve_device_service_config(device_id=None, account=None, snapshot=None):
    resolved_device_id = resolve_service_config_device_id(device_id, snapshot=snapshot)
    if not resolved_device_id:
        return default_device_service_config(device_id, account=account)
    return fetch_device_service_config(resolved_device_id, account=account, snapshot=snapshot)


def fetch_device_service_config(device_id, account=None, snapshot=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return default_device_service_config(device_id, account=account)
    resolved_account = account if account is not None else fetch_customer_account(normalized_device_id)
    with get_db() as db:
        row = db.execute(
            """
            SELECT device_id, device_setup_type, main_sensor_enabled, master_upper_sensor_enabled,
                   slave_device_enabled, slave_upper_sensor_enabled,
                   source_tank_monitoring_enabled, municipal_sensor_enabled, municipal_valve_enabled, source_outlet_valve_enabled,
                   starter_contactor_sensor_enabled, motor_current_sensor_enabled,
                   water_flow_sensor_enabled, water_pressure_sensor_enabled, turbidity_monitoring_enabled,
                   master_turbidity_enabled, slave_turbidity_enabled, relay_enabled, ai_analysis_enabled,
                   cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
                   buzzer_enabled, led_display_enabled, auto_mode_enabled, android_sso_session_limit,
                   tank_height_cm, tank_capacity_liters,
                   upper_tank_height_cm, upper_tank_capacity_liters,
                   lower_tank_height_cm, lower_tank_capacity_liters,
                   auto_start_pct, auto_stop_pct, direct_peer_wifi_channel,
                   telemetry_service_state, command_service_state, relay_service_state,
                   ota_service_state, local_firmware_upload_service_state,
                   buzzer_service_state, led_display_service_state,
                   lower_tank_service_state, slave_device_service_state,
                   created_at, updated_at
            FROM device_service_configs
            WHERE device_id = ?
            """,
            (normalized_device_id,),
        ).fetchone()
    stored_config = (
        serialize_device_service_config(normalized_device_id, dict(row), account=resolved_account)
        if row
        else default_device_service_config(normalized_device_id, account=resolved_account)
    )
    live_config = snapshot_device_service_config(
        snapshot,
        device_id=normalized_device_id,
        account=resolved_account,
        existing=stored_config,
    )
    return live_config or stored_config


def list_device_service_configs(device_ids=None, accounts_by_device=None, snapshots_by_device=None):
    normalized_device_ids = [
        item for item in (normalize_device_id(value) for value in (device_ids or [])) if item
    ]
    accounts_by_device = accounts_by_device or {}
    snapshots_by_device = snapshots_by_device or {}
    query = (
        """
        SELECT device_id, device_setup_type, main_sensor_enabled, master_upper_sensor_enabled,
               slave_device_enabled, slave_upper_sensor_enabled,
               source_tank_monitoring_enabled, municipal_sensor_enabled, municipal_valve_enabled, source_outlet_valve_enabled,
               starter_contactor_sensor_enabled, motor_current_sensor_enabled,
               water_flow_sensor_enabled, water_pressure_sensor_enabled, turbidity_monitoring_enabled,
               master_turbidity_enabled, slave_turbidity_enabled, relay_enabled, ai_analysis_enabled,
               cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
               buzzer_enabled, led_display_enabled, auto_mode_enabled, android_sso_session_limit,
               tank_height_cm, tank_capacity_liters,
               upper_tank_height_cm, upper_tank_capacity_liters,
               lower_tank_height_cm, lower_tank_capacity_liters,
               auto_start_pct, auto_stop_pct, direct_peer_wifi_channel,
               telemetry_service_state, command_service_state, relay_service_state,
               ota_service_state, local_firmware_upload_service_state,
               buzzer_service_state, led_display_service_state,
               lower_tank_service_state, slave_device_service_state,
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
            snapshot = snapshots_by_device.get(normalized_device_id)
            if snapshot:
                configs[normalized_device_id] = snapshot_device_service_config(
                    snapshot,
                    device_id=normalized_device_id,
                    account=accounts_by_device.get(normalized_device_id),
                    existing=configs[normalized_device_id],
                ) or configs[normalized_device_id]
    else:
        for normalized_device_id, snapshot in snapshots_by_device.items():
            if not snapshot:
                continue
            account = accounts_by_device.get(normalized_device_id)
            existing = configs.get(normalized_device_id) or default_device_service_config(
                normalized_device_id,
                account=account,
            )
            configs[normalized_device_id] = (
                snapshot_device_service_config(
                    snapshot,
                    device_id=normalized_device_id,
                    account=account,
                    existing=existing,
                )
                or existing
            )
    return configs


def upsert_device_service_config(
    device_id,
    device_setup_type=None,
    main_sensor_enabled=None,
    master_upper_sensor_enabled=None,
    slave_device_enabled=None,
    slave_upper_sensor_enabled=None,
    source_tank_monitoring_enabled=None,
    municipal_sensor_enabled=None,
    municipal_valve_enabled=None,
    source_outlet_valve_enabled=None,
    starter_contactor_sensor_enabled=None,
    motor_current_sensor_enabled=None,
    water_flow_sensor_enabled=None,
    water_pressure_sensor_enabled=None,
    turbidity_monitoring_enabled=None,
    master_turbidity_enabled=None,
    slave_turbidity_enabled=None,
    relay_enabled=None,
    ai_analysis_enabled=None,
    cloud_feed_mode=None,
    ota_enabled=None,
    local_firmware_upload_enabled=None,
    buzzer_enabled=None,
    led_display_enabled=None,
    auto_mode_enabled=None,
    android_sso_session_limit=None,
    tank_height_cm=None,
    tank_capacity_liters=None,
    upper_tank_height_cm=None,
    upper_tank_capacity_liters=None,
    lower_tank_height_cm=None,
    lower_tank_capacity_liters=None,
    auto_start_pct=None,
    auto_stop_pct=None,
    direct_peer_wifi_channel=None,
    telemetry_service_state=None,
    command_service_state=None,
    relay_service_state=None,
    ota_service_state=None,
    local_firmware_upload_service_state=None,
    buzzer_service_state=None,
    led_display_service_state=None,
    lower_tank_service_state=None,
    slave_device_service_state=None,
    local_web_password=None,
):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    account = fetch_customer_account(normalized_device_id)
    existing = fetch_device_service_config(normalized_device_id, account=account)
    resolved_device_setup_type = str(
        device_setup_type or existing.get("device_setup_type") or "custom"
    ).strip().lower()
    requested_slave_device_enabled = boolish_enabled(
        slave_device_enabled,
        default=existing.get("slave_device_enabled", True),
    )
    requested_slave_upper_sensor_enabled = boolish_enabled(
        slave_upper_sensor_enabled,
        default=existing.get("slave_upper_sensor_enabled", requested_slave_device_enabled),
    )
    resolved_slave_device_enabled = requested_slave_device_enabled and requested_slave_upper_sensor_enabled
    resolved_slave_upper_sensor_enabled = requested_slave_upper_sensor_enabled and resolved_slave_device_enabled
    resolved_master_upper_sensor_enabled = boolish_enabled(
        master_upper_sensor_enabled,
        default=existing.get("master_upper_sensor_enabled", not resolved_slave_device_enabled),
    )
    if not resolved_slave_device_enabled:
        resolved_master_upper_sensor_enabled = True
        resolved_slave_upper_sensor_enabled = False
    elif resolved_slave_upper_sensor_enabled:
        resolved_master_upper_sensor_enabled = False
    resolved_main_sensor_enabled = boolish_enabled(
        main_sensor_enabled,
        default=resolved_master_upper_sensor_enabled or resolved_slave_upper_sensor_enabled,
    )
    resolved_main_sensor_enabled = (
        resolved_main_sensor_enabled or resolved_master_upper_sensor_enabled or resolved_slave_upper_sensor_enabled
    )
    resolved_source_tank_monitoring_enabled = boolish_enabled(
        source_tank_monitoring_enabled,
        default=existing.get("source_tank_monitoring_enabled", True),
    )
    resolved_municipal_sensor_enabled = boolish_enabled(
        municipal_sensor_enabled,
        default=existing.get("municipal_sensor_enabled", False),
    )
    resolved_municipal_valve_enabled = boolish_enabled(
        municipal_valve_enabled,
        default=existing.get("municipal_valve_enabled", False),
    )
    resolved_source_outlet_valve_enabled = boolish_enabled(
        source_outlet_valve_enabled,
        default=existing.get("source_outlet_valve_enabled", False),
    )
    resolved_starter_contactor_sensor_enabled = boolish_enabled(
        starter_contactor_sensor_enabled,
        default=existing.get("starter_contactor_sensor_enabled", False),
    )
    resolved_motor_current_sensor_enabled = boolish_enabled(
        motor_current_sensor_enabled,
        default=existing.get("motor_current_sensor_enabled", False),
    )
    resolved_water_flow_sensor_enabled = boolish_enabled(
        water_flow_sensor_enabled,
        default=existing.get("water_flow_sensor_enabled", False),
    )
    resolved_water_pressure_sensor_enabled = boolish_enabled(
        water_pressure_sensor_enabled,
        default=existing.get("water_pressure_sensor_enabled", False),
    )
    # Flow and pressure are alternative municipal-availability inputs. Keep old
    # records/forms deterministic by preferring flow if both arrive enabled.
    if not resolved_municipal_sensor_enabled:
        resolved_water_flow_sensor_enabled = False
        resolved_water_pressure_sensor_enabled = False
    elif resolved_water_flow_sensor_enabled and resolved_water_pressure_sensor_enabled:
        resolved_water_pressure_sensor_enabled = False
    elif not resolved_water_flow_sensor_enabled and not resolved_water_pressure_sensor_enabled:
        resolved_water_flow_sensor_enabled = True
    resolved_turbidity_monitoring_enabled = boolish_enabled(
        turbidity_monitoring_enabled,
        default=existing.get("turbidity_monitoring_enabled", False),
    )
    resolved_master_turbidity_enabled = boolish_enabled(
        master_turbidity_enabled, default=existing.get("master_turbidity_enabled", resolved_turbidity_monitoring_enabled)
    )
    resolved_slave_turbidity_enabled = boolish_enabled(
        slave_turbidity_enabled, default=existing.get("slave_turbidity_enabled", resolved_turbidity_monitoring_enabled)
    )
    resolved_turbidity_monitoring_enabled = resolved_master_turbidity_enabled or resolved_slave_turbidity_enabled
    resolved_relay_enabled = boolish_enabled(
        relay_enabled,
        default=existing.get("relay_enabled", True),
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
        default=existing.get("local_firmware_upload_enabled", True),
    )
    resolved_buzzer_enabled = boolish_enabled(
        buzzer_enabled,
        default=existing.get("buzzer_enabled", True),
    )
    resolved_led_display_enabled = boolish_enabled(
        led_display_enabled,
        default=existing.get("led_display_enabled", True),
    )
    resolved_auto_mode_enabled = boolish_enabled(
        auto_mode_enabled,
        default=existing.get("auto_mode_enabled", False),
    )
    resolved_android_sso_session_limit = normalize_android_sso_session_limit(
        android_sso_session_limit,
        default=existing.get("android_sso_session_limit", DEFAULT_ANDROID_SSO_SESSION_LIMIT),
    )
    resolved_cloud_feed_mode = normalize_device_service_cloud_mode(
        cloud_feed_mode,
        default=existing.get("cloud_feed_mode", DEVICE_SERVICE_CLOUD_FEED_FULL),
    )
    resolved_tank_height_cm = normalize_optional_config_float(tank_height_cm)
    if resolved_tank_height_cm is None:
        resolved_tank_height_cm = normalize_optional_config_float(existing.get("tank_height_cm"))
    resolved_tank_capacity_liters = normalize_optional_config_float(tank_capacity_liters)
    if resolved_tank_capacity_liters is None:
        resolved_tank_capacity_liters = normalize_optional_config_float(existing.get("tank_capacity_liters"))
    resolved_upper_tank_height_cm = normalize_optional_config_float(upper_tank_height_cm)
    if resolved_upper_tank_height_cm is None:
        resolved_upper_tank_height_cm = normalize_optional_config_float(existing.get("upper_tank_height_cm"))
    if resolved_upper_tank_height_cm is None:
        resolved_upper_tank_height_cm = resolved_tank_height_cm
    resolved_upper_tank_capacity_liters = normalize_optional_config_float(upper_tank_capacity_liters)
    if resolved_upper_tank_capacity_liters is None:
        resolved_upper_tank_capacity_liters = normalize_optional_config_float(existing.get("upper_tank_capacity_liters"))
    if resolved_upper_tank_capacity_liters is None:
        resolved_upper_tank_capacity_liters = resolved_tank_capacity_liters
    resolved_lower_tank_height_cm = normalize_optional_config_float(lower_tank_height_cm)
    if resolved_lower_tank_height_cm is None:
        resolved_lower_tank_height_cm = normalize_optional_config_float(existing.get("lower_tank_height_cm"))
    resolved_lower_tank_capacity_liters = normalize_optional_config_float(lower_tank_capacity_liters)
    if resolved_lower_tank_capacity_liters is None:
        resolved_lower_tank_capacity_liters = normalize_optional_config_float(existing.get("lower_tank_capacity_liters"))
    resolved_auto_start_pct = normalize_optional_config_float(auto_start_pct)
    if resolved_auto_start_pct is None:
        resolved_auto_start_pct = normalize_optional_config_float(existing.get("auto_start_pct"))
    resolved_auto_stop_pct = normalize_optional_config_float(auto_stop_pct)
    if resolved_auto_stop_pct is None:
        resolved_auto_stop_pct = normalize_optional_config_float(existing.get("auto_stop_pct"))
    resolved_direct_peer_wifi_channel = _coerce_optional_peer_channel_value(direct_peer_wifi_channel)
    if resolved_direct_peer_wifi_channel is None:
        resolved_direct_peer_wifi_channel = _coerce_optional_peer_channel_value(existing.get("direct_peer_wifi_channel"))
    resolved_telemetry_service_state = normalize_optional_service_state(
        telemetry_service_state,
        default=existing.get("telemetry_service_state", "UNKNOWN"),
    )
    resolved_command_service_state = normalize_optional_service_state(
        command_service_state,
        default=existing.get("command_service_state", "UNKNOWN"),
    )
    resolved_relay_service_state = normalize_optional_service_state(
        relay_service_state,
        default=existing.get("relay_service_state", "UNKNOWN"),
    )
    resolved_ota_service_state = normalize_optional_service_state(
        ota_service_state,
        default=existing.get("ota_service_state", "UNKNOWN"),
    )
    resolved_local_firmware_upload_service_state = normalize_optional_service_state(
        local_firmware_upload_service_state,
        default=existing.get("local_firmware_upload_service_state", "UNKNOWN"),
    )
    resolved_buzzer_service_state = normalize_optional_service_state(
        buzzer_service_state,
        default=existing.get("buzzer_service_state", "UNKNOWN"),
    )
    resolved_led_display_service_state = normalize_optional_service_state(
        led_display_service_state,
        default=existing.get("led_display_service_state", "UNKNOWN"),
    )
    resolved_lower_tank_service_state = normalize_optional_service_state(
        lower_tank_service_state,
        default=existing.get("lower_tank_service_state", "UNKNOWN"),
    )
    resolved_slave_device_service_state = normalize_optional_service_state(
        slave_device_service_state,
        default=existing.get("slave_device_service_state", "UNKNOWN"),
    )

    with get_db() as db:
        db.execute(
            """
            INSERT INTO device_service_configs(
                device_id, device_setup_type, main_sensor_enabled, master_upper_sensor_enabled,
                slave_device_enabled, slave_upper_sensor_enabled,
                source_tank_monitoring_enabled, municipal_sensor_enabled, municipal_valve_enabled, source_outlet_valve_enabled,
                starter_contactor_sensor_enabled, motor_current_sensor_enabled,
                water_flow_sensor_enabled, water_pressure_sensor_enabled, turbidity_monitoring_enabled,
                master_turbidity_enabled, slave_turbidity_enabled, relay_enabled, ai_analysis_enabled,
                cloud_feed_mode, ota_enabled, local_firmware_upload_enabled,
                buzzer_enabled, led_display_enabled, auto_mode_enabled, android_sso_session_limit,
                tank_height_cm, tank_capacity_liters,
                upper_tank_height_cm, upper_tank_capacity_liters,
                lower_tank_height_cm, lower_tank_capacity_liters,
                auto_start_pct, auto_stop_pct, direct_peer_wifi_channel,
                telemetry_service_state, command_service_state, relay_service_state,
                ota_service_state, local_firmware_upload_service_state,
                buzzer_service_state, led_display_service_state,
                lower_tank_service_state, slave_device_service_state,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(device_id) DO UPDATE SET
                device_setup_type=excluded.device_setup_type,
                main_sensor_enabled=excluded.main_sensor_enabled,
                master_upper_sensor_enabled=excluded.master_upper_sensor_enabled,
                slave_device_enabled=excluded.slave_device_enabled,
                slave_upper_sensor_enabled=excluded.slave_upper_sensor_enabled,
                source_tank_monitoring_enabled=excluded.source_tank_monitoring_enabled,
                municipal_sensor_enabled=excluded.municipal_sensor_enabled,
                municipal_valve_enabled=excluded.municipal_valve_enabled,
                source_outlet_valve_enabled=excluded.source_outlet_valve_enabled,
                starter_contactor_sensor_enabled=excluded.starter_contactor_sensor_enabled,
                motor_current_sensor_enabled=excluded.motor_current_sensor_enabled,
                water_flow_sensor_enabled=excluded.water_flow_sensor_enabled,
                water_pressure_sensor_enabled=excluded.water_pressure_sensor_enabled,
                turbidity_monitoring_enabled=excluded.turbidity_monitoring_enabled,
                master_turbidity_enabled=excluded.master_turbidity_enabled,
                slave_turbidity_enabled=excluded.slave_turbidity_enabled,
                relay_enabled=excluded.relay_enabled,
                ai_analysis_enabled=excluded.ai_analysis_enabled,
                cloud_feed_mode=excluded.cloud_feed_mode,
                ota_enabled=excluded.ota_enabled,
                local_firmware_upload_enabled=excluded.local_firmware_upload_enabled,
                buzzer_enabled=excluded.buzzer_enabled,
                led_display_enabled=excluded.led_display_enabled,
                auto_mode_enabled=excluded.auto_mode_enabled,
                android_sso_session_limit=excluded.android_sso_session_limit,
                tank_height_cm=excluded.tank_height_cm,
                tank_capacity_liters=excluded.tank_capacity_liters,
                upper_tank_height_cm=excluded.upper_tank_height_cm,
                upper_tank_capacity_liters=excluded.upper_tank_capacity_liters,
                lower_tank_height_cm=excluded.lower_tank_height_cm,
                lower_tank_capacity_liters=excluded.lower_tank_capacity_liters,
                auto_start_pct=excluded.auto_start_pct,
                auto_stop_pct=excluded.auto_stop_pct,
                direct_peer_wifi_channel=excluded.direct_peer_wifi_channel,
                telemetry_service_state=excluded.telemetry_service_state,
                command_service_state=excluded.command_service_state,
                relay_service_state=excluded.relay_service_state,
                ota_service_state=excluded.ota_service_state,
                local_firmware_upload_service_state=excluded.local_firmware_upload_service_state,
                buzzer_service_state=excluded.buzzer_service_state,
                led_display_service_state=excluded.led_display_service_state,
                lower_tank_service_state=excluded.lower_tank_service_state,
                slave_device_service_state=excluded.slave_device_service_state,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                normalized_device_id,
                resolved_device_setup_type,
                1 if resolved_main_sensor_enabled else 0,
                1 if resolved_master_upper_sensor_enabled else 0,
                1 if resolved_slave_device_enabled else 0,
                1 if resolved_slave_upper_sensor_enabled else 0,
                1 if resolved_source_tank_monitoring_enabled else 0,
                1 if resolved_municipal_sensor_enabled else 0,
                1 if resolved_municipal_valve_enabled else 0,
                1 if resolved_source_outlet_valve_enabled else 0,
                1 if resolved_starter_contactor_sensor_enabled else 0,
                1 if resolved_motor_current_sensor_enabled else 0,
                1 if resolved_water_flow_sensor_enabled else 0,
                1 if resolved_water_pressure_sensor_enabled else 0,
                1 if resolved_turbidity_monitoring_enabled else 0,
                1 if resolved_master_turbidity_enabled else 0,
                1 if resolved_slave_turbidity_enabled else 0,
                1 if resolved_relay_enabled else 0,
                1 if resolved_ai_analysis_enabled else 0,
                resolved_cloud_feed_mode,
                1 if resolved_ota_enabled else 0,
                1 if resolved_local_firmware_upload_enabled else 0,
                1 if resolved_buzzer_enabled else 0,
                1 if resolved_led_display_enabled else 0,
                1 if resolved_auto_mode_enabled else 0,
                resolved_android_sso_session_limit,
                resolved_tank_height_cm,
                resolved_tank_capacity_liters,
                resolved_upper_tank_height_cm,
                resolved_upper_tank_capacity_liters,
                resolved_lower_tank_height_cm,
                resolved_lower_tank_capacity_liters,
                resolved_auto_start_pct,
                resolved_auto_stop_pct,
                resolved_direct_peer_wifi_channel,
                resolved_telemetry_service_state,
                resolved_command_service_state,
                resolved_relay_service_state,
                resolved_ota_service_state,
                resolved_local_firmware_upload_service_state,
                resolved_buzzer_service_state,
                resolved_led_display_service_state,
                resolved_lower_tank_service_state,
                resolved_slave_device_service_state,
            ),
        )

    persisted_local_web_password = str(local_web_password or "").strip()
    if persisted_local_web_password:
        save_device_local_web_password(normalized_device_id, persisted_local_web_password)
    elif not fetch_device_local_web_password(normalized_device_id, default_to_env=False):
        save_device_local_web_password(normalized_device_id, default_local_web_auth_password())

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
    slave_upper_sensor_enabled = bool(config.get("slave_upper_sensor_enabled", slave_device_enabled)) and slave_device_enabled
    master_upper_sensor_enabled = bool(config.get("master_upper_sensor_enabled", not slave_upper_sensor_enabled))
    if not slave_upper_sensor_enabled:
        master_upper_sensor_enabled = True
    source_tank_enabled = bool(config.get("source_tank_monitoring_enabled"))
    municipal_sensor_enabled = bool(config.get("municipal_sensor_enabled", False))
    municipal_valve_enabled = bool(config.get("municipal_valve_enabled", False))
    source_outlet_valve_enabled = bool(config.get("source_outlet_valve_enabled", False))
    turbidity_monitoring_enabled = bool(config.get("turbidity_monitoring_enabled", False))
    master_turbidity_enabled = bool(config.get("master_turbidity_enabled", turbidity_monitoring_enabled))
    slave_turbidity_enabled = bool(config.get("slave_turbidity_enabled", turbidity_monitoring_enabled))
    relay_enabled = bool(config.get("relay_enabled", True))
    auto_mode_enabled = bool(config.get("auto_mode_enabled", False))
    water_flow_sensor_enabled = municipal_sensor_enabled and bool(config.get("water_flow_sensor_enabled"))
    water_pressure_sensor_enabled = municipal_sensor_enabled and bool(config.get("water_pressure_sensor_enabled")) and not water_flow_sensor_enabled
    if municipal_sensor_enabled and not water_flow_sensor_enabled and not water_pressure_sensor_enabled:
        water_flow_sensor_enabled = True
    return "SERVICECFG11:{master_upper}:{slave_upper}:{source}:{relay}:{buzzer}:{led}:{ota}:{upload}:{auto_mode}:{municipal}:{master_turbidity}:{slave_turbidity}:{municipal_valve}:{source_outlet_valve}:{starter_aux}:{motor_current}:{water_flow}:{water_pressure}".format(
        master_upper=1 if master_upper_sensor_enabled else 0,
        slave_upper=1 if slave_upper_sensor_enabled else 0,
        source=1 if source_tank_enabled else 0,
        relay=1 if relay_enabled else 0,
        buzzer=1 if bool(config.get("buzzer_enabled")) else 0,
        led=1 if bool(config.get("led_display_enabled")) else 0,
        ota=0,
        upload=1 if bool(config.get("local_firmware_upload_enabled", True)) else 0,
        auto_mode=1 if auto_mode_enabled else 0,
        municipal=1 if municipal_sensor_enabled else 0,
        master_turbidity=1 if master_turbidity_enabled else 0,
        slave_turbidity=1 if slave_turbidity_enabled else 0,
        municipal_valve=1 if municipal_valve_enabled else 0,
        source_outlet_valve=1 if source_outlet_valve_enabled else 0,
        starter_aux=1 if bool(config.get("starter_contactor_sensor_enabled")) else 0,
        motor_current=1 if bool(config.get("motor_current_sensor_enabled")) else 0,
        water_flow=1 if water_flow_sensor_enabled else 0,
        water_pressure=1 if water_pressure_sensor_enabled else 0,
    )


def device_automation_settings_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    return f"{DEVICE_AUTOMATION_SETTINGS_PREFIX}{normalized_device_id}" if normalized_device_id else None


def build_device_automation_settings(device_id, auto_start_pct, auto_stop_pct, source, updated_at=None):
    return {
        "device_id": normalize_device_id(device_id),
        "auto_start_pct": round(float(auto_start_pct), 1),
        "auto_stop_pct": round(float(auto_stop_pct), 1),
        "source": str(source or "default").strip() or "default",
        "updated_at": updated_at,
    }


def default_device_automation_settings(device_id=None):
    return build_device_automation_settings(
        device_id,
        DEFAULT_DEVICE_AUTO_START_PCT,
        DEFAULT_DEVICE_AUTO_STOP_PCT,
        "default",
    )


def _coerce_optional_threshold_value(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        raise ValueError("Tank thresholds must be numeric percentages.")


def _resolve_threshold_value(payload, *keys):
    for key in keys:
        if key in (payload or {}):
            return payload.get(key)
    return None


def _coerce_optional_peer_channel_value(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        resolved = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("Peer channel must be a number between 1 and 13.")
    if resolved < 1 or resolved > 13:
        raise ValueError("Peer channel must be between 1 and 13.")
    return resolved


def telemetry_config_float_seed(saved_config, config_key, reported_value):
    if normalize_optional_config_float((saved_config or {}).get(config_key)) is not None:
        return None
    return reported_value


def telemetry_config_peer_channel_seed(saved_config, reported_value):
    try:
        if _coerce_optional_peer_channel_value((saved_config or {}).get("direct_peer_wifi_channel")) is not None:
            return None
    except ValueError:
        pass
    return reported_value


def snapshot_device_automation_settings(snapshot, device_id=None):
    if not snapshot:
        return None
    auto_start_pct = _coerce_optional_threshold_value(
        _resolve_threshold_value(snapshot, "auto_start_pct", "auto_start_level_pct", "lower_threshold_pct")
    )
    auto_stop_pct = _coerce_optional_threshold_value(
        _resolve_threshold_value(snapshot, "auto_stop_pct", "auto_stop_level_pct", "upper_threshold_pct")
    )
    if auto_start_pct is None or auto_stop_pct is None:
        return None
    if auto_start_pct < 0 or auto_start_pct > 95 or auto_stop_pct < 5 or auto_stop_pct > 100 or auto_start_pct >= auto_stop_pct:
        return None
    return build_device_automation_settings(
        device_id or snapshot.get("device_id"),
        auto_start_pct,
        auto_stop_pct,
        "live_device",
    )


def save_device_automation_settings(device_id, auto_start_pct, auto_stop_pct, source="cloud"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")
    settings = build_device_automation_settings(
        normalized_device_id,
        auto_start_pct,
        auto_stop_pct,
        source,
        now_utc().strftime(TIMESTAMP_FORMAT),
    )
    upsert_device_service_config(
        normalized_device_id,
        auto_start_pct=settings["auto_start_pct"],
        auto_stop_pct=settings["auto_stop_pct"],
    )
    setting_key = device_automation_settings_key(normalized_device_id)
    set_app_setting(setting_key, json.dumps(settings, separators=(",", ":")))
    return settings


def fetch_device_automation_settings(device_id, snapshot=None):
    normalized_device_id = normalize_device_id(device_id)
    stored_service_config = fetch_device_service_config(normalized_device_id) if normalized_device_id else {}
    stored_auto_start_pct = _coerce_optional_threshold_value((stored_service_config or {}).get("auto_start_pct"))
    stored_auto_stop_pct = _coerce_optional_threshold_value((stored_service_config or {}).get("auto_stop_pct"))
    if stored_auto_start_pct is not None and stored_auto_stop_pct is not None:
        return build_device_automation_settings(
            normalized_device_id,
            stored_auto_start_pct,
            stored_auto_stop_pct,
            "device_service_config",
            (stored_service_config or {}).get("updated_at"),
        )

    setting_key = device_automation_settings_key(normalized_device_id)
    raw_value = get_app_setting(setting_key, "") if setting_key else ""
    if raw_value:
        try:
            payload = json.loads(raw_value)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            auto_start_pct = _coerce_optional_threshold_value(payload.get("auto_start_pct"))
            auto_stop_pct = _coerce_optional_threshold_value(payload.get("auto_stop_pct"))
            if auto_start_pct is not None and auto_stop_pct is not None:
                upsert_device_service_config(
                    normalized_device_id,
                    auto_start_pct=auto_start_pct,
                    auto_stop_pct=auto_stop_pct,
                )
                return build_device_automation_settings(
                    normalized_device_id,
                    auto_start_pct,
                    auto_stop_pct,
                    payload.get("source") or "saved_cloud",
                    payload.get("updated_at"),
                )

    live_settings = snapshot_device_automation_settings(snapshot, device_id=normalized_device_id)
    if live_settings:
        seed_device_id = normalized_device_id or live_settings.get("device_id")
        if not seed_device_id:
            return live_settings
        return save_device_automation_settings(
            seed_device_id,
            live_settings["auto_start_pct"],
            live_settings["auto_stop_pct"],
            source="telemetry_seed",
        )

    return default_device_automation_settings(normalized_device_id)


def build_current_saved_config(device_id, account=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return {
            "device_id": "",
            "configuration_source": "db_upsert",
            "service_config": default_device_service_config(device_id, account=account),
            "automation_settings": default_device_automation_settings(device_id),
        }

    resolved_account = account if account is not None else fetch_customer_account(normalized_device_id)
    saved_service_config = fetch_device_service_config(normalized_device_id, account=resolved_account, snapshot=None)
    saved_automation_settings = fetch_device_automation_settings(normalized_device_id, snapshot=None)
    updated_candidates = [
        str(saved_service_config.get("updated_at") or "").strip(),
        str(saved_automation_settings.get("updated_at") or "").strip(),
    ]
    updated_at = next((value for value in updated_candidates if value), "")
    auto_start_pct = _coerce_optional_threshold_value(
        saved_automation_settings.get("auto_start_pct") if saved_automation_settings else None
    )
    if auto_start_pct is None:
        auto_start_pct = _coerce_optional_threshold_value(saved_service_config.get("auto_start_pct"))
    auto_stop_pct = _coerce_optional_threshold_value(
        saved_automation_settings.get("auto_stop_pct") if saved_automation_settings else None
    )
    if auto_stop_pct is None:
        auto_stop_pct = _coerce_optional_threshold_value(saved_service_config.get("auto_stop_pct"))
    return {
        "device_id": normalized_device_id,
        "configuration_source": "db_upsert",
        "updated_at": updated_at,
        "auto_mode_enabled": saved_service_config.get("auto_mode_enabled"),
        "auto_mode_label": saved_service_config.get("auto_mode_label"),
        "tank_height_cm": saved_service_config.get("tank_height_cm"),
        "tank_capacity_liters": saved_service_config.get("tank_capacity_liters"),
        "upper_tank_height_cm": saved_service_config.get("upper_tank_height_cm"),
        "upper_tank_capacity_liters": saved_service_config.get("upper_tank_capacity_liters"),
        "lower_tank_height_cm": saved_service_config.get("lower_tank_height_cm"),
        "lower_tank_capacity_liters": saved_service_config.get("lower_tank_capacity_liters"),
        "auto_start_pct": auto_start_pct,
        "auto_stop_pct": auto_stop_pct,
        "direct_peer_wifi_channel": saved_service_config.get("direct_peer_wifi_channel"),
        "telemetry_service_state": saved_service_config.get("telemetry_service_state"),
        "command_service_state": saved_service_config.get("command_service_state"),
        "relay_service_state": saved_service_config.get("relay_service_state"),
        "ota_service_state": saved_service_config.get("ota_service_state"),
        "local_firmware_upload_service_state": saved_service_config.get("local_firmware_upload_service_state"),
        "buzzer_service_state": saved_service_config.get("buzzer_service_state"),
        "led_display_service_state": saved_service_config.get("led_display_service_state"),
        "lower_tank_service_state": saved_service_config.get("lower_tank_service_state"),
        "slave_device_service_state": saved_service_config.get("slave_device_service_state"),
        "service_config": saved_service_config,
        "automation_settings": saved_automation_settings,
    }


def upsert_device_automation_settings(device_id, auto_start_pct=None, auto_stop_pct=None, snapshot=None, source="cloud"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    existing = fetch_device_automation_settings(normalized_device_id, snapshot=snapshot)
    resolved_start = _coerce_optional_threshold_value(auto_start_pct)
    resolved_stop = _coerce_optional_threshold_value(auto_stop_pct)
    if resolved_start is None:
        resolved_start = safe_float(existing.get("auto_start_pct"), DEFAULT_DEVICE_AUTO_START_PCT)
    if resolved_stop is None:
        resolved_stop = safe_float(existing.get("auto_stop_pct"), DEFAULT_DEVICE_AUTO_STOP_PCT)

    if resolved_start < 0 or resolved_start > 95:
        raise ValueError("Start level must be between 0 and 95%.")
    if resolved_stop < 5 or resolved_stop > 100:
        raise ValueError("Stop level must be between 5 and 100%.")
    if resolved_start >= resolved_stop:
        raise ValueError("Start level must stay below stop level.")

    return save_device_automation_settings(
        normalized_device_id,
        resolved_start,
        resolved_stop,
        source=source,
    )


def build_device_automation_command(settings):
    auto_start_pct = safe_float((settings or {}).get("auto_start_pct"), DEFAULT_DEVICE_AUTO_START_PCT)
    auto_stop_pct = safe_float((settings or {}).get("auto_stop_pct"), DEFAULT_DEVICE_AUTO_STOP_PCT)
    return "THRESHOLDS:{start}:{stop}".format(
        start=f"{auto_start_pct:g}",
        stop=f"{auto_stop_pct:g}",
    )


def build_device_peer_channel_command(channel):
    resolved_channel = _coerce_optional_peer_channel_value(channel)
    if resolved_channel is None:
        raise ValueError("Peer channel is required.")
    return f"PEER_CHANNEL:{resolved_channel}"


def runtime_sync_float_matches(live_value, saved_value, tolerance=0.11):
    saved_number = normalize_optional_config_float(saved_value)
    if saved_number is None:
        return True
    live_number = normalize_optional_config_float(live_value)
    if live_number is None:
        return False
    return abs(live_number - saved_number) <= tolerance


def runtime_sync_service_enabled(snapshot, key):
    return snapshot_device_service_flag(snapshot, key) is True


def build_runtime_sync_command(device_id, snapshot=None, account=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    live_snapshot = snapshot or fetch_device_snapshot(normalized_device_id)
    if not snapshot_is_fresh_enough_for_runtime_sync(live_snapshot):
        return None

    saved_config = build_current_saved_config(normalized_device_id, account=account)
    service_config = saved_config.get("service_config") or {}
    automation_settings = saved_config.get("automation_settings") or {}

    desired_slave_enabled = bool(service_config.get("slave_device_enabled", True)) and bool(
        service_config.get("slave_upper_sensor_enabled", service_config.get("slave_device_enabled", True))
    )
    desired_master_upper_enabled = bool(service_config.get("master_upper_sensor_enabled", not desired_slave_enabled))
    if not desired_slave_enabled:
        desired_master_upper_enabled = True

    live_upper_source = str(live_snapshot.get("upper_sensor_source") or "").strip().lower()
    live_slave_enabled = live_upper_source == "slave"
    service_drift = any(
        (
            live_slave_enabled != desired_slave_enabled,
            runtime_sync_service_enabled(live_snapshot, "lower_tank_service")
            != bool(service_config.get("source_tank_monitoring_enabled")),
            runtime_sync_service_enabled(live_snapshot, "relay_service")
            != bool(service_config.get("relay_enabled", True)),
            runtime_sync_service_enabled(live_snapshot, "buzzer_service")
            != bool(service_config.get("buzzer_enabled", True)),
            runtime_sync_service_enabled(live_snapshot, "led_display_service")
            != bool(service_config.get("led_display_enabled", True)),
            runtime_sync_service_enabled(live_snapshot, "local_firmware_upload_service")
            != bool(service_config.get("local_firmware_upload_enabled", True)),
            runtime_sync_service_enabled(live_snapshot, "municipal_feature_enabled")
            != bool(service_config.get("municipal_sensor_enabled", False)),
            runtime_sync_service_enabled(live_snapshot, "municipal_valve_feature_enabled")
            != bool(service_config.get("municipal_valve_enabled", False)),
            runtime_sync_service_enabled(live_snapshot, "source_pump_fill_feature_enabled")
            != bool(service_config.get("source_outlet_valve_enabled", False)),
            runtime_sync_service_enabled(live_snapshot, "master_turbidity_enabled")
            != bool(service_config.get("master_turbidity_enabled", False)),
            runtime_sync_service_enabled(live_snapshot, "slave_turbidity_enabled")
            != bool(service_config.get("slave_turbidity_enabled", False)),
        )
    )
    if desired_master_upper_enabled and live_upper_source != "master" and not desired_slave_enabled:
        service_drift = True
    if service_drift:
        return {"command": build_device_service_command(service_config), "reason": "service_config"}

    saved_start = _coerce_optional_threshold_value(
        automation_settings.get("auto_start_pct") if automation_settings else saved_config.get("auto_start_pct")
    )
    saved_stop = _coerce_optional_threshold_value(
        automation_settings.get("auto_stop_pct") if automation_settings else saved_config.get("auto_stop_pct")
    )
    live_start = _coerce_optional_threshold_value(
        live_snapshot.get("auto_start_pct", live_snapshot.get("auto_start_level_pct"))
    )
    live_stop = _coerce_optional_threshold_value(
        live_snapshot.get("auto_stop_pct", live_snapshot.get("auto_stop_level_pct"))
    )
    if (
        saved_start is not None
        and saved_stop is not None
        and (
            live_start is None
            or live_stop is None
            or abs(live_start - saved_start) > 0.11
            or abs(live_stop - saved_stop) > 0.11
        )
    ):
        return {
            "command": build_device_automation_command(
                {"auto_start_pct": saved_start, "auto_stop_pct": saved_stop}
            ),
            "reason": "automation_thresholds",
        }

    desired_upper_height = saved_config.get("tank_height_cm")
    desired_upper_capacity = saved_config.get("tank_capacity_liters")
    live_upper_height = live_snapshot.get("tank_height_cm")
    live_upper_capacity = live_snapshot.get("tank_capacity_liters", live_snapshot.get("capacity_liters"))
    if (
        normalize_optional_config_float(desired_upper_height) is not None
        and normalize_optional_config_float(desired_upper_capacity) is not None
        and (
            not runtime_sync_float_matches(live_upper_height, desired_upper_height)
            or not runtime_sync_float_matches(live_upper_capacity, desired_upper_capacity)
        )
    ):
        return {
            "command": "CONFIG_UPPER:{height}:{capacity}".format(
                height=f"{normalize_optional_config_float(desired_upper_height):.1f}",
                capacity=f"{normalize_optional_config_float(desired_upper_capacity):.1f}",
            ),
            "reason": "upper_tank_setup",
        }

    desired_lower_height = saved_config.get("lower_tank_height_cm")
    desired_lower_capacity = saved_config.get("lower_tank_capacity_liters")
    live_lower_height = live_snapshot.get("lower_tank_height_cm")
    live_lower_capacity = live_snapshot.get("lower_tank_capacity_liters", live_snapshot.get("source_tank_capacity_liters"))
    if (
        bool(service_config.get("source_tank_monitoring_enabled"))
        and normalize_optional_config_float(desired_lower_height) is not None
        and normalize_optional_config_float(desired_lower_capacity) is not None
        and (
            not runtime_sync_float_matches(live_lower_height, desired_lower_height)
            or not runtime_sync_float_matches(live_lower_capacity, desired_lower_capacity)
        )
    ):
        return {
            "command": "CONFIG_LOWER:{height}:{capacity}".format(
                height=f"{normalize_optional_config_float(desired_lower_height):.1f}",
                capacity=f"{normalize_optional_config_float(desired_lower_capacity):.1f}",
            ),
            "reason": "lower_tank_setup",
        }

    desired_peer_channel = _coerce_optional_peer_channel_value(saved_config.get("direct_peer_wifi_channel"))
    live_peer_channel = _coerce_optional_peer_channel_value(
        live_snapshot.get("direct_peer_config_channel", live_snapshot.get("direct_peer_wifi_channel"))
    )
    if desired_peer_channel is not None and desired_peer_channel != live_peer_channel:
        return {
            "command": build_device_peer_channel_command(desired_peer_channel),
            "reason": "peer_channel",
        }

    return None


def paired_slave_device_id(master_device_id):
    normalized_device_id = normalize_device_id(master_device_id)
    parts = normalized_device_id.split("-")
    if len(parts) == 5 and parts[0] == "swt" and parts[1] == "000":
        parts[1] = "100"
        return "-".join(parts)
    return normalized_device_id


def paired_activity_device_ids(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []
    ids = [normalized_device_id]
    slave_device_id = paired_slave_device_id(normalized_device_id)
    if slave_device_id and slave_device_id not in ids:
        ids.append(slave_device_id)
    return ids


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
    service_config = fetch_device_service_config(
        normalized_username,
        account=customer,
        snapshot=load_dashboard_snapshot(normalized_username),
    )
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
        "android_sso_session_limit": service_config.get("android_sso_session_limit", DEFAULT_ANDROID_SSO_SESSION_LIMIT),
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
    platform_session_id = register_active_platform_session(
        SESSION_PLATFORM_ANDROID,
        user.get("role"),
        username=user.get("username"),
        device_id=user.get("device_id"),
    )
    payload = {
        "role": user.get("role"),
        "username": user.get("username"),
        "device_id": normalize_device_id(user.get("device_id")),
        "auth_marker": auth_marker,
        "platform_session_id": platform_session_id,
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
    platform_session_id = str(payload.get("platform_session_id") or "").strip()

    if not token_auth_marker:
        g.mobile_user = None
        return None
    if not platform_session_id:
        g.mobile_auth_error = "session_replaced"
        g.mobile_user = None
        return None

    if role == "admin":
        g.mobile_user = None
        return None
    if role == "customer" and device_id:
        customer = fetch_customer_account(device_id)
        if not customer or int(customer.get("active", 0)) != 1:
            g.mobile_user = None
            return None
        current_auth_marker = current_auth_marker_for_identity("customer", device_id=device_id, account=customer)
        if not current_auth_marker or not secrets.compare_digest(token_auth_marker, current_auth_marker):
            g.mobile_user = None
            return None
        if not active_platform_session_matches(
            SESSION_PLATFORM_ANDROID,
            "customer",
            username=device_id,
            device_id=device_id,
            session_id=platform_session_id,
        ):
            register_active_platform_session(
                SESSION_PLATFORM_ANDROID,
                "customer",
                username=device_id,
                device_id=device_id,
                session_id=platform_session_id,
            )
        service_config = fetch_device_service_config(
            device_id,
            account=customer,
            snapshot=load_dashboard_snapshot(device_id),
        )
        user = {
            "role": "customer",
            "username": device_id,
            "device_id": device_id,
            "display_name": customer.get("display_name") or device_id,
            "platform_session_id": platform_session_id,
            "slave_device_enabled": service_config.get("slave_device_enabled", True),
            "source_tank_monitoring_enabled": service_config.get("source_tank_monitoring_enabled", True),
            "cloud_feed_enabled": service_config.get("cloud_feed_enabled", True),
            "cloud_feed_mode": service_config.get("cloud_feed_mode"),
            "ai_analysis_enabled": service_config.get("effective_ai_analysis_enabled", True),
            "android_sso_session_limit": service_config.get("android_sso_session_limit", DEFAULT_ANDROID_SSO_SESSION_LIMIT),
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
            if getattr(g, "mobile_auth_error", None) == "session_replaced":
                return jsonify({"error": "This Android session was signed out because the account was used on another Android device.", "code": "session_replaced"}), 409
            return jsonify({"error": "authentication required"}), 401
        return view(*args, **kwargs)

    return wrapped


def current_mobile_scope_device_id(requested_device_id=None):
    mobile_user = resolve_mobile_user()
    normalized_requested = normalize_device_id(requested_device_id)
    if not mobile_user or mobile_user.get("role") != "customer":
        abort(401, description="Authenticated customer account required.")
    scoped_device_id = normalize_device_id(mobile_user.get("device_id"))
    if not scoped_device_id:
        abort(403, description="The logged-in account has no assigned device.")
    if normalized_requested and normalized_requested != scoped_device_id:
        abort(403, description="The requested device is not assigned to the logged-in account.")
    return scoped_device_id


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
    row = db.execute(
        f"""
        SELECT
            COALESCE(SUM(CASE WHEN motor='ON' AND COALESCE(prev_motor,'OFF')!='ON' THEN 1 ELSE 0 END), 0) AS motor_cycles,
            COALESCE(SUM(CASE WHEN pipe_leak='YES' AND COALESCE(prev_pipe_leak,'NO')!='YES' THEN 1 ELSE 0 END), 0) AS leak_events
        FROM (
            SELECT motor,
                   pipe_leak,
                   LAG(motor) OVER (ORDER BY id) AS prev_motor,
                   LAG(pipe_leak) OVER (ORDER BY id) AS prev_pipe_leak
            FROM (
                SELECT id, motor, pipe_leak
                FROM tank_data
                WHERE {clause}
                ORDER BY created_at DESC, id DESC
                LIMIT {limit}
            ) recent_rows
            ORDER BY id
        ) transitions
        """,
        tuple(params),
    ).fetchone()

    return int(row["motor_cycles"] or 0), int(row["leak_events"] or 0)


def enrich_snapshot(data, motor_cycles=0, leak_events=0):
    created_at = parse_timestamp(data.get("created_at"))
    seconds_since_sync = None
    if created_at:
        seconds_since_sync = int((now_utc() - created_at).total_seconds())

    raw_level = safe_float(data.get("level"), None)
    level_valid = raw_level is not None and 0.0 <= raw_level <= 100.0
    level = raw_level if level_valid else 0.0
    capacity_liters = safe_float(data.get("tank_capacity_liters"), TANK_CAPACITY_LITERS)
    if capacity_liters <= 0:
        capacity_liters = TANK_CAPACITY_LITERS
    liters = round((level / 100) * capacity_liters, 1)

    data["level_valid"] = level_valid
    data["level"] = round(level, 2) if level_valid else None
    mode = str(data.get("mode", "AUTO")).upper()
    data["mode"] = mode if mode in {"AUTO", "MANUAL"} else "AUTO"
    stored_simulator_state = load_device_simulator_state(data.get("device_id"))
    stored_simulator_source = str((stored_simulator_state or {}).get("source") or "").strip()
    stored_simulator_enabled = (
        bool(stored_simulator_state.get("enabled"))
        if stored_simulator_state and stored_simulator_source != "admin_command"
        else None
    )
    data["simulator"] = (
        "ON" if stored_simulator_enabled else "OFF"
    ) if stored_simulator_enabled is not None else str(data.get("simulator") or "OFF").strip().upper() or "OFF"
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
    sensor_distance_raw = data.get("sensor_distance_cm")
    sensor_distance_cm = (
        round(safe_float(sensor_distance_raw, 0), 1)
        if sensor_distance_raw not in (None, "", "null") else None
    )
    data["sensor_distance_cm"] = sensor_distance_cm
    if sensor_distance_cm is not None and data["tank_height_cm"] > 0:
        data["water_depth_cm"] = round(max(0.0, min(data["tank_height_cm"], data["tank_height_cm"] - sensor_distance_cm)), 1)
        data["water_depth_label"] = f"{data['water_depth_cm']:.1f} cm"
    elif data["tank_height_cm"] > 0:
        data["water_depth_cm"] = round(max(0.0, min(data["tank_height_cm"], (level / 100.0) * data["tank_height_cm"])), 1)
        data["water_depth_label"] = f"{data['water_depth_cm']:.1f} cm"
    else:
        data["water_depth_cm"] = None
        data["water_depth_label"] = "--"
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
    lower_height_cm = safe_float(data.get("lower_tank_height_cm") or data.get("source_tank_height_cm"), 0)
    if data["lower_sensor_distance_cm"] is not None and lower_height_cm > 0:
        data["lower_water_depth_cm"] = round(max(0.0, min(lower_height_cm, lower_height_cm - data["lower_sensor_distance_cm"])), 1)
        data["lower_water_depth_label"] = f"{data['lower_water_depth_cm']:.1f} cm"
    elif lower_level is not None and lower_height_cm > 0:
        data["lower_water_depth_cm"] = round(max(0.0, min(lower_height_cm, (lower_level / 100.0) * lower_height_cm)), 1)
        data["lower_water_depth_label"] = f"{data['lower_water_depth_cm']:.1f} cm"
    else:
        data["lower_water_depth_cm"] = None
        data["lower_water_depth_label"] = "--"
    lower_capacity_value = (
        data.get("source_tank_capacity_liters") or data.get("lower_tank_capacity_liters")
    )
    # tank_data stores level telemetry, while source capacity is persisted in
    # device_service_configs.  Without this fallback every fetched dashboard
    # snapshot falls back to 2000 L after CONFIG_LOWER, even though firmware
    # correctly reports and persists the new capacity.
    if lower_capacity_value in (None, "", "null"):
        try:
            saved_tank_config = fetch_device_service_config(data.get("device_id"), snapshot=None) or {}
            lower_capacity_value = saved_tank_config.get("lower_tank_capacity_liters")
        except Exception as exc:
            logger.debug(
                "Could not load saved source capacity for %s: %s",
                normalize_device_id(data.get("device_id")),
                exc,
            )
    lower_capacity_liters = safe_float(lower_capacity_value, 2000.0)
    if lower_capacity_liters <= 0:
        lower_capacity_liters = capacity_liters
    data["source_tank_capacity_liters"] = round(lower_capacity_liters, 1)
    data["lower_tank_capacity_liters"] = round(lower_capacity_liters, 1)
    data["upper_tank_count"] = max(1, int(safe_float(data.get("upper_tank_count"), 1)))
    data["source_tank_count"] = max(1, int(safe_float(data.get("source_tank_count"), 1)))
    data["inlet_valve_route"] = str(
        data.get("inlet_valve_route") or data.get("municipal_valve_route") or "source"
    ).strip().lower()
    data["municipal_valve_route"] = data["inlet_valve_route"]
    data["inlet_valve_state"] = str(
        data.get("inlet_valve_state") or data.get("municipal_valve_state") or "unknown"
    ).strip().lower()
    data["municipal_detection_mode"] = str(
        data.get("municipal_detection_mode") or
        ("sensor" if data.get("municipal_sensor_enabled") else "upper_level_rise")
    ).strip().lower()
    if data["lower_tank_level"] is not None:
        lower_liters = round((data["lower_tank_level"] / 100) * lower_capacity_liters, 1)
        data["lower_water_available_label"] = f"{lower_liters:.1f} L / {lower_capacity_liters:.1f} L"
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
    data["direct_peer_remote_mac"] = str(data.get("direct_peer_remote_mac") or "").strip()
    data["last_ping_target"] = str(data.get("last_ping_target") or "").strip().lower()
    data["last_ping_status"] = str(data.get("last_ping_status") or "").strip().lower()
    for key in (
        "direct_peer_config_channel",
        "direct_peer_wifi_channel",
        "direct_peer_last_packet_age_s",
        "direct_peer_last_packet_bytes",
        "direct_peer_last_pong_age_s",
        "direct_peer_last_pong_nonce",
        "direct_peer_sync_channel",
        "direct_peer_sync_last_ok_age_s",
        "last_ping_response_ms",
        "last_ping_age_s",
        "last_ping_nonce",
    ):
        try:
            data[key] = int(data[key]) if data.get(key) not in (None, "", "null") else None
        except (TypeError, ValueError):
            data[key] = None
    data["direct_peer_sync_pending"] = bool_flag(data.get("direct_peer_sync_pending"))
    apply_source_tank_aliases(data, include_aliases=True)
    data["dry_run_active"] = "YES" if effective_dry_run_active(data) else "NO"
    data["pump_failure_active"] = "YES" if effective_pump_failure_active(data) else "NO"
    data["uptime_label"] = format_compact_uptime(data.get("uptime_s"))
    free_heap = data.get("free_heap")
    try:
        free_heap_value = int(free_heap) if free_heap is not None else None
    except (TypeError, ValueError):
        free_heap_value = None
    data["free_heap"] = free_heap_value
    data["free_heap_label"] = f"{free_heap_value} B" if free_heap_value is not None else "--"
    slave_free_heap = data.get("slave_free_heap")
    try:
        slave_free_heap_value = int(slave_free_heap) if slave_free_heap is not None else None
    except (TypeError, ValueError):
        slave_free_heap_value = None
    if slave_free_heap_value is not None and slave_free_heap_value <= 0:
        slave_free_heap_value = None
    data["slave_free_heap"] = slave_free_heap_value
    data["slave_free_heap_label"] = f"{slave_free_heap_value} B" if slave_free_heap_value is not None else "--"
    for key in ("cpu_utilization_pct", "slave_cpu_utilization_pct"):
        try:
            cpu_value = float(data.get(key)) if data.get(key) is not None else None
        except (TypeError, ValueError):
            cpu_value = None
        if cpu_value is not None:
            if cpu_value < 0:
                cpu_value = None
            else:
                cpu_value = max(0.0, min(100.0, cpu_value))
        data[key] = round(cpu_value, 1) if cpu_value is not None else None
        data[f"{key}_label"] = f"{data[key]:.1f}%" if data[key] is not None else "--"
    try:
        data["slave_uptime_s"] = int(data["slave_uptime_s"]) if data.get("slave_uptime_s") not in (None, "", "null") else None
    except (TypeError, ValueError):
        data["slave_uptime_s"] = None
    data["slave_uptime_label"] = format_compact_uptime(data.get("slave_uptime_s"))
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
    now = now_utc()
    now_ist = now.replace(tzinfo=timezone.utc).astimezone(IST_TIMEZONE)
    start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    start_dt = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    end_exclusive = now + timedelta(seconds=1)
    label = "Today" if days == 1 else f"Last {days} days"
    return start_dt, end_exclusive, label


def attach_default_chart_windows(payload, device_id=None):
    """Supply legacy default chart windows only when no range was requested."""
    if not isinstance(payload, dict):
        return payload
    requested_days = request.args.get("days", type=int)
    if request.args.get("start_date") or request.args.get("end_date") or requested_days is not None:
        return payload
    now = now_utc()
    now_ist = now.replace(tzinfo=timezone.utc).astimezone(IST_TIMEZONE)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = midnight_ist.astimezone(timezone.utc).replace(tzinfo=None)
    today_payload = build_analytics(today_start, now + timedelta(seconds=1), "Today", device_id=device_id)
    payload["default_chart_windows"] = {
        "tank_level": {
            "range": today_payload.get("range") or {},
            "levels": today_payload.get("levels") or {},
        },
        "pump_activity": {
            "range": today_payload.get("range") or {},
            "motor": today_payload.get("motor") or {},
            "pump_activity": today_payload.get("pump_activity") or {},
        },
        "daily_use": {
            "range": payload.get("range") or {},
            "daily": payload.get("daily") or {},
        },
    }
    return payload


def build_analytics_query(start_dt, end_exclusive, device_id=None, include_all_sources=False):
    query = """
        SELECT id, level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
               pump_failure, dry_run, wifi, wifi_rssi, sensor, lower_tank_level,
               ai_usage_rate, tomorrow_prediction, tank_capacity_liters,
               pump_total_runtime_s, pump_cycle_count, pump_runtime_boot_id, created_at
        FROM tank_data
        WHERE """
    params = []
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        query += "device_id = ? AND "
        params.append(normalized_device_id)
    query += "created_at >= ? AND created_at < ? AND "
    params.extend([start_dt.strftime(TIMESTAMP_FORMAT), end_exclusive.strftime(TIMESTAMP_FORMAT)])
    if include_all_sources:
        query += "1 = 1"
    else:
        source_clause, source_params = device_source_where_clause()
        query += source_clause
        params.extend(source_params)
    query += " ORDER BY created_at ASC, id ASC"
    return query, tuple(params)


def analytics_row_has_valid_level(row):
    level = safe_float((row or {}).get("level"), None)
    return level is not None and math.isfinite(level) and 0 <= level <= 100


def filter_valid_analytics_rows(rows):
    # ``build_analytics`` already materializes database rows as dictionaries.
    # Reusing those objects avoids a second full copy of large telemetry
    # histories, which is significant on memory-limited application workers.
    return [row if isinstance(row, dict) else dict(row) for row in (rows or []) if analytics_row_has_valid_level(row)]


def fetch_event_analytics_rows(start_dt, end_exclusive, device_id=None, existing_source_row_ids=None):
    normalized_device_id = normalize_device_id(device_id)
    if get_device_source_mode() != DEVICE_SOURCE_REAL:
        return []

    where_clauses = [
        "event_at >= ?",
        "event_at < ?",
        "details_json IS NOT NULL",
        "(details_json LIKE ? OR details_json LIKE ?)",
    ]
    params = [
        start_dt.strftime(TIMESTAMP_FORMAT),
        end_exclusive.strftime(TIMESTAMP_FORMAT),
        '%"level"%',
        '%\\"level\\"%',
    ]
    if normalized_device_id:
        where_clauses.insert(0, "device_id = ?")
        params.insert(0, normalized_device_id)

    query = f"""
        SELECT id, event_kind, details_json, source_table, source_row_id, event_at
        FROM device_events
        WHERE {' AND '.join(where_clauses)}
        ORDER BY event_at ASC, id ASC
        LIMIT 5000
    """
    existing_ids = {str(value) for value in (existing_source_row_ids or set()) if value not in (None, "")}
    seen_keys = set()
    rows = []
    with get_db() as db:
        event_rows = [dict(row) for row in db.execute(query, tuple(params))]

    for event_row in event_rows:
        try:
            details = json.loads(event_row.get("details_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict):
            continue
        level = safe_float(details.get("level"), None)
        if level is None or not math.isfinite(level) or level < 0 or level > 100:
            continue

        source_row_id = str(details.get("source_row_id") or event_row.get("source_row_id") or "").strip()
        if source_row_id and source_row_id in existing_ids:
            continue
        dedupe_key = source_row_id or f"{event_row.get('event_at')}:{event_row.get('event_kind')}:{level}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        motor = str(details.get("motor") or details.get("pump") or "").strip().upper()
        if motor not in {"ON", "OFF"}:
            raw_line = str(details.get("raw_line") or "")
            motor_match = re.search(r"\bpump=(ON|OFF)\b", raw_line, flags=re.IGNORECASE)
            motor = motor_match.group(1).upper() if motor_match else "OFF"

        event_at = parse_timestamp(event_row.get("event_at")) or now_utc()
        rows.append(
            {
                "id": f"event:{event_row.get('id')}",
                "level": level,
                "motor": motor,
                "mode": details.get("mode") or "AUTO",
                "pipe_leak": details.get("pipe_leak") or "NO",
                "slow_leak": details.get("slow_leak") or "NO",
                "drip": details.get("drip") or "NO",
                "abnormal": details.get("abnormal") or "NO",
                "pump_failure": details.get("pump_failure") or "NO",
                "dry_run": details.get("dry_run") or "NO",
                "wifi": details.get("wifi") or "ONLINE",
                "wifi_rssi": details.get("wifi_rssi"),
                "sensor": details.get("sensor") or "OK",
                "lower_tank_level": details.get("lower_tank_level") or details.get("source_tank_level"),
                "ai_usage_rate": details.get("ai_usage_rate"),
                "tomorrow_prediction": details.get("tomorrow_prediction"),
                "created_at": event_at,
                "_analytics_source": "device_events",
            }
        )

    return rows


def merge_analytics_source_rows(tank_rows, event_rows):
    merged_rows = []
    seen = set()
    for row in list(tank_rows or []) + list(event_rows or []):
        row_dict = dict(row)
        created_at = parse_timestamp(row_dict.get("created_at")) if not isinstance(row_dict.get("created_at"), datetime) else row_dict.get("created_at")
        if created_at is None:
            continue
        source_key = str(row_dict.get("id") or row_dict.get("source_row_id") or "")
        dedupe_key = source_key or f"{created_at.strftime(TIMESTAMP_FORMAT)}:{safe_float(row_dict.get('level'), 0):.2f}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        row_dict["created_at"] = created_at
        merged_rows.append(row_dict)
    return sorted(merged_rows, key=lambda item: (item["created_at"], str(item.get("id") or "")))


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

    # Preserve representative minima and maxima instead of relying only on
    # evenly spaced points, which can hide short tank peaks and drawdowns.
    numeric_budget = max(0, max_points - len(mandatory))
    bucket_count = max(1, numeric_budget // 2) if numeric_budget else 0
    if bucket_count:
        interior_size = max(0, size - 2)
        for bucket in range(bucket_count):
            start = 1 + int(bucket * interior_size / bucket_count)
            stop = 1 + int((bucket + 1) * interior_size / bucket_count)
            candidates = [
                index for index in range(start, max(start + 1, stop))
                if index < size - 1 and safe_values[index] is not None
            ]
            if not candidates:
                continue
            try:
                mandatory.add(min(candidates, key=lambda index: float(safe_values[index])))
                mandatory.add(max(candidates, key=lambda index: float(safe_values[index])))
            except (TypeError, ValueError):
                pass

    if len(mandatory) >= max_points:
        final_indices = pick_series_indices(sorted(mandatory), max_points)
    else:
        remaining = [index for index in range(size) if index not in mandatory]
        final_indices = sorted(mandatory.union(pick_series_indices(remaining, max_points - len(mandatory))))

    return [safe_times[index] for index in final_indices], [safe_values[index] for index in final_indices]


def compact_motor_series(time_values, value_values):
    """Return one point per real binary-state transition.

    Repeated ON or OFF samples are observation duplicates, not new pump
    cycles. A telemetry gap is retained once and starts a new state segment.
    """
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
            if not compact_values or compact_values[-1] is not None:
                compact_times.append(safe_times[index])
                compact_values.append(None)
            last_state = None
            continue

        try:
            numeric_state = int(raw_value)
        except (TypeError, ValueError):
            continue
        if numeric_state not in (0, 1):
            continue
        current_state = numeric_state
        if last_state is None or current_state != last_state:
            compact_times.append(safe_times[index])
            compact_values.append(current_state)
            last_state = current_state

    return compact_times, compact_values


def build_motor_activity_metrics(time_values, value_values):
    safe_times = list(time_values or [])
    safe_values = list(value_values or [])
    size = min(len(safe_times), len(safe_values))
    if size <= 1:
        return {
            "runtime_seconds": 0,
            "runtime_hours": 0,
            "observed_hours": 0,
            "duty_cycle_pct": 0,
            "started_runs": 0,
            "completed_runs": 0,
            "avg_run_seconds": 0,
            "short_cycle_count": 0,
            "avg_off_seconds": 0,
        }

    runtime_seconds = 0.0
    observed_seconds = 0.0
    run_durations = []
    off_durations = []
    current_run_seconds = 0.0
    current_off_seconds = 0.0
    run_active = False
    off_active = False

    # Count every observed OFF/unknown-to-ON relay transition. This is kept
    # separate from completed_runs because a telemetry gap can prevent safely
    # timing a run without erasing the fact that the pump started.
    started_runs = 0
    previous_state = None
    for raw_value in safe_values[:size]:
        if raw_value is None or raw_value == "":
            previous_state = None
            continue
        try:
            state = 1 if int(raw_value) == 1 else 0
        except (TypeError, ValueError):
            previous_state = None
            continue
        if state == 1 and previous_state != 1:
            started_runs += 1
        previous_state = state

    for index in range(size - 1):
        start_time = parse_timestamp(safe_times[index])
        end_time = parse_timestamp(safe_times[index + 1])
        if start_time is None or end_time is None:
            run_active = False
            off_active = False
            current_run_seconds = 0.0
            current_off_seconds = 0.0
            continue

        duration_seconds = (end_time - start_time).total_seconds()
        if duration_seconds <= 0:
            continue

        raw_value = safe_values[index]
        if raw_value is None or raw_value == "":
            if run_active and current_run_seconds > 0:
                runtime_seconds = max(0.0, runtime_seconds - current_run_seconds)
            run_active = False
            off_active = False
            current_run_seconds = 0.0
            current_off_seconds = 0.0
            continue

        observed_seconds += duration_seconds
        is_on = int(raw_value) == 1
        if is_on:
            runtime_seconds += duration_seconds
            current_run_seconds = (current_run_seconds + duration_seconds) if run_active else duration_seconds
            run_active = True
            if off_active and current_off_seconds > 0:
                off_durations.append(current_off_seconds)
            off_active = False
            current_off_seconds = 0.0
        else:
            current_off_seconds = (current_off_seconds + duration_seconds) if off_active else duration_seconds
            off_active = True
            if run_active and current_run_seconds > 0:
                run_durations.append(current_run_seconds)
            run_active = False
            current_run_seconds = 0.0

    # The final sample is an endpoint rather than an interval. It still closes
    # a run whose last ON interval ends at that sample.
    if run_active and current_run_seconds > 0:
        try:
            final_state = int(safe_values[-1])
        except (TypeError, ValueError):
            final_state = None
        if final_state == 0:
            run_durations.append(current_run_seconds)

    completed_runs = len(run_durations)
    avg_run_seconds = sum(run_durations) / completed_runs if completed_runs else 0.0
    avg_off_seconds = sum(off_durations) / len(off_durations) if off_durations else 0.0
    short_cycle_count = sum(1 for seconds in run_durations if seconds < 5 * 60)
    duty_cycle_pct = (runtime_seconds / observed_seconds) * 100.0 if observed_seconds > 0 else 0.0
    return {
        "runtime_seconds": int(round(runtime_seconds)),
        "runtime_hours": round(runtime_seconds / 3600.0, 3),
        "observed_hours": round(observed_seconds / 3600.0, 3),
        "duty_cycle_pct": round(duty_cycle_pct, 2),
        "started_runs": started_runs,
        "completed_runs": completed_runs,
        "avg_run_seconds": int(round(avg_run_seconds)),
        "short_cycle_count": short_cycle_count,
        "avg_off_seconds": int(round(avg_off_seconds)),
    }


def build_authoritative_pump_metrics(rows):
    samples = []
    for row in rows or []:
        total = safe_float((row or {}).get("pump_total_runtime_s"), None)
        created_at = parse_timestamp((row or {}).get("created_at"))
        if total is not None and total >= 0 and created_at is not None:
            samples.append((created_at, total, safe_float((row or {}).get("pump_cycle_count"), 0), str((row or {}).get("pump_runtime_boot_id") or "")))
    if len(samples) < 2:
        return None
    runtime_seconds = 0.0
    started_runs = 0
    for previous, current in zip(samples, samples[1:]):
        _prev_at, prev_total, prev_cycles, _prev_boot = previous
        _at, total, cycles, _boot = current
        if total >= prev_total:
            runtime_seconds += total - prev_total
        if cycles >= prev_cycles:
            started_runs += int(cycles - prev_cycles)
    observed_seconds = max(0.0, (samples[-1][0] - samples[0][0]).total_seconds())
    return {
        "runtime_seconds": int(round(runtime_seconds)), "runtime_hours": round(runtime_seconds / 3600.0, 3),
        "observed_hours": round(observed_seconds / 3600.0, 3),
        "duty_cycle_pct": round((runtime_seconds / observed_seconds) * 100.0, 2) if observed_seconds else 0,
        "started_runs": started_runs, "completed_runs": started_runs,
        "avg_run_seconds": int(round(runtime_seconds / started_runs)) if started_runs else 0,
        "short_cycle_count": 0, "avg_off_seconds": 0,
    }


def extend_ongoing_motor_activity(time_values, value_values, current_time):
    """Close a fresh, currently-ON relay interval at the analysis time."""
    times = list(time_values or [])
    values = list(value_values or [])
    if not times or not values or len(times) != len(values):
        return times, values
    try:
        last_on = int(values[-1]) == 1
    except (TypeError, ValueError):
        return times, values
    last_time = parse_timestamp(times[-1])
    end_time = parse_timestamp(current_time)
    if not last_on or last_time is None or end_time is None:
        return times, values
    trailing_seconds = (end_time - last_time).total_seconds()
    freshness_limit = max(60.0, min(ANALYTICS_MAX_GAP_MINUTES * 60.0, STALE_AFTER_SECONDS))
    if trailing_seconds <= 0 or trailing_seconds > freshness_limit:
        return times, values
    times.append(end_time.strftime(TIMESTAMP_FORMAT))
    values.append(1)
    return times, values


def build_observed_motor_activity_series(rows):
    """Build a gap-safe relay series from raw telemetry, including sensor-error rows."""
    times = []
    values = []
    parsed_times = []
    for row in rows or []:
        created_at = parse_timestamp((row or {}).get("created_at"))
        if created_at is None:
            continue
        motor = str((row or {}).get("motor") or "").strip().upper()
        if motor in {"ON", "RUNNING", "RUN", "1", "TRUE"}:
            value = 1
        elif motor in {"OFF", "STOPPED", "STOP", "0", "FALSE"}:
            value = 0
        else:
            value = None
        times.append(created_at.strftime(TIMESTAMP_FORMAT))
        values.append(value)
        parsed_times.append(created_at)

    gap_limit_seconds = max(60.0, ANALYTICS_MAX_GAP_MINUTES * 60.0)
    for index in range(len(parsed_times) - 1):
        gap_seconds = (parsed_times[index + 1] - parsed_times[index]).total_seconds()
        if gap_seconds <= 0 or gap_seconds > gap_limit_seconds:
            values[index] = None
    return times, values


def infer_pump_activity_from_level_history(
    time_values,
    level_values,
    stop_threshold_pct=None,
    start_threshold_pct=None,
):
    """Infer tank-fill cycles without using the relay pulse state.

    With a configured stop threshold, a complete cycle runs from the clear
    local minimum through the first observed reading at or above 90%. The
    legacy consecutive-rise detector remains available when no threshold is
    supplied.
    """
    safe_times = list(time_values or [])
    safe_levels = list(level_values or [])
    size = min(len(safe_times), len(safe_levels))
    if size <= 0:
        return [], [], []

    parsed_times = [parse_timestamp(value) for value in safe_times[:size]]
    levels = [safe_float(value, None) if value is not None else None for value in safe_levels[:size]]
    states = [0] * size
    runs = []
    gap_limit_seconds = max(60.0, ANALYTICS_MAX_GAP_MINUTES * 60.0)
    candidate_start = None
    candidate_base = None
    candidate_peak = None
    candidate_peak_index = None
    candidate_stop_index = None
    consecutive_rises = 0
    non_rising_intervals = 0
    candidate_confirmed = False
    configured_stop_threshold = safe_float(stop_threshold_pct, None)
    if configured_stop_threshold is not None and not 0 < configured_stop_threshold <= 100:
        configured_stop_threshold = None
    configured_start_threshold = safe_float(start_threshold_pct, None)
    if (
        configured_start_threshold is not None
        and (
            configured_stop_threshold is None
            or not 0 <= configured_start_threshold < configured_stop_threshold
        )
    ):
        configured_start_threshold = None

    # When an upper stop threshold is configured, a completed filling cycle is
    # the lowest reading after the previous fill through the first reading at
    # or above 90%. Intermediate dips, zero readings, sparse telemetry, and
    # sensor fluctuations do not split that minimum-to-maximum cycle.
    if configured_stop_threshold is not None:
        target_level = 90.0
        # A fixed 69-point rise cannot occur for common 30% start / 90% stop
        # configurations. When the start threshold is known, accept a complete
        # threshold-spanning refill while retaining the stricter legacy rule for
        # callers that do not have trustworthy configuration data.
        configured_fill_range = (
            target_level - configured_start_threshold
            if configured_start_threshold is not None
            else None
        )
        required_fill_delta = ANALYTICS_MIN_FULL_FILL_DELTA_PCT
        if configured_fill_range is not None and configured_fill_range > 0:
            required_fill_delta = min(
                required_fill_delta,
                max(ANALYTICS_MIN_REFILL_DELTA_PCT, configured_fill_range),
            )
        minimum_index = None
        minimum_level = None
        for index in range(size):
            level = levels[index]
            timestamp = parsed_times[index]
            if level is None or timestamp is None or not 0 <= level <= 100:
                continue
            if level < target_level:
                if minimum_level is None or level < minimum_level:
                    minimum_index = index
                    minimum_level = level
                continue
            if minimum_index is None or minimum_level is None:
                continue

            started_at = parsed_times[minimum_index]
            rise = float(level - minimum_level)
            if (
                started_at is not None
                and timestamp > started_at
                and rise >= required_fill_delta
            ):
                for state_index in range(minimum_index, index):
                    states[state_index] = 1
                runs.append(
                    {
                        "start_index": minimum_index,
                        "stop_index": index,
                        "started_at": safe_times[minimum_index],
                        "stopped_at": safe_times[index],
                        "duration_seconds": int(round((timestamp - started_at).total_seconds())),
                        "level_rise_pct": round(rise, 2),
                        "start_level_pct": round(float(minimum_level), 2),
                        "stop_level_pct": round(float(level), 2),
                        "stop_threshold_pct": round(target_level, 2),
                    }
                )
            minimum_index = None
            minimum_level = None

        states[-1] = 0 if levels[-1] is not None and parsed_times[-1] is not None else None
        return safe_times[:size], states, runs

    def reset_candidate():
        nonlocal candidate_start, candidate_base, candidate_peak, candidate_peak_index, candidate_stop_index
        nonlocal consecutive_rises, non_rising_intervals
        nonlocal candidate_confirmed
        candidate_start = None
        candidate_base = None
        candidate_peak = None
        candidate_peak_index = None
        candidate_stop_index = None
        consecutive_rises = 0
        non_rising_intervals = 0
        candidate_confirmed = False

    def confirm_candidate(confirmation_index):
        if candidate_start is None or candidate_peak_index is None:
            reset_candidate()
            return
        stop_index = candidate_stop_index if candidate_stop_index is not None else candidate_peak_index
        if stop_index is None:
            reset_candidate()
            return
        if not candidate_confirmed:
            reset_candidate()
            return
        stop_level = levels[stop_index]
        if stop_level is None:
            reset_candidate()
            return
        rise = float(stop_level - candidate_base)
        if rise < ANALYTICS_MIN_REFILL_DELTA_PCT:
            reset_candidate()
            return
        confirmation_level = levels[confirmation_index] if 0 <= confirmation_index < size else None
        # A symmetric rise-and-fall on the next reading is sensor bounce, not a
        # confirmed refill. Allow only a small post-fill settling movement.
        settling_tolerance = max(1.0, rise * 0.25)
        if confirmation_level is None or candidate_peak - confirmation_level > settling_tolerance:
            reset_candidate()
            return
        started_at = parsed_times[candidate_start]
        stopped_at = parsed_times[stop_index]
        if started_at is None or stopped_at is None or stopped_at <= started_at:
            reset_candidate()
            return
        for state_index in range(candidate_start, stop_index):
            states[state_index] = 1
        runs.append(
            {
                "start_index": candidate_start,
                "stop_index": stop_index,
                "started_at": safe_times[candidate_start],
                "stopped_at": safe_times[stop_index],
                "duration_seconds": int(round((stopped_at - started_at).total_seconds())),
                "level_rise_pct": round(rise, 2),
                "stop_level_pct": round(float(stop_level), 2),
                "stop_threshold_pct": round(configured_stop_threshold, 2) if configured_stop_threshold is not None else None,
            }
        )
        reset_candidate()

    for index in range(size - 1):
        current_time = parsed_times[index]
        next_time = parsed_times[index + 1]
        current_level = levels[index]
        next_level = levels[index + 1]
        valid_interval = (
            current_time is not None
            and next_time is not None
            and current_level is not None
            and next_level is not None
            and 0 < (next_time - current_time).total_seconds() <= gap_limit_seconds
            and abs(next_level - current_level) <= ANALYTICS_MAX_LEVEL_DELTA_PCT
        )
        if not valid_interval:
            states[index] = None
            reset_candidate()
            continue

        delta = next_level - current_level
        if candidate_start is None:
            effective_stop_threshold = (
                max(0.0, configured_stop_threshold - ANALYTICS_STOP_THRESHOLD_TOLERANCE_PCT)
                if configured_stop_threshold is not None
                else None
            )
            below_stop_threshold = effective_stop_threshold is None or current_level < effective_stop_threshold
            if delta >= ANALYTICS_MIN_USAGE_DELTA_PCT and below_stop_threshold:
                candidate_start = index
                candidate_base = current_level
                candidate_peak = next_level
                candidate_peak_index = index + 1
                consecutive_rises = 1
                if effective_stop_threshold is not None and next_level >= effective_stop_threshold:
                    candidate_stop_index = index + 1
            continue

        if delta >= ANALYTICS_MIN_USAGE_DELTA_PCT:
            consecutive_rises += 1
            non_rising_intervals = 0
        else:
            consecutive_rises = 0
            non_rising_intervals += 1

        # Ignore sub-noise movements when selecting the runtime endpoint.
        if delta >= ANALYTICS_MIN_USAGE_DELTA_PCT and next_level > candidate_peak:
            candidate_peak = next_level
            candidate_peak_index = index + 1
        effective_stop_threshold = (
            max(0.0, configured_stop_threshold - ANALYTICS_STOP_THRESHOLD_TOLERANCE_PCT)
            if configured_stop_threshold is not None
            else None
        )
        if (
            effective_stop_threshold is not None
            and candidate_stop_index is None
            and next_level >= effective_stop_threshold
        ):
            candidate_stop_index = index + 1

        confirmed_rise = (
            consecutive_rises >= ANALYTICS_FILL_CONFIRM_INTERVALS
            and candidate_peak - candidate_base >= ANALYTICS_MIN_REFILL_DELTA_PCT
        )
        candidate_confirmed = candidate_confirmed or confirmed_rise
        if candidate_stop_index is not None and candidate_confirmed:
            confirm_candidate(index + 1)
        elif non_rising_intervals >= ANALYTICS_FILL_STOP_INTERVALS:
            confirm_candidate(index + 1)

    # A rise at the edge of the selected range has no confirming stop reading,
    # so it is intentionally not reported as a completed pump cycle.
    states[-1] = 0 if levels[-1] is not None and parsed_times[-1] is not None else None
    return safe_times[:size], states, runs


def estimate_level_history_usage(time_values, level_values, fill_states):
    """Estimate drawdown only outside confirmed level-derived fill cycles."""
    safe_times = list(time_values or [])
    safe_levels = list(level_values or [])
    safe_states = list(fill_states or [])
    size = min(len(safe_times), len(safe_levels), len(safe_states))
    daily_usage = {}
    hourly_usage = [0.0] * 24
    hourly_timeline = {}
    rate_segments = []
    total_usage = 0.0
    valid_hours = 0.0
    valid_drop_count = 0
    usage_floor = None

    for index in range(size):
        created_at = parse_timestamp(safe_times[index])
        level = safe_float(safe_levels[index], None) if safe_levels[index] is not None else None
        if created_at is None or level is None:
            usage_floor = None
            continue
        date_key = created_at.date().isoformat()
        daily_usage.setdefault(date_key, 0.0)
        if index == 0:
            usage_floor = level
            continue
        previous_at = parse_timestamp(safe_times[index - 1])
        previous_level = safe_float(safe_levels[index - 1], None) if safe_levels[index - 1] is not None else None
        delta_hours = (created_at - previous_at).total_seconds() / 3600.0 if previous_at else 0.0
        valid_interval = (
            previous_at is not None
            and previous_level is not None
            and 0 < delta_hours <= (ANALYTICS_MAX_GAP_MINUTES / 60.0)
            and abs(level - previous_level) <= ANALYTICS_MAX_LEVEL_DELTA_PCT
        )
        if not valid_interval:
            usage_floor = level
            continue
        valid_hours += delta_hours
        interval_fill = safe_states[index - 1] == 1
        if interval_fill:
            usage_floor = level
            continue
        if usage_floor is None:
            usage_floor = previous_level
        usage = max(0.0, usage_floor - level)
        if usage >= ANALYTICS_MIN_USAGE_DELTA_PCT:
            total_usage += usage
            daily_usage[date_key] += usage
            hourly_usage[created_at.hour] += usage
            hour_key = created_at.replace(minute=0, second=0, microsecond=0).strftime(TIMESTAMP_FORMAT)
            hourly_timeline[hour_key] = hourly_timeline.get(hour_key, 0.0) + usage
            if delta_hours >= (1.0 / 60.0):
                valid_drop_count += 1
                rate_segments.append(usage / delta_hours)
            usage_floor = level

    return {
        "daily_usage": daily_usage,
        "hourly_usage": hourly_usage,
        "hourly_timeline": hourly_timeline,
        "total_usage": total_usage,
        "valid_hours": valid_hours,
        "valid_drop_count": valid_drop_count,
        "consumption_rate_segments": rate_segments,
    }


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


def analytics_last_valid_setting_key(cache_key):
    try:
        cache_key_parts = [str(part) for part in cache_key]
    except TypeError:
        cache_key_parts = [str(cache_key)]
    device_token = normalize_device_id(cache_key_parts[2]) if len(cache_key_parts) >= 3 else ""
    device_token = device_token or "all"
    digest = hashlib.sha256(json.dumps(cache_key_parts, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"{ANALYTICS_LAST_VALID_SETTING_PREFIX}{device_token}:{digest}"


def build_analytics_cache_key(start_dt, end_exclusive, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    return (
        start_dt.strftime(DATE_ONLY_FORMAT),
        end_exclusive.strftime(DATE_ONLY_FORMAT),
        normalized_device_id or "*",
        get_device_source_mode(),
        ANALYTICS_ALGORITHM_VERSION,
    )


def analytics_build_lock(cache_key):
    """Return the per-window lock used to collapse concurrent analytics work."""
    with analytics_build_locks_guard:
        lock = analytics_build_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            analytics_build_locks[cache_key] = lock
        return lock


def copy_analytics_payload(payload):
    return copy.deepcopy(payload) if isinstance(payload, dict) else payload


def analytics_series_has_points(series, value_key="values"):
    if not isinstance(series, dict):
        return False
    values = series.get(value_key)
    if not isinstance(values, list):
        return False
    return any(value is not None for value in values)


def analytics_payload_has_valid_result(payload):
    if not isinstance(payload, dict):
        return False
    analysis = payload.get("analysis") or {}
    quality = analysis.get("quality") or {}
    if bool(analysis.get("live_snapshot_fallback")):
        return False
    if int(safe_float(quality.get("row_count"), 0)) < 2:
        return False
    return any(
        (
            analytics_series_has_points(payload.get("levels")),
            analytics_series_has_points(payload.get("motor")),
            analytics_series_has_points(payload.get("daily")),
            analytics_series_has_points(payload.get("pattern")),
        )
    )


def analytics_payload_version(payload):
    version_source = {
        "range": (payload or {}).get("range") or {},
        "latest_sync_at": (payload or {}).get("latest_sync_at"),
        "quality": ((payload or {}).get("analysis") or {}).get("quality") or {},
        "insights": (payload or {}).get("insights") or {},
    }
    return hashlib.sha256(
        json.dumps(version_source, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


def mark_computed_analytics_payload(payload, now_ts=None):
    if not isinstance(payload, dict):
        return payload
    payload.setdefault("analytics_source", "computed")
    payload.setdefault("analytics_generated_at", now_utc().strftime(TIMESTAMP_FORMAT))
    payload.setdefault("analytics_version", analytics_payload_version(payload))
    payload.setdefault("analytics_cached", False)
    return payload


def persist_last_valid_analytics(cache_key, payload, now_ts=None):
    if not analytics_payload_has_valid_result(payload) or payload.get("analytics_cached"):
        return False
    payload_to_store = copy_analytics_payload(payload)
    mark_computed_analytics_payload(payload_to_store, now_ts=now_ts)
    record = {
        "schema": ANALYTICS_LAST_VALID_SCHEMA_VERSION,
        "cache_key": [str(part) for part in cache_key],
        "saved_at": now_utc().strftime(TIMESTAMP_FORMAT),
        "analytics_version": payload_to_store.get("analytics_version"),
        "payload": payload_to_store,
    }
    try:
        set_app_setting(analytics_last_valid_setting_key(cache_key), json.dumps(record, default=str))
        return True
    except Exception as exc:
        logger.warning("Could not persist last valid analytics for %s: %s", cache_key, exc)
        return False


def read_last_valid_analytics(cache_key, reason=None):
    raw_record = get_app_setting(analytics_last_valid_setting_key(cache_key))
    if not raw_record:
        return None
    try:
        record = json.loads(raw_record)
    except (TypeError, ValueError):
        return None
    if record.get("schema") != ANALYTICS_LAST_VALID_SCHEMA_VERSION:
        return None
    expected_key = [str(part) for part in cache_key]
    if record.get("cache_key") != expected_key:
        return None
    payload = copy_analytics_payload(record.get("payload"))
    if not analytics_payload_has_valid_result(payload):
        return None

    payload["analytics_cached"] = True
    payload["analytics_source"] = "last_valid"
    payload["analytics_cached_at"] = record.get("saved_at")
    payload["analytics_version"] = record.get("analytics_version") or analytics_payload_version(payload)
    warning = reason or "Showing the last successful AI analysis while fresh analytics catches up."
    payload["analytics_warning"] = warning
    alerts = payload.setdefault("alerts", [])
    if isinstance(alerts, list) and warning not in alerts:
        alerts.insert(0, warning)
    return payload


def fallback_analytics_payload(cache_key, empty_payload, reason=None, now_ts=None):
    cached_payload = read_last_valid_analytics(cache_key, reason=reason)
    if cached_payload is not None:
        return store_cached_analytics(cache_key, cached_payload, now_ts=now_ts)
    return store_cached_analytics(cache_key, empty_payload, now_ts=now_ts)


def build_analytics_fallback_payload(start_dt, end_exclusive, label, device_id=None, reason=None, now_ts=None):
    normalized_device_id = normalize_device_id(device_id)
    cache_key = build_analytics_cache_key(start_dt, end_exclusive, normalized_device_id)
    empty_payload = build_empty_analytics(start_dt, end_exclusive, label, normalized_device_id)
    if reason:
        empty_payload["analytics_warning"] = reason
    return fallback_analytics_payload(cache_key, empty_payload, reason=reason, now_ts=now_ts)


def store_cached_analytics(cache_key, payload, now_ts=None):
    if analytics_payload_has_valid_result(payload):
        mark_computed_analytics_payload(payload, now_ts=now_ts)
        persist_last_valid_analytics(cache_key, payload, now_ts=now_ts)
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
        "auto_start_pct": DEFAULT_DEVICE_AUTO_START_PCT,
        "auto_stop_pct": DEFAULT_DEVICE_AUTO_STOP_PCT,
        "auto_start_stable_ms": None,
        "auto_level_average_samples": None,
        "remaining_liters": 0,
        "tank_health": 100,
        "tank_health_status": "Healthy",
        "tank_health_reasons": ["No data has been received yet."],
        "device_id": normalized_device_id or None,
    }
    live_snapshot_available = bool(snapshot and snapshot.get("device_id"))
    snapshot_level = round(safe_float(snapshot.get("level"), 0), 2)
    snapshot_time = format_timestamp(snapshot.get("created_at")) or now_utc().strftime(TIMESTAMP_FORMAT)
    snapshot_dt = parse_timestamp(snapshot_time) or now_utc()
    baseline_time = (snapshot_dt - timedelta(minutes=10)).strftime(TIMESTAMP_FORMAT)
    snapshot_date = (parse_timestamp(snapshot_time) or now_utc()).strftime(DATE_ONLY_FORMAT)
    level_times = [baseline_time, snapshot_time] if live_snapshot_available else []
    level_values = [snapshot_level, snapshot_level] if live_snapshot_available else []
    motor_times = [baseline_time, snapshot_time] if live_snapshot_available else []
    motor_values = [0, 0] if live_snapshot_available else []
    motor_cycles = 0
    daily_dates = []
    if live_snapshot_available:
        cursor_date = start_dt.date()
        final_date = (end_exclusive - timedelta(days=1)).date()
        while cursor_date <= final_date and len(daily_dates) < 62:
            daily_dates.append(cursor_date.isoformat())
            cursor_date += timedelta(days=1)
        if snapshot_date not in daily_dates:
            daily_dates.append(snapshot_date)
    daily_values = [0.0] if live_snapshot_available else []
    if daily_dates:
        daily_values = [0.0] * len(daily_dates)
    fallback_alert = (
        f"Live snapshot is available for {normalized_device_id}; more history is needed for forecasts."
        if live_snapshot_available and normalized_device_id
        else (
            f"No telemetry available for device {normalized_device_id} in the selected range."
            if normalized_device_id
            else "No telemetry available for the selected range."
        )
    )

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
            "avg_daily_usage": None,
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
        "daily": {"dates": daily_dates, "values": daily_values},
        "pattern": {"time": [], "values": [], "aggregation": "hourly_selected_range"},
        "levels": {"time": level_times, "values": level_values},
        "motor": {
            "time": motor_times,
            "values": motor_values,
            "source": "tank_level_history",
            "relay_state_used": False,
        },
        "comparison": {
            "latest_day": "--",
            "latest_day_usage": 0,
            "previous_day": "--",
            "previous_day_usage": 0,
            "change_pct": None
        },
        "prediction": {
            "tomorrow_usage": None,
            "tomorrow_usage_liters": None,
            "confidence": 0,
            "sample_days": 0,
            "status": "insufficient_data",
            "method": "robust_weighted_daily_baseline_v2",
            "limitations": ["At least two complete days with reliable telemetry are required."],
        },
        "analysis": {
            "quality": {
                "score": 0,
                "row_count": 0,
                "usable_hours": 0,
                "requested_hours": round(max(0.0, (end_exclusive - start_dt).total_seconds() / 3600.0), 2),
                "coverage_ratio": 0,
                "coverage_percent": 0,
                "gap_count": 0,
                "valid_drop_count": 0,
                "status": "limited",
                "sufficient_for_anomaly": False,
                "sufficient_for_forecast": False,
                "limitations": ["Historical telemetry is not available."],
            },
            "forecast_confidence": 0,
            "anomaly_count": 0,
            "anomalies": [],
            "leakage": {
                "model": "telemetry-leakage-ai-v2",
                "status": "insufficient_data",
                "label": "Not enough reliable data",
                "severity": "info",
                "score": 0,
                "confidence": 0,
                "leak_type": "none",
                "features": {},
                "reasons": ["Not enough telemetry is available for leakage analysis."],
            },
            "model": {
                "family": "coverage-aware-robust-hybrid-v2",
                "calibration": "evidence-gated",
                "signals": ["live_snapshot_fallback"] if live_snapshot_available else [],
            },
            "live_snapshot_fallback": live_snapshot_available,
        },
        "events_analysis": {
            "event_count": 0,
            "event_counts": {},
            "severity_counts": {},
            "pump_runtime_seconds": 0,
            "pump_runtime_hours": 0,
            "pump_runs": 0,
            "latest_warning": None,
            "retention_days": DEVICE_EVENT_RETENTION_DAYS,
        },
        "pump_activity": {
            "runtime_seconds": 0,
            "runtime_hours": 0,
            "observed_hours": 0,
            "duty_cycle_pct": 0,
            "started_runs": 0,
            "completed_runs": 0,
            "avg_run_seconds": 0,
            "short_cycle_count": 0,
            "avg_off_seconds": 0,
            "source": "tank_level_history",
            "relay_state_used": False,
            "inference_status": "insufficient_level_history",
            "runtime_basis": "tank_level_rise_to_configured_stop_threshold",
            "stop_threshold_pct": round(safe_float(snapshot.get("auto_stop_pct"), DEFAULT_DEVICE_AUTO_STOP_PCT), 1),
            "last_started_at": None,
            "last_stopped_at": None,
            "validated_runs": [],
        },
        "alerts": [fallback_alert]
    }
    payload["guidance"] = build_shared_guidance_payload(snapshot, payload)
    return payload


def meaningful_forecast_hours(snapshot, analytics_payload):
    insights = (analytics_payload or {}).get("insights") or {}
    analysis = (analytics_payload or {}).get("analysis") or {}
    quality = analysis.get("quality") or {}
    leakage_model = analysis.get("leakage") or {}
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
    confidence = safe_float(analysis.get("forecast_confidence"), safe_float(quality.get("score"), 0))
    leak_signal = any(bool_flag((snapshot or {}).get(key)) for key in ("pipe_leak", "slow_leak", "drip", "abnormal"))
    ai_leak_status = str(leakage_model.get("status") or "").lower()
    ai_leak_score = safe_float(leakage_model.get("score"), 0)
    if confidence < 60 or not bool(quality.get("sufficient_for_forecast", confidence >= 60)):
        return None
    # A short extrapolated empty-time from noisy history should not override a
    # clearly full live tank. Keep the forecast actionable only near the working
    # range or when current consumption is extremely high.
    if level >= 90 and forecast_hours < 12 and consumption_rate < 20:
        return None
    if level >= 75 and forecast_hours < 6 and consumption_rate < 12:
        return None
    if level >= 70 and forecast_hours < 6 and not leak_signal:
        if not ai_leakage_alert_eligible(leakage_model) or ai_leak_status != "likely_leak" or ai_leak_score < 70:
            return None
    if level >= 70 and forecast_hours < 4 and confidence < 75 and not leak_signal:
        return None
    return round(forecast_hours, 2)


def dry_run_fault_recovered(snapshot):
    snapshot = snapshot or {}
    telemetry = str(snapshot.get("telemetry_status") or "").strip().lower()
    if telemetry in {"stale", "offline", "no-data"}:
        return False
    if str(snapshot.get("motor") or "").strip().upper() == "ON":
        return False
    sensor = str(snapshot.get("sensor") or "").strip().upper()
    if sensor and sensor != "OK":
        return False

    source_service = str(snapshot.get("lower_tank_service") or snapshot.get("source_tank_service") or "").strip().upper()
    source_level_raw = snapshot.get("lower_tank_level", snapshot.get("source_tank_level"))
    source_level = None if source_level_raw in (None, "", "null") else safe_float(source_level_raw, -1)
    source_sensor = str(snapshot.get("lower_sensor") or snapshot.get("source_sensor") or "").strip().upper()
    if source_service == "ON" and (
        source_level is None
        or source_level < 20
        or (source_sensor and source_sensor not in {"OK", "DISABLED"})
    ):
        return False

    level = safe_float(snapshot.get("level"), -1)
    auto_start = safe_float(snapshot.get("auto_start_pct") or snapshot.get("auto_start_level_pct"), 40)
    recovery_level = max(45.0, min(85.0, auto_start + 10.0))
    return level >= recovery_level


def effective_dry_run_active(snapshot):
    return bool_flag((snapshot or {}).get("dry_run")) and not dry_run_fault_recovered(snapshot)


def effective_pump_failure_active(snapshot):
    return bool_flag((snapshot or {}).get("pump_failure")) and not dry_run_fault_recovered(snapshot)


def numeric_percentile(values, fraction):
    clean_values = []
    for value in values or []:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_value):
            clean_values.append(numeric_value)
    clean_values = sorted(clean_values)
    if not clean_values:
        return None
    if len(clean_values) == 1:
        return clean_values[0]
    position = (len(clean_values) - 1) * max(0.0, min(1.0, float(fraction)))
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return clean_values[lower_index]
    weight = position - lower_index
    return clean_values[lower_index] + ((clean_values[upper_index] - clean_values[lower_index]) * weight)


def robust_consumption_rate(mean_rate, segment_rates, valid_hours, valid_drop_count):
    clean_rates = []
    for rate in segment_rates or []:
        try:
            numeric_rate = float(rate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(numeric_rate) and numeric_rate >= 0:
            clean_rates.append(numeric_rate)
    if valid_hours < 0.25 or valid_drop_count < 2 or not clean_rates:
        return 0.0

    median_rate = numeric_percentile(clean_rates, 0.5) or 0.0
    upper_rate = numeric_percentile(clean_rates, 0.75) or median_rate
    blended_rate = (max(0.0, float(mean_rate or 0.0)) * 0.55) + (median_rate * 0.45)
    if len(clean_rates) < 4:
        blended_rate = min(blended_rate, median_rate)
    else:
        blended_rate = min(blended_rate, max(median_rate, upper_rate))
    if blended_rate < ANALYTICS_MIN_CONSUMPTION_RATE_PCT_PER_HOUR:
        return 0.0
    return round(float(blended_rate), 4)


def level_usage_matches_observed_refills(total_usage_pct, motor_cycles, allowance_per_fill_pct=120.0):
    """Reject accumulated level drops that exceed observed available tank volume.

    The opening tank contributes one fill. Each observed pump start can contribute at
    most roughly one additional fill; a 20% allowance covers thresholds and rounding.
    Missing pump history cannot prove a high usage estimate, so callers should hide it.
    """
    total = max(0.0, safe_float(total_usage_pct, 0.0))
    cycles = max(0, int(safe_float(motor_cycles, 0)))
    limit = float((cycles + 1) * max(100.0, safe_float(allowance_per_fill_pct, 120.0)))
    return total <= limit, round(limit, 1)


def daily_usage_matches_tank_turnover_limit(daily_usage, max_turnovers=None):
    """Reject daily drawdown totals that imply an unreasonable number of tankfuls.

    Refill inference can otherwise make sensor oscillation look physically possible:
    every false rise adds another refill and therefore another usage allowance.
    """
    turnover_limit = max(
        1.0,
        safe_float(max_turnovers, ANALYTICS_MAX_DAILY_TANK_TURNOVERS),
    )
    limit_pct = turnover_limit * 100.0
    values = [
        max(0.0, safe_float(value, 0.0))
        for value in (daily_usage or {}).values()
    ]
    peak_usage_pct = max(values, default=0.0)
    return peak_usage_pct <= limit_pct, round(limit_pct, 1), round(peak_usage_pct, 1)


def build_analytics_quality_payload(
    row_count,
    valid_hours,
    gap_count,
    valid_drop_count,
    latest_seconds_since_sync,
    window_hours=None,
):
    requested_hours = max(0.0, safe_float(window_hours, valid_hours))
    coverage_ratio = min(1.0, (float(valid_hours) / requested_hours)) if requested_hours > 0 else 0.0
    row_score = min(20.0, (max(0, int(row_count)) / 48.0) * 20.0)
    duration_target = min(max(requested_hours, 1.0), 24.0)
    duration_score = min(25.0, (max(0.0, float(valid_hours)) / duration_target) * 25.0)
    evidence_score = min(15.0, (max(0, int(valid_drop_count)) / 8.0) * 15.0)
    coverage_score = coverage_ratio * 25.0
    freshness_score = 15.0
    stale = latest_seconds_since_sync is None or latest_seconds_since_sync > STALE_AFTER_SECONDS
    if stale:
        freshness_score = 0.0
    gap_penalty = min(20.0, max(0, int(gap_count)) * 2.5)
    score = int(round(max(0.0, min(96.0, row_score + duration_score + evidence_score + coverage_score + freshness_score - gap_penalty))))
    limitations = []
    if row_count < 4:
        limitations.append("Too few telemetry samples.")
    if coverage_ratio < 0.5:
        limitations.append("Less than half of the selected time range has continuous telemetry.")
    if valid_drop_count < 3:
        limitations.append("Too few validated off-pump level drops for pattern detection.")
    if stale:
        limitations.append("The latest telemetry is stale or unavailable.")
    sufficient_for_anomaly = score >= 60 and valid_hours >= 2 and valid_drop_count >= 3 and row_count >= 8
    sufficient_for_forecast = score >= 60 and valid_hours >= 6 and row_count >= 12
    return {
        "score": score,
        "row_count": int(row_count),
        "usable_hours": round(float(valid_hours), 2),
        "requested_hours": round(requested_hours, 2),
        "coverage_ratio": round(coverage_ratio, 3),
        "coverage_percent": round(coverage_ratio * 100.0, 1),
        "gap_count": int(gap_count),
        "valid_drop_count": int(valid_drop_count),
        "status": "strong" if score >= 80 else "moderate" if score >= 60 else "limited",
        "sufficient_for_anomaly": sufficient_for_anomaly,
        "sufficient_for_forecast": sufficient_for_forecast,
        "limitations": limitations,
    }


def forecast_confidence_from_quality(quality, consumption_rate, current_level, leak_events=0):
    score = safe_float((quality or {}).get("score"), 0)
    if not bool((quality or {}).get("sufficient_for_forecast", score >= 60)):
        score = min(score, 45)
    if consumption_rate <= 0:
        score -= 25
    if current_level >= 70 and consumption_rate >= 25 and leak_events <= 0:
        score -= 18
    if leak_events > 0:
        score += 8
    return max(0, min(95, int(round(score))))


def estimate_tomorrow_usage(avg_daily_usage, latest_day_usage, previous_day_usage, peak_value, daily_history=None):
    if daily_history is not None:
        history = [float(value) for value in daily_history if value is not None and math.isfinite(float(value)) and float(value) >= 0]
        if len(history) < 2:
            return None
        recent = history[-7:]
        median = percentile_value(recent, 50)
        weights = list(range(1, len(recent) + 1))
        weighted_mean = sum(value * weight for value, weight in zip(recent, weights)) / sum(weights)
        changes = [
            (current - previous) / previous
            for previous, current in zip(recent, recent[1:])
            if previous > 0
        ]
        trend = percentile_value(changes, 50) if changes else 0.0
        trend = max(-0.25, min(0.25, trend))
        robust_baseline = (median * 0.6) + (weighted_mean * 0.4)
        return round(max(0.0, robust_baseline * (1.0 + trend * 0.35)), 2)
    values = [float(value or 0.0) for value in (avg_daily_usage, latest_day_usage, previous_day_usage, peak_value)]
    avg_usage, latest_usage, previous_usage, peak_usage = values
    if max(values) <= 0:
        return 0.0
    baseline = (avg_usage * 0.45) + (latest_usage * 0.35) + (previous_usage * 0.15) + (min(peak_usage, max(avg_usage, latest_usage) * 1.5) * 0.05)
    if latest_usage > previous_usage > 0:
        baseline *= min(1.18, 1.0 + ((latest_usage - previous_usage) / previous_usage) * 0.12)
    return round(max(0.0, baseline), 2)


def build_daily_usage_forecast(daily_history, quality):
    history = [float(value) for value in (daily_history or []) if value is not None and math.isfinite(float(value)) and float(value) >= 0]
    quality_score = int(safe_float((quality or {}).get("score"), 0))
    sufficient_quality = bool((quality or {}).get("sufficient_for_forecast", quality_score >= 60))
    if len(history) < 2:
        reasons = []
        reasons.append("At least two complete days are required.")
        return {
            "value": None,
            "lower": None,
            "upper": None,
            "confidence": min(45, quality_score),
            "sample_days": len(history),
            "status": "insufficient_data",
            "method": "robust_weighted_daily_baseline_v2",
            "limitations": reasons,
        }

    value = estimate_tomorrow_usage(0, 0, 0, 0, daily_history=history)
    median = percentile_value(history, 50)
    deviations = [abs(item - median) for item in history]
    mad = percentile_value(deviations, 50)
    relative_variability = (mad / median) if median > 0 else (1.0 if mad > 0 else 0.0)
    uncertainty_fraction = max(0.10, min(0.60, 0.12 + relative_variability * 1.5 + (0.12 if len(history) < 4 else 0.0)))
    confidence = int(round(max(0.0, min(95.0, quality_score - relative_variability * 35.0 - (12 if len(history) < 4 else 0)))))
    limitations = []
    if not sufficient_quality:
        confidence = min(confidence, 45)
        limitations.append("Provisional estimate: telemetry validation is limited.")
    if confidence < 60:
        limitations.append("Recent daily usage varies substantially.")
    return {
        "value": round(float(value), 2),
        "lower": round(max(0.0, float(value) * (1.0 - uncertainty_fraction)), 2),
        "upper": round(float(value) * (1.0 + uncertainty_fraction), 2),
        "confidence": confidence,
        "sample_days": len(history),
        "status": "ready" if confidence >= 60 and sufficient_quality else "low_confidence",
        "method": "robust_weighted_daily_baseline_v2",
        "limitations": limitations,
    }


def percentile_value(values, percentile):
    numeric_values = sorted(float(value) for value in values if value is not None and math.isfinite(float(value)))
    if not numeric_values:
        return 0.0
    if len(numeric_values) == 1:
        return numeric_values[0]
    position = (len(numeric_values) - 1) * max(0.0, min(100.0, float(percentile))) / 100.0
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return numeric_values[lower_index]
    lower_value = numeric_values[lower_index]
    upper_value = numeric_values[upper_index]
    return lower_value + ((upper_value - lower_value) * (position - lower_index))


def build_leakage_ai_model(
    *,
    leak_events,
    consumption_rate,
    consumption_rate_segments,
    usage_change_pct,
    motor_cycles,
    refill_events,
    valid_hours,
    valid_drop_count,
    quality,
    pump_activity_metrics=None,
):
    pump_activity_metrics = pump_activity_metrics or {}
    rates = [float(value) for value in (consumption_rate_segments or []) if value is not None and math.isfinite(float(value)) and value > 0]
    median_rate = percentile_value(rates, 50)
    p90_rate = percentile_value(rates, 90)
    latest_rates = rates[-min(6, len(rates)) :] if rates else []
    latest_rate = sum(latest_rates) / len(latest_rates) if latest_rates else 0.0
    baseline = median_rate if median_rate > 0 else max(1.0, float(consumption_rate or 0.0))
    rate_ratio = (latest_rate or float(consumption_rate or 0.0)) / baseline if baseline > 0 else 0.0

    score = 0.0
    reasons = []
    if leak_events > 0:
        score += min(55.0, 35.0 + (leak_events * 4.0))
        reasons.append("Device leak flags were active.")
    if valid_drop_count >= 3 and valid_hours >= 1:
        score += min(18.0, 8.0 + valid_drop_count)
        reasons.append("Tank level dropped repeatedly while the pump was off.")
    if p90_rate >= 20:
        score += 26.0
        reasons.append("Peak off-pump loss rate is very high.")
    elif p90_rate >= 10:
        score += 16.0
        reasons.append("Peak off-pump loss rate is above normal.")
    if consumption_rate >= 15:
        score += 16.0
        reasons.append("Average consumption rate is high for the selected range.")
    elif consumption_rate >= 8:
        score += 8.0
    if rate_ratio >= 2.5 and latest_rate >= 5:
        score += 18.0
        reasons.append("Recent usage is much higher than the learned baseline.")
    elif rate_ratio >= 1.7 and latest_rate >= 3:
        score += 10.0
    if usage_change_pct is not None and usage_change_pct >= 60:
        score += 22.0
        reasons.append("Daily usage jumped sharply against the previous day.")
    elif usage_change_pct is not None and usage_change_pct >= 25:
        score += 12.0
        reasons.append("Daily usage is above the recent baseline.")
    if motor_cycles > 12:
        score += 6.0
    short_cycle_count = int(safe_float(pump_activity_metrics.get("short_cycle_count"), 0))
    duty_cycle_pct = safe_float(pump_activity_metrics.get("duty_cycle_pct"), 0)
    if short_cycle_count >= 4:
        score += min(8.0, short_cycle_count)
        reasons.append("Pump activity shows repeated short runs.")
    if duty_cycle_pct >= 45 and consumption_rate >= 8:
        score += 4.0
        reasons.append("Pump duty cycle is high during this range.")
    if refill_events > 0 and leak_events <= 0:
        score = max(0.0, score - min(12.0, refill_events * 3.0))

    quality_score = safe_float((quality or {}).get("score"), 35)
    sufficient_evidence = bool(
        (quality or {}).get(
            "sufficient_for_anomaly",
            quality_score >= 60 and valid_hours >= 2 and valid_drop_count >= 3,
        )
    )
    if quality_score < 60:
        score *= 0.82
        reasons.append("Telemetry quality is limited, so the model reduced confidence.")

    score = round(max(0.0, min(100.0, score)), 1)
    confidence = int(max(0, min(95, quality_score + (10 if len(rates) >= 6 else -12) + (8 if leak_events > 0 else 0))))
    if leak_events <= 0 and not sufficient_evidence:
        status = "insufficient_data"
        severity = "info"
        label = "Not enough reliable data"
        reasons.insert(0, "More continuous off-pump telemetry is required before classifying leakage.")
    elif score >= 70:
        status = "likely_leak"
        severity = "danger"
        label = "Likely leakage"
    elif score >= 45:
        status = "possible_leak"
        severity = "warning"
        label = "Possible leakage"
    elif score >= 30:
        status = "watch"
        severity = "info"
        label = "Watch usage"
    else:
        status = "normal"
        severity = "ok"
        label = "No leakage pattern"

    leak_type = "none"
    if status in {"likely_leak", "possible_leak"}:
        if leak_events > 0 or p90_rate >= 20:
            leak_type = "pipe_leak"
        elif valid_drop_count >= 3 or rate_ratio >= 1.7:
            leak_type = "slow_leak"
        else:
            leak_type = "usage_anomaly"

    alert_eligible = (
        status in {"likely_leak", "possible_leak"}
        and confidence > AI_LEAK_ALERT_MIN_CONFIDENCE
        and score > 90.0
    )
    return {
        "model": "telemetry-leakage-ai-v2",
        "status": status,
        "label": label,
        "severity": severity,
        "score": score,
        "confidence": confidence,
        "alert_confidence_threshold": AI_LEAK_ALERT_MIN_CONFIDENCE,
        "alert_eligible": alert_eligible,
        "leak_type": leak_type,
        "features": {
            "off_pump_drop_count": int(valid_drop_count),
            "usable_hours": round(float(valid_hours or 0.0), 2),
            "avg_loss_rate_pct_per_hour": round(float(consumption_rate or 0.0), 2),
            "p90_loss_rate_pct_per_hour": round(float(p90_rate), 2),
            "recent_to_baseline_ratio": round(float(rate_ratio), 2),
            "usage_change_pct": round(float(usage_change_pct), 2) if usage_change_pct is not None else None,
            "device_leak_events": int(leak_events or 0),
            "pump_runtime_hours": round(safe_float(pump_activity_metrics.get("runtime_hours"), 0), 3),
            "pump_duty_cycle_pct": round(duty_cycle_pct, 2),
            "pump_short_cycle_count": short_cycle_count,
            "avg_pump_run_seconds": int(safe_float(pump_activity_metrics.get("avg_run_seconds"), 0)),
            "sufficient_evidence": sufficient_evidence,
        },
        "reasons": reasons[:5] or ["No unusual off-pump water loss pattern was found."],
    }


def ai_leakage_alert_eligible(leakage_model):
    model = leakage_model or {}
    status = str(model.get("status") or "").strip().lower()
    confidence = safe_float(model.get("confidence"), 0)
    score = safe_float(model.get("score"), 0)
    return (
        status in {"likely_leak", "possible_leak"}
        and confidence > AI_LEAK_ALERT_MIN_CONFIDENCE
        and score > 90.0
    )


def synchronize_ai_leakage_alert(device_id, leakage_model):
    """Publish the already-computed AI leak result without recomputing analytics."""
    model = leakage_model or {}
    eligible = ai_leakage_alert_eligible(model)
    status = str(model.get("status") or "").strip().lower()
    if status == "likely_leak":
        message = "AI/ML telemetry analysis found a likely leakage pattern."
    else:
        message = "AI/ML telemetry analysis found a possible leakage pattern."
    set_alert(
        "ai_leakage",
        "warning",
        message,
        device_id=device_id,
        active=eligible,
        best_effort=True,
    )
    return eligible


def build_analysis_payload(
    *,
    quality,
    current_level,
    consumption_rate,
    empty_prediction,
    usage_change_pct,
    leak_events,
    motor_cycles,
    refill_events,
    event_analysis=None,
    leakage_model=None,
    pump_activity_metrics=None,
):
    event_analysis = event_analysis or {}
    leakage_model = leakage_model or {}
    pump_activity_metrics = pump_activity_metrics or {}
    event_counts = event_analysis.get("event_counts") or {}
    severity_counts = event_analysis.get("severity_counts") or {}
    sufficient_anomaly_evidence = bool((quality or {}).get("sufficient_for_anomaly", safe_float((quality or {}).get("score"), 0) >= 60))
    anomalies = []
    event_leak_count = sum(int(event_counts.get(kind, 0) or 0) for kind in ("pipe_leak", "slow_leak", "drip", "abnormal"))
    if leak_events > 0 or event_leak_count > 0:
        anomalies.append({"kind": "leak_signal", "severity": "danger", "message": "Leak indicators were active in this range."})
    if ai_leakage_alert_eligible(leakage_model) and leak_events <= 0 and event_leak_count <= 0:
        anomalies.append(
            {
                "kind": "ai_leakage",
                "severity": leakage_model.get("severity") or "warning",
                "message": "AI/ML telemetry pattern suggests possible leakage.",
                "score": leakage_model.get("score"),
                "confidence": leakage_model.get("confidence"),
            }
        )
    if sufficient_anomaly_evidence and usage_change_pct is not None and usage_change_pct >= 60:
        anomalies.append({"kind": "usage_spike", "severity": "danger", "message": "Usage is far above the recent baseline."})
    elif sufficient_anomaly_evidence and usage_change_pct is not None and usage_change_pct >= 25:
        anomalies.append({"kind": "usage_spike", "severity": "warning", "message": "Usage is above the recent baseline."})
    telemetry_short_cycles = int(safe_float(pump_activity_metrics.get("short_cycle_count"), 0))
    if int(event_counts.get("pump_started", 0) or 0) > 12 or (sufficient_anomaly_evidence and (motor_cycles > 12 or telemetry_short_cycles >= 4)):
        anomalies.append({"kind": "short_cycling", "severity": "warning", "message": "Pump cycling is higher than expected."})
    if int(severity_counts.get("warning", 0) or 0) >= 3:
        anomalies.append({"kind": "event_warning_pattern", "severity": "warning", "message": "Several warning events were recorded in the event history."})
    if refill_events > 0:
        anomalies.append({"kind": "refill_activity", "severity": "info", "message": "Refill events were detected in the selected range."})

    forecast_confidence = forecast_confidence_from_quality(
        quality,
        consumption_rate,
        current_level,
        leak_events=max(leak_events, event_leak_count),
    )
    if forecast_confidence >= 60 and empty_prediction is not None and empty_prediction <= 6:
        severity = "danger" if empty_prediction <= 3 else "warning"
        anomalies.append({"kind": "empty_forecast", "severity": severity, "message": "Tank may run low soon if the current trend continues."})

    return {
        "quality": quality,
        "forecast_confidence": forecast_confidence,
        "anomaly_count": len([item for item in anomalies if item.get("severity") in {"danger", "warning"}]),
        "anomalies": anomalies[:6],
        "event_window": {
            "event_count": int(event_analysis.get("event_count", 0) or 0),
            "pump_runtime_seconds": int(event_analysis.get("pump_runtime_seconds", 0) or 0),
            "telemetry_pump_runtime_seconds": int(safe_float(pump_activity_metrics.get("runtime_seconds"), 0)),
            "telemetry_pump_duty_cycle_pct": round(safe_float(pump_activity_metrics.get("duty_cycle_pct"), 0), 2),
            "telemetry_short_cycle_count": telemetry_short_cycles,
            "warning_count": int(severity_counts.get("warning", 0) or 0),
            "danger_count": int(severity_counts.get("danger", 0) or 0),
        },
        "leakage": leakage_model,
        "model": {
            "family": "coverage-aware-robust-hybrid-v2",
            "calibration": "evidence-gated",
            "signals": [
                "level_trend",
                "consumption_rate",
                "daily_baseline",
                "pump_cycles",
                "pump_runtime",
                "pump_duty_cycle",
                "leak_flags",
                "ai_leakage_score",
                "telemetry_quality",
            ],
        },
    }


def build_device_event_analysis(start_dt, end_exclusive, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT event_kind, severity, details_json, duration_seconds, event_at
        FROM device_events
        WHERE event_at >= ? AND event_at < ?
    """
    params = [start_dt.strftime(TIMESTAMP_FORMAT), end_exclusive.strftime(TIMESTAMP_FORMAT)]
    if normalized_device_id:
        query += " AND device_id = ?"
        params.append(normalized_device_id)
    query += " ORDER BY event_at ASC, id ASC"

    event_counts = {}
    severity_counts = {}
    pump_runtime_seconds = 0
    pump_runs = 0
    latest_warning = None

    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    for row in rows:
        kind = str(row["event_kind"] or "event").strip().lower() or "event"
        severity = str(row["severity"] or "info").strip().lower() or "info"
        event_counts[kind] = int(event_counts.get(kind, 0) or 0) + 1
        severity_counts[severity] = int(severity_counts.get(severity, 0) or 0) + 1
        if severity in {"warning", "danger"}:
            latest_warning = {
                "kind": kind,
                "severity": severity,
                "event_at": format_timestamp(row["event_at"]),
            }

        details = {}
        raw_details = row["details_json"]
        if raw_details:
            try:
                details = json.loads(raw_details)
            except (TypeError, ValueError):
                details = {}

        run_seconds = details.get("run_seconds")
        if run_seconds is None and kind == "pump_stopped":
            run_seconds = row["duration_seconds"]
        try:
            run_seconds = int(run_seconds) if run_seconds is not None else None
        except (TypeError, ValueError):
            run_seconds = None
        if kind == "pump_stopped" and run_seconds is not None and run_seconds > 0:
            pump_runtime_seconds += min(run_seconds, 24 * 60 * 60)
            pump_runs += 1

    return {
        "event_count": len(rows),
        "event_counts": event_counts,
        "severity_counts": severity_counts,
        "pump_runtime_seconds": int(pump_runtime_seconds),
        "pump_runtime_hours": round(pump_runtime_seconds / 3600.0, 3),
        "pump_runs": pump_runs,
        "latest_warning": latest_warning,
        "retention_days": DEVICE_EVENT_RETENTION_DAYS,
    }


def build_shared_guidance_payload(snapshot=None, analytics_payload=None):
    snapshot = snapshot or {}
    analytics_payload = analytics_payload or {}
    insights = analytics_payload.get("insights") or {}
    analysis = analytics_payload.get("analysis") or {}
    quality = analysis.get("quality") or {}
    event_analysis = analytics_payload.get("events_analysis") or {}
    pump_activity = analytics_payload.get("pump_activity") or {}
    event_counts = event_analysis.get("event_counts") or {}
    severity_counts = event_analysis.get("severity_counts") or {}
    comparison = analytics_payload.get("comparison") or {}
    leakage_model = analysis.get("leakage") or {}
    sufficient_anomaly_evidence = bool(quality.get("sufficient_for_anomaly", safe_float(quality.get("score"), 0) >= 60))

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
    ai_leak_status = str(leakage_model.get("status") or "").lower()
    ai_leak_active = ai_leakage_alert_eligible(leakage_model)
    dry_run = effective_dry_run_active(snapshot)
    source_monitoring_active = source_service == "ON"
    source_blocked = (
        source_monitoring_active
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
        title = "Pump stopped to prevent dry run" if not source_monitoring_active else "Pump locked by source tank safety"
        summary = "Dry-run protection stopped the pump to protect the motor."
        action_title = "Check the source of water before starting the pump"
        action_note = "Start the pump only after source water and inlet flow are available."
        observations.append("Dry-run protection is active.")
        actions.append("Check the source of water before starting the pump.")
    elif pipe_leak:
        severity = "warning"
        tone = "warn"
        title = "Possible leak detected"
        summary = "Leak-related signals are active and need inspection."
        action_title = "Inspect pipes and fittings now"
        action_note = "Keep watching the trend after inspection to confirm it settles."
        observations.append("Leak-related alerts are active on the device.")
        actions.append("Check pipes, valves, and overflow points for unexpected water loss.")
    elif ai_leak_active:
        severity = "critical" if ai_leak_status == "likely_leak" else "warning"
        tone = "bad" if severity == "critical" else "warn"
        if ai_leak_status == "likely_leak":
            title = "Possible Water Leak Detected"
            action_title = "Inspect pipes and taps"
        else:
            title = "Possible Water Leak Detected"
            action_title = "Check pipes and taps"
        summary = "Unusual water loss was observed while the pump was off. No motor-safety alert is active."
        action_note = "Check taps and toilet flush tanks first, then inspect visible pipes, overflow, and the pump outlet valve."
        observations.extend((leakage_model.get("reasons") or [])[:4])
        actions.extend(
            [
                "Check kitchen and bathroom taps.",
                "Check toilet flush tanks for continuous flow.",
                "Check visible pipes, tank overflow, garden lines, and the pump outlet valve.",
                "Review the leak report after the next device update.",
            ]
        )
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
    elif sufficient_anomaly_evidence and usage_change is not None and usage_change >= 35:
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
    elif quality.get("usage_physically_plausible") is False:
        severity = "warning"
        tone = "warn"
        title = "Usage history needs review"
        summary = "Live tank status is available, but historical level changes cannot support a reliable usage estimate."
        action_title = "Verify sensor stability and refill reporting"
        action_note = "Keep auto protection enabled; review sensor mounting and pump-state telemetry before using forecasts."
        observations.append("Recorded level changes exceed the water supported by observed refill cycles.")
        actions.append("Check sensor mounting, wiring, tank dimensions, and whether pump ON/OFF states are reported correctly.")
    else:
        observations.append("Tank level, pump state, and connection look steady right now.")
        actions.append("Keep auto protection enabled and review the chart trend later today.")

    motor_cycles = safe_float(insights.get("motor_cycles"), safe_float(snapshot.get("motor_cycles"), 0))
    event_pump_cycles = safe_float(event_counts.get("pump_started"), 0)
    telemetry_short_cycles = safe_float(pump_activity.get("short_cycle_count"), 0)
    if event_pump_cycles > 12 or (sufficient_anomaly_evidence and (motor_cycles > 12 or telemetry_short_cycles >= 4)):
        observations.append("Your pump is starting more often than usual.")
        actions.append("Review auto-start and auto-stop thresholds if the pump keeps short-cycling.")
    if safe_float(severity_counts.get("warning"), 0) >= 3:
        observations.append("This issue has occurred several times in the selected period.")
        actions.append("Review recent device events before changing automation settings.")

    quality_confidence = int(safe_float(quality.get("score"), 0))
    confidence = int(safe_float(analysis.get("forecast_confidence"), quality_confidence))
    direct_hardware_signal = dry_run or source_blocked or main_sensor_bad or pipe_leak
    if direct_hardware_signal and telemetry in {"live", "recent", "online"}:
        confidence = max(confidence, 85)
    elif analytics_payload:
        confidence = min(confidence, quality_confidence)
    else:
        confidence = 0
    if telemetry == "stale":
        confidence -= 20
    if not analytics_payload or not (analytics_payload.get("levels") or {}).get("values"):
        confidence -= 10
    if main_sensor_bad:
        confidence -= 8
    if quality.get("usage_physically_plausible") is False:
        confidence = min(confidence, 45)
    confidence = max(0, min(95, confidence))

    if ai_leak_active:
        confidence = max(confidence, int(safe_float(leakage_model.get("confidence"), 0)))
        confidence = min(95, confidence)

    friendly_reason_map = {
        "Device leak flags were active.": "The device reported a leak-related signal.",
        "Tank level dropped repeatedly while the pump was off.": "The tank level dropped repeatedly while the pump was off.",
        "Peak off-pump loss rate is very high.": "Water was being used even while the pump was off.",
        "Peak off-pump loss rate is above normal.": "Water use while the pump was off was above the normal range.",
        "Average consumption rate is high for the selected range.": "Water use was high during the selected period.",
        "Recent usage is much higher than the learned baseline.": "Recent water use was much higher than usual.",
        "Daily usage jumped sharply against the previous day.": "Daily water use increased sharply compared with the previous day.",
        "Daily usage is above the recent baseline.": "Daily water use was above its recent normal range.",
        "Pump activity shows repeated short runs.": "The pump started repeatedly for short periods.",
        "Pump duty cycle is high during this range.": "The pump ran for an unusually large part of this period.",
        "Telemetry quality is limited, so the model reduced confidence.": "Some device readings are missing, so this result is less reliable.",
    }
    observations = [friendly_reason_map.get(item, item) for item in observations]
    reliability_label = "High" if confidence >= 90 else "Medium" if confidence >= 70 else "Limited"
    risk_label = "High" if severity == "critical" else "Medium" if severity == "warning" else "Low"
    motor_safety = (
        "Motor protection is active; do not restart until source water is available."
        if dry_run
        else "No immediate motor-safety alert was detected."
    )
    possible_causes = (
        ["Tap left open", "Toilet flush leak", "Visible pipe or valve leak", "Tank overflow"]
        if ai_leak_active or pipe_leak
        else []
    )
    leakage_features = leakage_model.get("features") or {}
    capacity_liters = safe_float(snapshot.get("capacity_liters"), TANK_CAPACITY_LITERS)
    loss_rate_pct_per_hour = safe_float(leakage_features.get("avg_loss_rate_pct_per_hour"), 0)
    usable_loss_hours = min(24.0, max(0.0, safe_float(leakage_features.get("usable_hours"), 0)))
    estimated_abnormal_loss = None
    impact_period = None
    if (ai_leak_active or pipe_leak) and sufficient_anomaly_evidence and capacity_liters > 0 and loss_rate_pct_per_hour > 0 and usable_loss_hours > 0:
        estimated_abnormal_loss = round(capacity_liters * (loss_rate_pct_per_hour / 100.0) * usable_loss_hours, 1)
        impact_period = f"over {usable_loss_hours:g} observed hrs"
    elif sufficient_anomaly_evidence and not ai_leak_active and not pipe_leak:
        estimated_abnormal_loss = 0.0
        impact_period = "in the validated period"

    risk_reason = (
        f"High because the tank may empty in about {effective_empty:g} hours; system health and motor safety are separate checks."
        if risk_label == "High" and effective_empty is not None and effective_empty <= 3
        else "Based on water availability, active alerts, forecast urgency, and recent usage—not only device health."
    )

    return {
        "severity": severity,
        "tone": tone,
        "title": title,
        "summary": summary,
        "action_title": action_title,
        "action_note": action_note,
        "time_to_empty_hours": effective_empty,
        "confidence_percent": int(confidence),
        "reliability_label": reliability_label,
        "risk_label": risk_label,
        "risk_reason": risk_reason,
        "motor_safety": motor_safety,
        "possible_causes": possible_causes,
        "estimated_impact": {
            "water_loss_liters": estimated_abnormal_loss,
            "period": impact_period,
            "cost_inr": None,
            "message": (
                "Estimated from validated off-pump level loss."
                if estimated_abnormal_loss is not None and estimated_abnormal_loss > 0
                else "No abnormal water loss was detected in the validated period."
                if estimated_abnormal_loss == 0
                else "Not enough evidence from reliable off-pump telemetry is available to estimate abnormal loss."
            ),
        },
        "last_checked_at": snapshot.get("last_sync_at") or snapshot.get("updated_at"),
        "confidence_basis": "direct_device_signal" if direct_hardware_signal else "telemetry_quality",
        "limitations": list(quality.get("limitations") or [])[:4],
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
        "sensor_distance_cm": None,
        "water_depth_cm": None,
        "water_depth_label": "--",
        "auto_status": "Waiting for device telemetry.",
        "auto_status_tone": "warn",
        "auto_timer": "Waiting for device telemetry.",
        "tank_health": 100,
        "tank_health_status": "Healthy",
        "tank_health_reasons": ["No telemetry received yet."],
        "telemetry_status": "no-data",
        "device_id": normalize_device_id(device_id) or None,
        "firmware_version": None,
        "slave_firmware_version": None,
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
        "direct_peer_remote_mac": "",
        "direct_peer_config_channel": None,
        "direct_peer_wifi_channel": None,
        "direct_peer_last_packet_age_s": None,
        "direct_peer_last_packet_bytes": None,
        "direct_peer_last_pong_age_s": None,
        "direct_peer_last_pong_nonce": None,
        "direct_peer_sync_pending": False,
        "direct_peer_sync_channel": None,
        "direct_peer_sync_last_ok_age_s": None,
        "last_ping_target": "",
        "last_ping_status": "",
        "last_ping_response_ms": None,
        "last_ping_age_s": None,
        "last_ping_nonce": None,
        "uptime_label": "--",
        "free_heap_label": "--",
        "cpu_utilization_pct_label": "--",
        "slave_free_heap_label": "--",
        "slave_cpu_utilization_pct_label": "--",
        "slave_uptime_label": "--",
        "lower_tank_level": None,
        "lower_sensor": "DISABLED",
        "lower_sensor_info": "Lower sensor disabled",
        "lower_sensor_distance_cm": None,
        "lower_water_available_label": "--",
    }
    return apply_source_tank_aliases(payload, include_aliases=True)


def load_dashboard_snapshot(device_id=None, prefer_capacity=False):
    normalized_device_id = normalize_device_id(device_id)
    active_mode = get_device_source_mode()
    read_source = "capacity" if prefer_capacity else "legacy"
    cache_key = f"{read_source}:{active_mode}:{normalized_device_id or '__latest__'}"
    if SNAPSHOT_CACHE_TTL_SECONDS > 0:
        cached = dashboard_snapshot_cache.get(cache_key)
        if cached and (time.time() - cached["created_at"] < SNAPSHOT_CACHE_TTL_SECONDS):
            return dict(cached["payload"])
    if normalized_device_id:
        snapshot = None
        if prefer_capacity:
            try:
                with get_db() as db:
                    capacity_payload = fetch_latest_state_payload(db.cursor(), normalized_device_id, active_mode)
                snapshot = enrich_snapshot(capacity_payload) if capacity_payload else None
            except Exception as exc:
                logger.warning("Latest-state read unavailable for %s; using legacy snapshot: %s", normalized_device_id, exc)
        if snapshot is None:
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


def overlay_capacity_snapshot(summary, device_id, feature_name):
    payload = copy.deepcopy(summary or {})
    if not CAPACITY_FEATURES.enabled(feature_name):
        return payload
    snapshot = load_dashboard_snapshot(device_id, prefer_capacity=True)
    if not snapshot_has_live_device_data(snapshot):
        return payload
    public_snapshot = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    payload["snapshot"] = public_snapshot
    payload["system_status"] = build_system_status_payload(snapshot, device_id=device_id)
    payload["monitoring_summary"] = build_monitoring_summary_payload(snapshot, device_id=device_id)
    return payload


def build_dashboard_summary_payload(device_id, event_limit=30, audit_limit=30):
    """Compute a device summary off the request path (ingestion/reconciliation only)."""
    normalized_device_id = normalize_device_id(device_id)
    snapshot = load_dashboard_snapshot(normalized_device_id)
    public_snapshot = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    updated_at = now_utc().strftime(TIMESTAMP_FORMAT)
    return {
        "snapshot": public_snapshot,
        "system_status": build_system_status_payload(snapshot, device_id=normalized_device_id),
        "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=normalized_device_id),
        "events": build_events(event_limit, device_id=normalized_device_id, sync=False),
        "audit": fetch_audit_events(limit=audit_limit, device_id=normalized_device_id),
        "guidance": build_shared_guidance_payload(snapshot, None),
        "last_updated": updated_at,
        "generated_at": updated_at,
    }


def persist_dashboard_summary(device_id, payload=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    active_mode = get_device_source_mode()
    summary = payload or build_dashboard_summary_payload(normalized_device_id)
    updated_at = str(summary.get("last_updated") or now_utc().strftime(TIMESTAMP_FORMAT))
    source_updated_at = (summary.get("snapshot") or {}).get("created_at")
    encoded = json.dumps(summary, separators=(",", ":"), default=str)

    def persist_summary():
        with get_db() as db:
            db.execute(
                """
                INSERT INTO dashboard_summaries(
                    device_id, device_source, summary_json, source_updated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, device_source) DO UPDATE SET
                    summary_json=excluded.summary_json,
                    source_updated_at=excluded.source_updated_at,
                    updated_at=excluded.updated_at
                """,
                (normalized_device_id, active_mode, encoded, source_updated_at, updated_at),
            )

    run_with_database_lock_retries(
        persist_summary,
        operation_name="persist dashboard summary",
        attempts=4,
        initial_delay_s=0.25,
    )

    cache_key = f"{active_mode}:{normalized_device_id}"
    dashboard_summary_cache[cache_key] = copy.deepcopy(summary)
    with dashboard_summary_refresh_lock:
        dashboard_summary_last_refresh_at[normalized_device_id] = time.monotonic()
    return copy.deepcopy(summary)


def load_persisted_dashboard_summary(device_id):
    """Read the fast memory copy, falling back to the last durable summary."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    active_mode = get_device_source_mode()
    cache_key = f"{active_mode}:{normalized_device_id}"
    cached = dashboard_summary_cache.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    try:
        with get_db() as db:
            row = db.execute(
                """
                SELECT summary_json, updated_at
                FROM dashboard_summaries
                WHERE device_id = ? AND device_source = ?
                """,
                (normalized_device_id, active_mode),
            ).fetchone()
    except Exception as exc:
        logger.warning("Dashboard summary store unavailable for %s: %s", normalized_device_id, exc)
        return None
    if not row:
        return None
    try:
        summary = json.loads(row.get("summary_json") or "{}")
    except (TypeError, ValueError):
        logger.warning("Discarding invalid dashboard summary for %s", normalized_device_id)
        return None
    summary.setdefault("last_updated", row.get("updated_at"))
    dashboard_summary_cache[cache_key] = copy.deepcopy(summary)
    return copy.deepcopy(summary)


def empty_dashboard_summary(device_id):
    snapshot = build_empty_snapshot_payload(normalize_device_id(device_id))
    return {
        "snapshot": snapshot,
        "system_status": build_system_status_payload(snapshot, device_id=device_id),
        "monitoring_summary": {"alerts": [], "devices": [], "registered_devices": []},
        "events": [],
        "audit": [],
        "guidance": build_shared_guidance_payload(snapshot, None),
        "last_updated": None,
        "generated_at": None,
        "summary_pending": True,
    }


def refresh_dashboard_summary(device_id):
    """Best-effort summary refresh used by telemetry and event ingestion."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    try:
        invalidate_dashboard_summary_memory(normalized_device_id)
        return persist_dashboard_summary(normalized_device_id)
    except Exception as exc:
        logger.warning("Dashboard summary refresh failed for %s: %s", normalized_device_id, exc)
        return None


def schedule_dashboard_summary_refresh(device_id):
    """Coalesce bursts of event writes into one per-device summary update."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return
    # Keep tests deterministic: a daemon refresh from one test must not retain
    # monkeypatched database objects and leak work into the next test.
    if app.testing:
        refresh_dashboard_summary(normalized_device_id)
        return
    with dashboard_summary_refresh_lock:
        if normalized_device_id in dashboard_summary_refresh_pending:
            return
        last_refresh_at = dashboard_summary_last_refresh_at.get(normalized_device_id, 0.0)
        if last_refresh_at and time.monotonic() - last_refresh_at < DASHBOARD_SUMMARY_MIN_REFRESH_SECONDS:
            return
        dashboard_summary_refresh_pending.add(normalized_device_id)

    def refresh_after_event_burst():
        try:
            time.sleep(0.2)
            refresh_dashboard_summary(normalized_device_id)
        finally:
            with dashboard_summary_refresh_lock:
                dashboard_summary_refresh_pending.discard(normalized_device_id)

    threading.Thread(
        target=refresh_after_event_burst,
        name=f"dashboard-summary-{normalized_device_id}",
        daemon=True,
    ).start()


def snapshot_has_live_device_data(snapshot):
    if not snapshot:
        return False
    telemetry_status = str(snapshot.get("telemetry_status") or "").strip().lower()
    if telemetry_status in {"", "no-data", "unknown"}:
        return False
    return bool(snapshot.get("device_id"))


def snapshot_is_fresh_enough_for_runtime_sync(snapshot):
    if not snapshot_has_live_device_data(snapshot):
        return False
    telemetry_status = str(snapshot.get("telemetry_status") or "").strip().lower()
    if telemetry_status in {"stale", "offline"}:
        return False
    seconds_since_sync = snapshot.get("seconds_since_sync")
    try:
        return seconds_since_sync is None or float(seconds_since_sync) <= STALE_AFTER_SECONDS
    except (TypeError, ValueError):
        return True


STATUS_CONTRACT_VERSION = 1


def build_synchronized_status_payload(snapshot, device_id=None, service_config=None):
    snapshot = snapshot or {}
    normalized_device_id = normalize_device_id(device_id or snapshot.get("device_id"))
    config = service_config or (
        fetch_device_service_config(normalized_device_id, snapshot=snapshot)
        if normalized_device_id
        else {}
    )
    sensor_status = admin_relay_sensor_status_fields(snapshot, config)
    fresh = snapshot_is_fresh_enough_for_runtime_sync(snapshot)
    pump_running = str(
        snapshot.get("pump")
        or snapshot.get("motor")
        or snapshot.get("swt_relay")
        or snapshot.get("pump_status")
        or "OFF"
    ).strip().upper() in {"ON", "RUNNING", "ACTIVE"}
    ai_enabled = boolish_enabled(
        config.get("effective_ai_analysis_enabled", config.get("ai_analysis_enabled")),
        default=True,
    )
    return {
        "contract_version": STATUS_CONTRACT_VERSION,
        "firmware_contract_version": snapshot.get("status_contract_version"),
        "device_id": normalized_device_id,
        "observed_at": snapshot.get("last_sync_at") or snapshot.get("created_at"),
        "telemetry_status": snapshot.get("telemetry_status") or "no-data",
        "pump": {
            "state": "ON" if pump_running else "OFF",
            "mode": str(snapshot.get("mode") or "UNKNOWN").strip().upper(),
            "health": sensor_status.get("relay_status_label") or "Unreachable",
            "source": "firmware",
        },
        "sensors": {
            "upper": sensor_status.get("upper_sensor_status_label") or "Unreachable",
            "source": sensor_status.get("lower_sensor_status_label") or "Unreachable",
            "municipal": sensor_status.get("municipal_sensor_status_label") or "Disabled",
            "source_of_truth": "firmware",
        },
        "ai_ml": {
            "state": "ON" if ai_enabled else "OFF",
            "cloud_feed_mode": config.get("cloud_feed_mode") or "off",
            "firmware_state": snapshot.get("ai_ml_status") or "SERVER_MANAGED",
            "source": "flask",
        },
        "configuration": {
            "state": "SYNCED" if fresh else ("STALE" if snapshot else "WAITING"),
            "firmware_state": snapshot.get("configuration_status") or "UNKNOWN",
            "auto_mode": "ON" if boolish_enabled(config.get("auto_mode_enabled"), default=False) else "OFF",
            "source_tank": "ON" if boolish_enabled(config.get("source_tank_monitoring_enabled"), default=True) else "OFF",
            "municipal": "ON" if boolish_enabled(config.get("municipal_sensor_enabled"), default=False) else "OFF",
            "updated_at": config.get("updated_at"),
            "source": "flask+firmware",
        },
    }


def build_system_status_payload(snapshot, device_id=None, service_config=None):
    if snapshot_has_live_device_data(snapshot):
        evaluate_snapshot_alerts(snapshot)
    device_state = device_status_from_snapshot(snapshot if snapshot_has_live_device_data(snapshot) else None)
    active_alerts = fetch_active_alerts(limit=6, device_id=device_id)
    normalized_device_id = normalize_device_id(device_id or ((snapshot or {}).get("device_id") if snapshot else None))
    resolved_service_config = service_config
    if resolved_service_config is None and normalized_device_id:
        resolved_service_config = fetch_device_service_config(normalized_device_id, snapshot=snapshot)
    node_status = admin_node_status_fields(snapshot or {}, resolved_service_config or {})

    synchronized_status = build_synchronized_status_payload(
        snapshot, device_id=normalized_device_id, service_config=resolved_service_config
    )
    return {
        "server": "online",
        "database": "online",
        "device": device_state["device"],
        "device_status_code": device_state["status_code"],
        "api_version": API_VERSION,
        "swt_version": SWT_VERSION,
        "ota_contract_version": OTA_CONTRACT_VERSION,
        "ota_authorization": "artifact_hmac_sha256",
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
        "slave_firmware_version": snapshot.get("slave_firmware_version") if snapshot else None,
        "reset_reason": snapshot.get("reset_reason") if snapshot else None,
        "channel_mode": snapshot.get("channel_mode") if snapshot else "unknown",
        "telemetry_service": snapshot.get("telemetry_service") if snapshot else "UNKNOWN",
        "command_service": snapshot.get("command_service") if snapshot else "UNKNOWN",
        "ota_service": snapshot.get("ota_service") if snapshot else "UNKNOWN",
        "lower_tank_service": snapshot.get("lower_tank_service") if snapshot else "UNKNOWN",
        "buzzer_service": snapshot.get("buzzer_service") if snapshot else "UNKNOWN",
        "led_display_service": snapshot.get("led_display_service") if snapshot else "UNKNOWN",
        "local_firmware_upload_service": snapshot.get("local_firmware_upload_service") if snapshot else "UNKNOWN",
        "master_status_label": node_status.get("master_status_label"),
        "master_status_tone": node_status.get("master_status_tone"),
        "slave_status_label": node_status.get("slave_status_label"),
        "slave_status_tone": node_status.get("slave_status_tone"),
        "uptime_label": snapshot.get("uptime_label") if snapshot else "--",
        "free_heap_label": snapshot.get("free_heap_label") if snapshot else "--",
        "active_alert_count": len(active_alerts),
        "active_alerts": active_alerts,
        "synchronized_status": synchronized_status,
        # Backward-compatible aliases for clients that adopted the initial
        # cross-project status contract before the nested field was finalized.
        "synchronized_status_current_status": synchronized_status,
        "status_contract": synchronized_status,
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
            "slave_firmware_version": snapshot.get("slave_firmware_version") if snapshot else None,
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
        "connection_policy": {
            "auto_create_database": mysql_auto_create_database_enabled(),
            "connect_timeout_seconds": max(5, env_int("MYSQL_CONNECT_TIMEOUT_SECONDS", 10)),
            "read_timeout_seconds": max(10, env_int("MYSQL_READ_TIMEOUT_SECONDS", 25)),
            "write_timeout_seconds": max(10, env_int("MYSQL_WRITE_TIMEOUT_SECONDS", 25)),
            "lock_wait_timeout_seconds": max(
                1,
                min(60, env_int("MYSQL_LOCK_WAIT_TIMEOUT_SECONDS", 10)),
            ),
            "automatic_write_retries": False,
            "pool_enabled": CAPACITY_FEATURES.enabled("db_connection_pool"),
            "pool_size": max(1, min(env_int("MYSQL_POOL_SIZE", 2), 4)),
            "pool_max_overflow": max(0, min(env_int("MYSQL_POOL_MAX_OVERFLOW", 1), 2)),
            "pool_recycle_seconds": max(30, min(env_int("MYSQL_POOL_RECYCLE_SECONDS", 240), 900)),
            "pool_stats": _MYSQL_CONNECTION_POOL.stats() if _MYSQL_CONNECTION_POOL is not None else None,
        },
    }

    return {
        "database": database_payload,
        "analytics": {
            "history_enabled": TELEMETRY_HISTORY_ENABLED,
            "device_source_mode": active_mode,
            "retention_days": DATA_RETENTION_DAYS,
            "device_event_retention_days": DEVICE_EVENT_RETENTION_DAYS,
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
            "mysql_optimize_enabled": MYSQL_OPTIMIZE_ENABLED,
            "target_size_mb": DB_TARGET_SIZE_MB,
            "target_size_bytes": DB_TARGET_SIZE_BYTES,
            "min_interval_seconds": DB_MAINTENANCE_MIN_INTERVAL_SECONDS,
            "wal_autocheckpoint_pages": DB_WAL_AUTOCHECKPOINT_PAGES,
            "retention_delete_batch_rows": DB_RETENTION_DELETE_BATCH_ROWS,
            "retention_delete_max_batches": DB_RETENTION_DELETE_MAX_BATCHES,
            "retention_delete_force_max_batches": DB_RETENTION_DELETE_FORCE_MAX_BATCHES,
            "optimize_after_prune_rows": DB_OPTIMIZE_AFTER_PRUNE_ROWS,
            "temporary_size_guard_enabled": TEMP_DB_SIZE_GUARD_ENABLED,
            "temporary_hard_size_cap_enabled": TEMP_HARD_DB_CAP_ENABLED,
            "hard_size_cap_batch_rows": TEMP_HARD_DB_CAP_BATCH_ROWS,
            "hard_size_cap_max_batches": TEMP_HARD_DB_CAP_MAX_BATCHES,
            "device_command_retention_days": DEVICE_COMMAND_RETENTION_DAYS,
            "device_event_retention_days": DEVICE_EVENT_RETENTION_DAYS,
            "ops_alert_retention_days": OPS_ALERT_RETENTION_DAYS,
            "ops_audit_retention_days": OPS_AUDIT_RETENTION_DAYS,
            "last_run_at_epoch": db_maintenance_state.get("last_run_at"),
            "last_reason": db_maintenance_state.get("last_reason"),
            "last_error": db_maintenance_state.get("last_error"),
            "last_total_bytes": db_maintenance_state.get("last_total_bytes"),
            "last_action": db_maintenance_state.get("last_action"),
            "last_pruned_rows": db_maintenance_state.get("last_pruned_rows"),
            "last_tables": db_maintenance_state.get("last_tables"),
            "last_skip_at_epoch": db_maintenance_state.get("last_skip_at"),
            "last_skip_reason": db_maintenance_state.get("last_skip_reason"),
            "last_size_cap_pruned_rows": db_prune_state.get("last_size_cap_rows"),
            "last_size_cap_batches": db_prune_state.get("last_size_cap_batches"),
            "last_size_cap_remaining_pressure": db_prune_state.get("last_size_cap_remaining_pressure"),
        },
    }


def build_analytics(start_dt, end_exclusive, label, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    cache_key = build_analytics_cache_key(start_dt, end_exclusive, normalized_device_id)
    now_ts = time.time()
    cached_payload = read_cached_analytics(cache_key, now_ts=now_ts)
    if cached_payload is not None:
        return cached_payload
    # Analytics requests are read-only.  Event generation/persistence runs on
    # telemetry ingestion or the summary worker, never in a dashboard request,
    # where it could contend with device and retention writers.

    if not TELEMETRY_HISTORY_ENABLED:
        payload = build_empty_analytics(start_dt, end_exclusive, label, normalized_device_id)
        payload["alerts"] = ["Analytics history is disabled on this deployment."]
        return fallback_analytics_payload(
            cache_key,
            payload,
            reason="Analytics history is disabled; showing the last successful AI analysis if available.",
            now_ts=now_ts,
        )

    query, params = build_analytics_query(start_dt, end_exclusive, normalized_device_id)
    with get_db() as db:
        raw_tank_rows = [dict(row) for row in db.execute(query, params)]
    observed_motor_times, observed_motor_values = build_observed_motor_activity_series(raw_tank_rows)
    tank_rows = filter_valid_analytics_rows(raw_tank_rows)
    history_source = "tank_data"
    if len(tank_rows) < 2 and normalized_device_id:
        all_source_query, all_source_params = build_analytics_query(
            start_dt,
            end_exclusive,
            normalized_device_id,
            include_all_sources=True,
        )
        with get_db() as db:
            raw_all_source_rows = [dict(row) for row in db.execute(all_source_query, all_source_params)]
            all_source_rows = filter_valid_analytics_rows(raw_all_source_rows)
        if len(all_source_rows) > len(tank_rows):
            tank_rows = all_source_rows
            observed_motor_times, observed_motor_values = build_observed_motor_activity_series(raw_all_source_rows)
            history_source = "tank_data_all_sources"
    existing_source_row_ids = {str(row.get("id")) for row in tank_rows if row.get("id") not in (None, "")}
    event_rows = []
    if len(tank_rows) < 2:
        event_rows = fetch_event_analytics_rows(
            start_dt,
            end_exclusive,
            device_id=normalized_device_id,
            existing_source_row_ids=existing_source_row_ids,
        )
        if event_rows:
            history_source = "device_events"
    analytics_rows = merge_analytics_source_rows(tank_rows, event_rows)
    gap_threshold_hours = ANALYTICS_MAX_GAP_MINUTES / 60.0
    daily_usage = {}
    level_times = []
    level_values = []
    fill_level_values = []
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
    valid_drop_count = 0
    gap_count = 0
    refill_events = 0
    consumption_rate_segments = []
    prev_created_at = None
    first_created_at = None
    prev_level = None
    last_display_level = None
    latest_row = None

    for row in analytics_rows:
        created_at = parse_timestamp(row["created_at"])
        if created_at is None:
            continue

        if first_created_at is None:
            first_created_at = created_at

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
            if gap_break:
                gap_count += 1

        drop = 0.0 if prev_level is None else level - prev_level
        implausible_level_jump = prev_level is not None and abs(drop) > ANALYTICS_MAX_LEVEL_DELTA_PCT
        level_times.append(timestamp_label)
        if gap_break or implausible_level_jump:
            level_values.append(last_display_level if last_display_level is not None else level)
        else:
            level_values.append(level)
            last_display_level = level
        fill_level_values.append(level)
        if pipe_leak == "YES":
            leak_events += 1

        latest_row = dict(row)
        latest_row["created_at"] = created_at
        latest_row["level"] = level
        latest_row["motor"] = motor
        latest_row["pipe_leak"] = pipe_leak

        prev_created_at = created_at
        prev_level = level

    if row_count < 2 or latest_row is None or prev_level is None:
        payload = build_empty_analytics(start_dt, end_exclusive, label, normalized_device_id)
        return fallback_analytics_payload(
            cache_key,
            payload,
            reason="Fresh analytics needs more history; showing the last successful AI analysis if available.",
            now_ts=now_ts,
        )

    automation_settings = fetch_device_automation_settings(
        normalized_device_id,
        snapshot=latest_row,
    ) if normalized_device_id else default_device_automation_settings()
    stop_threshold_pct = safe_float(
        automation_settings.get("auto_stop_pct"),
        DEFAULT_DEVICE_AUTO_STOP_PCT,
    )
    start_threshold_pct = safe_float(
        automation_settings.get("auto_start_pct"),
        DEFAULT_DEVICE_AUTO_START_PCT,
    )
    inferred_motor_times, inferred_motor_values, inferred_fill_runs = infer_pump_activity_from_level_history(
        level_times,
        fill_level_values,
        stop_threshold_pct=stop_threshold_pct,
        start_threshold_pct=start_threshold_pct,
    )
    level_usage = estimate_level_history_usage(level_times, level_values, inferred_motor_values)
    daily_usage = level_usage["daily_usage"]
    hourly_timeline = level_usage["hourly_timeline"]
    total_usage = level_usage["total_usage"]
    valid_hours = level_usage["valid_hours"]
    valid_drop_count = level_usage["valid_drop_count"]
    consumption_rate_segments = level_usage["consumption_rate_segments"]
    activity_window_end = min(end_exclusive, now_utc())
    observed_motor_times, observed_motor_values = extend_ongoing_motor_activity(
        observed_motor_times,
        observed_motor_values,
        activity_window_end,
    )
    observed_motor_metrics = build_motor_activity_metrics(observed_motor_times, observed_motor_values)
    authoritative_pump_metrics = build_authoritative_pump_metrics(raw_tank_rows)
    observed_motor_has_on_state = any(value == 1 for value in observed_motor_values)
    inferred_motor_metrics = build_motor_activity_metrics(inferred_motor_times, inferred_motor_values)
    if observed_motor_has_on_state:
        motor_times = observed_motor_times
        motor_values = observed_motor_values
        motor_cycles = observed_motor_metrics["started_runs"]
    else:
        motor_times = inferred_motor_times
        motor_values = inferred_motor_values
        motor_cycles = inferred_motor_metrics["completed_runs"]
    refill_events = motor_cycles

    mean_consumption_rate = total_usage / valid_hours if valid_hours > 0 else 0.0
    consumption_rate = robust_consumption_rate(
        mean_consumption_rate,
        consumption_rate_segments,
        valid_hours,
        valid_drop_count,
    )
    current_level = float(prev_level)
    empty_prediction = current_level / consumption_rate if consumption_rate > 0 else None
    daily_dates = list(daily_usage.keys())
    daily_values = list(daily_usage.values())
    configured_capacity = safe_float(latest_row.get("tank_capacity_liters"), None)
    if configured_capacity is None or configured_capacity <= 0:
        service_config = fetch_device_service_config(normalized_device_id) if normalized_device_id else {}
        configured_capacity = safe_float(service_config.get("tank_capacity_liters"), TANK_CAPACITY_LITERS)
    tank_capacity_liters = configured_capacity if configured_capacity and configured_capacity > 0 else TANK_CAPACITY_LITERS
    def percent_to_liters(value):
        if value is None:
            return None
        return round((float(value) * tank_capacity_liters) / 100.0, 1)
    daily_liters = [percent_to_liters(value) for value in daily_values]
    daily_complete = []
    completed_through = min(end_exclusive, now_utc())
    pattern_times = []
    pattern_values = []
    pattern_cursor = start_dt.replace(minute=0, second=0, microsecond=0)
    while pattern_cursor < completed_through and len(pattern_times) < 24 * 62:
        pattern_key = pattern_cursor.strftime(TIMESTAMP_FORMAT)
        pattern_times.append(pattern_key)
        pattern_values.append(float(hourly_timeline.get(pattern_key, 0.0)))
        pattern_cursor += timedelta(hours=1)
    pattern_liters = [percent_to_liters(value) for value in pattern_values]
    for date_text in daily_dates:
        day_start = datetime.strptime(date_text, DATE_ONLY_FORMAT)
        daily_complete.append(start_dt <= day_start and completed_through >= day_start + timedelta(days=1))
    complete_usage = {
        date_text: daily_usage[date_text]
        for date_text, is_complete in zip(daily_dates, daily_complete)
        if is_complete
    }
    comparison_usage = complete_usage or daily_usage
    comparison_dates = list(comparison_usage.keys())
    comparison_values = list(comparison_usage.values())

    peak_day = max(comparison_usage, key=comparison_usage.get) if comparison_usage else "--"
    lowest_day = min(comparison_usage, key=comparison_usage.get) if comparison_usage else "--"
    peak_value = float(comparison_usage.get(peak_day, 0.0)) if comparison_usage else 0.0
    lowest_value = float(comparison_usage.get(lowest_day, 0.0)) if comparison_usage else 0.0
    avg_daily_usage = float(sum(comparison_values) / len(comparison_values)) if comparison_values else 0.0

    latest_day = str(comparison_dates[-1]) if comparison_dates else "--"
    previous_day = str(comparison_dates[-2]) if len(comparison_dates) >= 2 else "--"
    latest_day_usage = float(comparison_values[-1]) if comparison_values else 0.0
    previous_day_usage = float(comparison_values[-2]) if len(comparison_values) >= 2 else 0.0
    if previous_day_usage >= ANALYTICS_MIN_BASELINE_USAGE_PCT:
        usage_change_pct = ((latest_day_usage - previous_day_usage) / previous_day_usage) * 100
    else:
        usage_change_pct = None

    latest_row["seconds_since_sync"] = max(0, int((now_utc() - latest_row["created_at"]).total_seconds()))
    pump_activity_metrics = authoritative_pump_metrics or (observed_motor_metrics if observed_motor_has_on_state else inferred_motor_metrics)
    if authoritative_pump_metrics:
        motor_cycles = authoritative_pump_metrics["started_runs"]
        refill_events = motor_cycles
    pump_activity_metrics.update(
        {
            "source": "firmware_runtime_counter" if authoritative_pump_metrics else ("telemetry_relay_state" if observed_motor_has_on_state else "tank_level_history"),
            "relay_state_used": bool(observed_motor_has_on_state and not authoritative_pump_metrics),
            "inference_status": (
                "observed_relay_transitions"
                if observed_motor_has_on_state
                else "validated_threshold_cycles" if inferred_fill_runs else "no_threshold_reaching_fill_cycles"
            ),
            "runtime_basis": "persistent_physical_feedback_counter" if authoritative_pump_metrics else ("reported_motor_on_intervals" if observed_motor_has_on_state else "local_minimum_to_90_pct_threshold"),
            "stop_threshold_pct": round(stop_threshold_pct, 1),
            "last_started_at": inferred_fill_runs[-1]["started_at"] if inferred_fill_runs else None,
            "last_stopped_at": inferred_fill_runs[-1]["stopped_at"] if inferred_fill_runs else None,
            "validated_runs": inferred_fill_runs,
        }
    )
    quality_window_end = min(end_exclusive, now_utc())
    quality_window_start = max(start_dt, first_created_at or start_dt)
    analytics_quality = build_analytics_quality_payload(
        row_count=row_count,
        valid_hours=valid_hours,
        gap_count=gap_count,
        valid_drop_count=valid_drop_count,
        latest_seconds_since_sync=latest_row["seconds_since_sync"],
        # Do not count days before a newly installed device's first telemetry as
        # missing coverage. Gaps after the first reading remain fully penalized.
        window_hours=max(0.0, (quality_window_end - quality_window_start).total_seconds() / 3600.0),
    )
    # A level-derived usage total cannot reliably exceed the water made available by
    # the opening tank plus observed refill cycles. Large excesses usually indicate
    # sensor bounce (repeated false drops and recoveries), not real consumption.
    usage_physically_plausible, plausible_usage_limit_pct = level_usage_matches_observed_refills(
        total_usage, motor_cycles
    )
    daily_turnover_plausible, daily_turnover_limit_pct, peak_daily_usage_pct = (
        daily_usage_matches_tank_turnover_limit(daily_usage)
    )
    usage_physically_plausible = bool(usage_physically_plausible and daily_turnover_plausible)
    complete_day_count = len(complete_usage)
    usage_rate_reliable = bool(
        usage_physically_plausible and analytics_quality.get("sufficient_for_anomaly")
    )
    daily_usage_reliable = bool(
        usage_physically_plausible
        and analytics_quality.get("sufficient_for_forecast")
        and complete_day_count >= 2
    )
    analytics_quality["usage_physically_plausible"] = usage_physically_plausible
    analytics_quality["usage_plausibility_limit_pct"] = round(plausible_usage_limit_pct, 1)
    analytics_quality["daily_turnover_plausible"] = daily_turnover_plausible
    analytics_quality["daily_turnover_limit_pct"] = daily_turnover_limit_pct
    analytics_quality["peak_daily_usage_pct"] = peak_daily_usage_pct
    analytics_quality["complete_day_count"] = complete_day_count
    analytics_quality["usage_rate_reliable"] = usage_rate_reliable
    analytics_quality["daily_usage_reliable"] = daily_usage_reliable
    if not usage_physically_plausible:
        analytics_quality["sufficient_for_forecast"] = False
        analytics_quality["sufficient_for_anomaly"] = False
        analytics_quality["status"] = "limited"
        analytics_quality.setdefault("limitations", []).append(
            "Level changes exceed the volume supported by observed refill cycles; daily usage is hidden as unreliable."
        )
        if not daily_turnover_plausible:
            analytics_quality.setdefault("limitations", []).append(
                "Daily level changes exceed the configured tank-turnover safety limit."
            )
    if complete_day_count < 2:
        analytics_quality.setdefault("limitations", []).append(
            "At least two complete days are required for average daily usage."
        )
    # Keep the observed day-over-day estimate available for display even when
    # validation is limited. Reliability remains explicit in the quality and
    # comparison flags, and alert generation still uses the quality gates.
    usage_forecast = build_daily_usage_forecast(comparison_values, analytics_quality)
    leakage_model = build_leakage_ai_model(
        leak_events=leak_events,
        consumption_rate=consumption_rate,
        consumption_rate_segments=consumption_rate_segments,
        usage_change_pct=usage_change_pct,
        motor_cycles=motor_cycles,
        refill_events=refill_events,
        valid_hours=valid_hours,
        valid_drop_count=valid_drop_count,
        quality=analytics_quality,
        pump_activity_metrics=pump_activity_metrics,
    )
    synchronize_ai_leakage_alert(latest_row.get("device_id"), leakage_model)
    health = calculate_health(
        snapshot=latest_row,
        leak_events=leak_events,
        motor_cycles=motor_cycles,
        consumption_rate=consumption_rate,
    )

    alerts = []
    if leak_events > 0:
        alerts.append("Possible pipe leak detected in the selected period.")
    elif leakage_model.get("status") == "likely_leak" and ai_leakage_alert_eligible(leakage_model):
        alerts.append("AI/ML model found a likely leakage pattern in tank level history.")
    elif leakage_model.get("status") == "possible_leak" and ai_leakage_alert_eligible(leakage_model):
        alerts.append("AI/ML model found a possible leakage pattern. Inspect pipes and taps.")
    if analytics_quality.get("sufficient_for_anomaly") and consumption_rate > 15:
        alerts.append("Water consumption is above the usual range.")
    if analytics_quality.get("sufficient_for_forecast") and empty_prediction is not None and empty_prediction < 6:
        alerts.append("Tank may empty within the next 6 hours.")
    if analytics_quality.get("sufficient_for_anomaly") and (motor_cycles > 12 or int(pump_activity_metrics.get("short_cycle_count", 0) or 0) >= 4):
        alerts.append("Motor is cycling frequently. Check automation thresholds.")
    if latest_row["seconds_since_sync"] > STALE_AFTER_SECONDS:
        alerts.append("Live telemetry looks stale. Check device connectivity.")
    if not alerts and analytics_quality.get("status") == "limited":
        alerts.append("More continuous telemetry is needed before AI/ML conclusions are reliable.")
    elif not alerts:
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
            "avg_daily_usage_liters": percent_to_liters(avg_daily_usage),
            "peak_usage_day": peak_day,
            "peak_usage_value": round(peak_value, 2),
            "peak_usage_liters": percent_to_liters(peak_value),
            "lowest_usage_day": lowest_day,
            "lowest_usage_value": round(lowest_value, 2),
            "lowest_usage_liters": percent_to_liters(lowest_value),
            "latest_day_usage": round(latest_day_usage, 2),
            "latest_day_usage_liters": percent_to_liters(latest_day_usage),
            "previous_day_usage": round(previous_day_usage, 2),
            "previous_day_usage_liters": percent_to_liters(previous_day_usage),
            "usage_change_pct": round(float(usage_change_pct), 2) if usage_change_pct is not None else None,
        },
        "health": health,
        "daily": {
            "dates": daily_dates,
            "values": [round(float(value), 2) for value in daily_values],
            "liters": daily_liters,
            "unit": "L",
            "measurement": "estimated_from_level_change",
            "complete": daily_complete,
            "reliable": daily_usage_reliable,
        },
        "pattern": {
            "time": pattern_times,
            "values": [round(float(value), 2) for value in pattern_values],
            "liters": pattern_liters,
            "unit": "L",
            "measurement": "estimated_from_level_change",
            "aggregation": "hourly_selected_range",
        },
        "levels": {
            "time": level_times,
            "values": [round(float(value), 2) if value is not None else None for value in level_values],
        },
        "motor": {
            "time": motor_times,
            "values": [int(value) if value is not None else None for value in motor_values],
            "source": "tank_level_history",
            "relay_state_used": False,
        },
        "pump_activity": pump_activity_metrics,
        "comparison": {
            "latest_day": latest_day,
            "latest_day_usage": round(latest_day_usage, 2),
            "latest_day_usage_liters": percent_to_liters(latest_day_usage),
            "previous_day": previous_day,
            "previous_day_usage": round(previous_day_usage, 2),
            "previous_day_usage_liters": percent_to_liters(previous_day_usage),
            "change_pct": round(float(usage_change_pct), 2) if usage_change_pct is not None else None,
            "reliable": daily_usage_reliable,
        },
        "prediction": {
            "tomorrow_usage": usage_forecast["value"],
            "tomorrow_usage_liters": percent_to_liters(usage_forecast["value"]),
            "lower_usage": usage_forecast["lower"],
            "upper_usage": usage_forecast["upper"],
            "lower_usage_liters": percent_to_liters(usage_forecast["lower"]),
            "upper_usage_liters": percent_to_liters(usage_forecast["upper"]),
            "confidence": usage_forecast["confidence"],
            "sample_days": usage_forecast["sample_days"],
            "status": usage_forecast["status"],
            "method": usage_forecast["method"],
            "limitations": usage_forecast["limitations"],
        },
        "usage": {
            "unit": "L",
            "tank_capacity_liters": round(tank_capacity_liters, 1),
            "measurement": "estimated_from_level_change",
            "reliable": daily_usage_reliable,
        },
        "alerts": alerts,
    }
    event_analysis = build_device_event_analysis(start_dt, end_exclusive, device_id=normalized_device_id)
    payload["events_analysis"] = event_analysis
    payload["analysis"] = build_analysis_payload(
        quality=analytics_quality,
        current_level=current_level,
        consumption_rate=consumption_rate,
        empty_prediction=empty_prediction,
        usage_change_pct=usage_change_pct,
        leak_events=leak_events,
        motor_cycles=motor_cycles,
        refill_events=refill_events,
        event_analysis=event_analysis,
        leakage_model=leakage_model,
        pump_activity_metrics=pump_activity_metrics,
    )
    payload["analysis"]["live_snapshot_fallback"] = False
    payload["analysis"]["history_source"] = history_source
    payload["analysis"]["tank_data_raw_row_count"] = len(raw_tank_rows)
    payload["analysis"]["tank_data_row_count"] = len(tank_rows)
    payload["analysis"]["event_history_row_count"] = len(event_rows)
    if normalized_device_id:
        guidance_snapshot = fetch_device_snapshot(normalized_device_id) or latest_row
    else:
        guidance_snapshot = latest_row
    payload["latest_sync_at"] = format_timestamp(latest_row.get("created_at")) if latest_row else None
    payload["guidance"] = build_shared_guidance_payload(guidance_snapshot, payload)

    return store_cached_analytics(cache_key, payload, now_ts=now_ts)


AI_INSIGHT_KEYS = {
    "empty_prediction",
    "consumption_rate",
    "avg_daily_usage",
    "avg_daily_usage_liters",
    "peak_usage_day",
    "peak_usage_value",
    "peak_usage_liters",
    "lowest_usage_day",
    "lowest_usage_value",
    "lowest_usage_liters",
    "latest_day_usage",
    "latest_day_usage_liters",
    "previous_day_usage",
    "previous_day_usage_liters",
    "usage_change_pct",
}


def fixed_ai_analysis_window(now=None):
    current = now_utc() if now is None else now
    current_ist = current.replace(tzinfo=timezone.utc).astimezone(IST_TIMEZONE)
    start_ist = current_ist.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    start_dt = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
    return start_dt, current + timedelta(seconds=1), "Last 7 days"


def build_cached_fixed_ai_analytics(device_id=None):
    ai_start, ai_end, ai_label = fixed_ai_analysis_window()
    cache_key = (
        normalize_device_id(device_id) or "*",
        ai_start.strftime(DATE_ONLY_FORMAT),
        get_device_source_mode(),
        ANALYTICS_ALGORITHM_VERSION,
    )
    current_time = time.time()
    cached = fixed_ai_dashboard_cache.get(cache_key)
    if cached and current_time - float(cached.get("created_at") or 0.0) < FIXED_AI_CACHE_TTL_SECONDS:
        return copy_analytics_payload(cached.get("payload")), ai_start, ai_end, ai_label
    payload = build_analytics(ai_start, ai_end, ai_label, device_id=device_id)
    fixed_ai_dashboard_cache[cache_key] = {
        "created_at": current_time,
        "payload": copy_analytics_payload(payload),
    }
    # Retain a small per-device cache so one customer's request does not evict
    # every other customer's seven-day analysis.
    if len(fixed_ai_dashboard_cache) > 32:
        oldest_key = min(
            fixed_ai_dashboard_cache,
            key=lambda key: float(fixed_ai_dashboard_cache[key].get("created_at") or 0.0),
        )
        fixed_ai_dashboard_cache.pop(oldest_key, None)
    return payload, ai_start, ai_end, ai_label


def build_dashboard_analytics(start_dt, end_exclusive, label, device_id=None):
    """Build selected-range charts with a fixed seven-day AI/ML window."""
    selected = copy_analytics_payload(build_analytics(start_dt, end_exclusive, label, device_id=device_id))
    ai_start, ai_end, ai_label = fixed_ai_analysis_window()
    selected_is_ai_window = (
        start_dt.strftime(DATE_ONLY_FORMAT) == ai_start.strftime(DATE_ONLY_FORMAT)
        and end_exclusive.strftime(DATE_ONLY_FORMAT) == ai_end.strftime(DATE_ONLY_FORMAT)
    )
    if selected_is_ai_window:
        ai_payload = selected
    else:
        ai_payload, ai_start, ai_end, ai_label = build_cached_fixed_ai_analytics(device_id=device_id)
    selected["analysis_window"] = {
        "label": ai_label,
        "start_date": ai_start.strftime(DATE_ONLY_FORMAT),
        "end_date": (ai_end - timedelta(seconds=1)).strftime(DATE_ONLY_FORMAT),
        "days": 7,
        "fixed": True,
    }
    selected["chart_quality"] = copy_analytics_payload(((selected.get("analysis") or {}).get("quality") or {}))
    selected_insights = selected.setdefault("insights", {})
    ai_insights = (ai_payload or {}).get("insights") or {}
    for key in AI_INSIGHT_KEYS:
        if key in ai_insights:
            selected_insights[key] = copy.deepcopy(ai_insights[key])
    for key in ("prediction", "analysis", "alerts", "guidance", "comparison", "usage", "events_analysis"):
        if key in (ai_payload or {}):
            selected[key] = copy_analytics_payload(ai_payload[key])
    selected["ai_daily"] = copy_analytics_payload((ai_payload or {}).get("daily") or {})
    selected["analytics_version"] = analytics_payload_version(selected)
    # Bind analytics to the physical device whose firmware telemetry produced
    # it so every client can reject stale data after an account/device switch.
    selected["device_id"] = str(device_id or "").strip()
    selected["sync_contract"] = {
        "version": 1,
        "device_id": str(device_id or "").strip(),
        "telemetry_authority": "firmware",
        "analytics_authority": "flask",
        "control_authority": "firmware",
        "analytics_generated_at": selected.get("analytics_generated_at"),
    }
    return selected


def build_dashboard_analytics_singleflight(start_dt, end_exclusive, label, device_id=None):
    """Serialize expensive builds for the same device and date window."""
    cache_key = build_analytics_cache_key(start_dt, end_exclusive, device_id)
    with analytics_build_lock(cache_key):
        # build_analytics re-checks its cache after this lock is acquired, so
        # queued requests reuse the first request's result.
        return build_dashboard_analytics(start_dt, end_exclusive, label, device_id=device_id)


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


TELEMETRY_SYNC_FINGERPRINT_FIELDS = (
    "device_id",
    "device_source",
    "level",
    "motor",
    "mode",
    "leak",
    "pump_failure",
    "abnormal",
    "drip",
    "slow_leak",
    "pipe_leak",
    "dry_run",
    "wifi",
    "sensor",
    "sensor_info",
    "sensor_distance_cm",
    "tank_height_cm",
    "tank_capacity_liters",
    "auto_start_pct",
    "auto_stop_pct",
    "auto_start_stable_ms",
    "auto_level_average_samples",
    "auto_status",
    "auto_status_tone",
    "auto_timer",
    "tank_health",
    "lower_tank_level",
    "lower_sensor",
    "lower_sensor_info",
    "lower_sensor_distance_cm",
    "municipal_sensor_enabled",
    "municipal_sensor_state",
    "municipal_sensor_simulated",
    "municipal_sensor_reachable",
    "municipal_sensor_last_updated",
    "municipal_valve_enabled",
    "municipal_valve_feature_enabled",
    "municipal_valve_hardware_ready",
    "municipal_valve_simulated",
    "municipal_valve_state",
    "municipal_valve_route",
    "source_pump_fill_feature_enabled",
    "source_pump_fill_active",
    "source_outlet_valve_state",
    "source_outlet_route",
    "source_outlet_valve_simulated",
    "inlet_valve_route",
    "inlet_valve_state",
    "municipal_detection_mode",
    "municipal_trial_locked_to_source",
    "upper_tank_count",
    "source_tank_count",
    "water_supply_plan",
    "turbidity_monitoring_enabled",
    "upper_turbidity_sensor",
    "lower_turbidity_sensor",
    "upper_turbidity_raw_adc",
    "lower_turbidity_raw_adc",
    "upper_turbidity_estimated_ntu",
    "lower_turbidity_estimated_ntu",
    "upper_turbidity_simulated",
    "lower_turbidity_simulated",
    "firmware_version",
    "slave_firmware_version",
    "reset_reason",
    "device_local_url",
    "channel_mode",
    "telemetry_service",
    "command_service",
    "ota_service",
    "lower_tank_service",
    "buzzer_service",
    "led_display_service",
    "local_firmware_upload_service",
    "arch_id",
    "node_role",
    "device_type",
    "direct_peer",
    "direct_peer_remote_ip",
    "direct_peer_remote_mac",
    "direct_peer_config_channel",
    "direct_peer_wifi_channel",
    "direct_peer_sync_pending",
    "direct_peer_sync_channel",
)


def normalize_telemetry_fingerprint_value(value):
    if value in (None, "", "null"):
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        normalized = f"{value:.3f}".rstrip("0").rstrip(".")
        return normalized or "0"

    text = str(value).strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return "1" if lowered == "true" else "0"
    try:
        numeric = float(text)
        if math.isfinite(numeric) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
            normalized = f"{numeric:.3f}".rstrip("0").rstrip(".")
            return normalized or "0"
    except (TypeError, ValueError):
        pass
    return lowered


def build_telemetry_sync_fingerprint(cleaned):
    if not isinstance(cleaned, dict):
        return ""
    fingerprint_source = "|".join(
        f"{field}={normalize_telemetry_fingerprint_value(cleaned.get(field))}"
        for field in TELEMETRY_SYNC_FINGERPRINT_FIELDS
    ).strip("|")
    if not fingerprint_source:
        return ""
    return hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()


def telemetry_sync_is_local_transport(transport=None, source_ip=None):
    normalized_transport = str(transport or "").strip().lower()
    normalized_source = str(source_ip or "").strip().lower()
    return normalized_transport in LOCAL_SYNC_TRANSPORTS or normalized_source in LOCAL_SYNC_TRANSPORTS


def telemetry_sync_is_recent_duplicate(device_id, fingerprint, transport=None, source_ip=None):
    if not telemetry_sync_is_local_transport(transport=transport, source_ip=source_ip):
        return False
    normalized_device_id = normalize_device_id(device_id)
    normalized_fingerprint = str(fingerprint or "").strip()
    if not normalized_device_id or not normalized_fingerprint:
        return False

    with get_db() as db:
        row = db.execute(
            """
            SELECT created_at
            FROM tank_data
            WHERE device_id = ?
              AND telemetry_fingerprint = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id, normalized_fingerprint),
        ).fetchone()

    if not row:
        return False

    created_at = parse_timestamp(row["created_at"])
    if created_at is None:
        return False
    age_seconds = max(0, int((now_utc() - created_at).total_seconds()))
    return age_seconds <= TELEMETRY_LOCAL_SYNC_DEDUPE_WINDOW_SECONDS


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
        "Estimated Daily Water Use",
        (payload.get("daily") or {}).get("dates") or [],
        (payload.get("daily") or {}).get("liters") or [],
        "L",
        label_builder=lambda _x, y: format_analytics_csv_value(y),
    )
    append_series(
        "hourly_water_pattern",
        "Estimated Hourly Water Use",
        (payload.get("pattern") or {}).get("time") or [],
        (payload.get("pattern") or {}).get("liters") or [],
        "L",
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


def build_generated_device_events(limit=12, device_id=None, include_pair=True, row_limit=None):
    if not TELEMETRY_HISTORY_ENABLED:
        return []

    normalized_device_id = normalize_device_id(device_id)
    requested_limit = max(1, int(limit or 12))
    try:
        requested_row_limit = int(row_limit) if row_limit is not None else requested_limit * 4
    except (TypeError, ValueError):
        requested_row_limit = requested_limit * 4
    telemetry_row_limit = max(20, min(240, requested_row_limit))
    if include_pair and normalized_device_id:
        activity_device_ids = paired_activity_device_ids(normalized_device_id)
        if len(activity_device_ids) > 1:
            events = []
            for activity_device_id in activity_device_ids:
                events.extend(
                    build_generated_device_events(
                        limit=limit,
                        device_id=activity_device_id,
                        include_pair=False,
                        row_limit=telemetry_row_limit,
                    )
                )
            combined_limit = requested_limit
            return sorted(events, key=lambda event: str(event.get("time") or ""), reverse=True)[:combined_limit]

    source_clause, source_params = device_source_where_clause()
    query = """
        SELECT id, device_id, level, motor, mode, pipe_leak, slow_leak, drip, abnormal,
               pump_failure, dry_run, sensor, wifi, wifi_rssi, firmware_version, slave_firmware_version,
               reset_reason, free_heap, uptime_s, lower_tank_level, lower_sensor,
               municipal_sensor_enabled, municipal_sensor_state, municipal_sensor_simulated,
               municipal_sensor_reachable, municipal_valve_simulated, municipal_valve_feature_enabled,
               source_outlet_valve_simulated, source_pump_fill_feature_enabled,
               lower_turbidity_simulated, upper_turbidity_simulated,
               channel_mode, telemetry_service, command_service, ota_service,
               lower_tank_service, buzzer_service, led_display_service,
               local_firmware_upload_service, tank_height_cm, tank_capacity_liters,
               node_role, device_type,
               direct_peer, direct_peer_remote_ip, direct_peer_remote_mac,
               direct_peer_config_channel, direct_peer_wifi_channel,
               direct_peer_last_packet_age_s, direct_peer_last_packet_bytes,
               direct_peer_last_sequence, direct_peer_duplicate_packets,
               direct_peer_out_of_order_packets, direct_peer_estimated_lost_packets,
               direct_peer_last_pong_age_s, direct_peer_last_pong_nonce,
               direct_peer_sync_pending, direct_peer_sync_channel,
               direct_peer_sync_last_ok_age_s,
               last_ping_target, last_ping_status, last_ping_response_ms,
               last_ping_age_s, last_ping_nonce,
               controller_state, upper_high_float_enabled, upper_high_float_active,
               source_low_float_enabled, source_low_float_active,
               created_at
        FROM tank_data
        WHERE 
    """
    query += source_clause
    params = list(source_params)
    if normalized_device_id:
        query += " AND device_id = ?"
        params.append(normalized_device_id)
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(telemetry_row_limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    timeline = []
    def row_with_event_telemetry_status(row):
        current = dict(row or {})
        if str(current.get("telemetry_status") or "").strip():
            return current
        created_at = parse_timestamp(current.get("created_at"))
        if created_at is None:
            current["telemetry_status"] = "no-data"
            return current
        age_seconds = max(0, int((now_utc() - created_at).total_seconds()))
        current["telemetry_status"] = telemetry_status(age_seconds)
        return current

    latest_snapshot_row = fetch_device_snapshot(normalized_device_id) if normalized_device_id else None
    if snapshot_has_live_device_data(latest_snapshot_row):
        latest_peer_row = dict(latest_snapshot_row)
    else:
        latest_peer_row = row_with_event_telemetry_status(rows[0]) if rows else None
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
    event_service_config = fetch_device_service_config(normalized_device_id) if normalized_device_id else {}

    def add_event(current, severity, message, kind, details=None, event_time=None):
        telemetry_details = {
            "main_tank_level": current.get("level"),
            "source_tank_level": current.get("lower_tank_level"),
            "motor_state": current.get("motor"),
            "mode": current.get("mode"),
            "main_sensor_state": current.get("sensor"),
            "source_sensor_state": current.get("lower_sensor"),
            "wifi_state": current.get("wifi"),
            "wifi_rssi": current.get("wifi_rssi"),
            "municipal_sensor_enabled": bool_flag(current.get("municipal_sensor_enabled")),
            "municipal_sensor_state": current.get("municipal_sensor_state"),
            "municipal_sensor_reachable": bool_flag(current.get("municipal_sensor_reachable")),
            "municipal_sensor_simulated": bool_flag(current.get("municipal_sensor_simulated")),
            "motorized_valve_enabled": bool_flag(current.get("municipal_valve_feature_enabled")),
            "motorized_valve_simulated": bool_flag(current.get("municipal_valve_simulated")),
            "source_outlet_valve_enabled": bool_flag(current.get("source_pump_fill_feature_enabled")),
            "source_outlet_valve_simulated": bool_flag(current.get("source_outlet_valve_simulated")),
            "lower_turbidity_simulated": bool_flag(current.get("lower_turbidity_simulated")),
            "upper_turbidity_simulated": bool_flag(current.get("upper_turbidity_simulated")),
        }
        timeline.append(
            {
                "time": format_timestamp(event_time or current.get("created_at")),
                "severity": severity,
                "message": message,
                "kind": kind,
                "details": {
                    "source_table": "tank_data",
                    "source_row_id": current.get("id"),
                    "device_id": normalize_device_id(current.get("device_id")),
                    "node_role": current.get("node_role"),
                    "device_type": current.get("device_type"),
                    **telemetry_details,
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

    def peer_channel_label(value):
        try:
            channel = int(value)
        except (TypeError, ValueError):
            return "--"
        return str(channel) if 1 <= channel <= 13 else "--"

    def peer_packet_age(current):
        try:
            return int(current.get("direct_peer_last_packet_age_s"))
        except (TypeError, ValueError):
            return None

    def peer_link_state(current):
        if str(current.get("direct_peer") or "").strip().lower() in {"", "disabled", "off"}:
            return "disabled"
        age = peer_packet_age(current)
        if age is None or age < 0:
            return "waiting"
        if age <= DIRECT_PEER_STALE_AFTER_SECONDS:
            return "reachable"
        return "stale"

    def peer_event_details(current, extra=None):
        details = {
            "direct_peer": current.get("direct_peer"),
            "direct_peer_config_channel": current.get("direct_peer_config_channel"),
            "direct_peer_wifi_channel": current.get("direct_peer_wifi_channel"),
            "direct_peer_last_packet_age_s": current.get("direct_peer_last_packet_age_s"),
            "direct_peer_last_packet_bytes": current.get("direct_peer_last_packet_bytes"),
            "direct_peer_last_pong_age_s": current.get("direct_peer_last_pong_age_s"),
            "direct_peer_last_pong_nonce": current.get("direct_peer_last_pong_nonce"),
            "direct_peer_remote_ip": current.get("direct_peer_remote_ip"),
            "direct_peer_remote_mac": current.get("direct_peer_remote_mac"),
            "direct_peer_sync_pending": bool_flag(current.get("direct_peer_sync_pending")),
            "direct_peer_sync_channel": current.get("direct_peer_sync_channel"),
            "direct_peer_sync_last_ok_age_s": current.get("direct_peer_sync_last_ok_age_s"),
            "last_ping_target": current.get("last_ping_target"),
            "last_ping_status": current.get("last_ping_status"),
            "last_ping_response_ms": current.get("last_ping_response_ms"),
            "last_ping_age_s": current.get("last_ping_age_s"),
            "last_ping_nonce": current.get("last_ping_nonce"),
        }
        details.update({key: value for key, value in (extra or {}).items() if value is not None})
        return details

    def peer_status_message(current, state):
        config_channel = peer_channel_detail(current.get("direct_peer_config_channel"), "firmware not reporting")
        active_channel = peer_channel_detail(current.get("direct_peer_wifi_channel"), "firmware not reporting")
        age = peer_packet_age(current)
        channel_note = f"configured channel {config_channel}, Wi-Fi channel {active_channel}"
        if state == "reachable":
            return f"Slave peer reachable: {channel_note}, last packet {age}s ago."
        if state == "stale":
            return f"Slave peer stale: {channel_note}, last packet {age}s ago."
        if state == "waiting":
            return f"Slave peer waiting for accepted packet: {channel_note}."
        return f"Direct peer disabled: {channel_note}."

    def peer_status_severity(state):
        if state == "reachable":
            return "success"
        if state in {"waiting", "stale"}:
            return "warning"
        return "info"

    def peer_channel_detail(value, fallback="not reported"):
        label = peer_channel_label(value)
        return label if label != "--" else fallback

    def config_enabled(value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled", "full", "basic"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
        return bool(value)

    def config_on_off(value, default=False):
        return "ON" if config_enabled(value, default=default) else "OFF"

    def firmware_service_label(value):
        normalized = str(value or "").strip().upper()
        if normalized in {"ON", "OK", "ENABLED", "READY", "CONNECTED"}:
            return "ON"
        if normalized in {"OFF", "DISABLED"}:
            return "OFF"
        if normalized in {"", "UNKNOWN", "--", "N/A"}:
            return "not reported"
        return normalized

    def peer_age_detail(current):
        age = peer_packet_age(current)
        if age is None or age < 0:
            return "no accepted packet"
        return f"{age}s ago"

    def saved_peer_channel(current):
        for value in (
            event_service_config.get("direct_peer_wifi_channel"),
            current.get("direct_peer_config_channel"),
            current.get("direct_peer_wifi_channel"),
            6,
        ):
            if peer_channel_label(value) != "--":
                return value
        return None

    def peer_sync_detail(current):
        if bool_flag(current.get("direct_peer_sync_pending")):
            sync_channel = peer_channel_detail(current.get("direct_peer_sync_channel"), "")
            return f"pending channel {sync_channel}" if sync_channel else "pending"
        try:
            last_ok_age = int(current.get("direct_peer_sync_last_ok_age_s"))
        except (TypeError, ValueError):
            last_ok_age = None
        if last_ok_age is not None and last_ok_age >= 0:
            return f"last OK {last_ok_age}s ago"
        return "not pending"

    def ping_status_severity(status):
        normalized = str(status or "").strip().lower()
        if normalized in {"reachable", "success", "ok", "responded"}:
            return "success"
        if normalized in {"", "unknown", "not_reported"}:
            return "info"
        if normalized in {"queued", "pending", "sent"}:
            return "info"
        return "warning"

    def ping_result_message(current, prefix="Device ping result"):
        target = str(current.get("last_ping_target") or "peer").strip().lower() or "peer"
        status = str(current.get("last_ping_status") or "not reported").strip().lower() or "not reported"
        response_ms = current.get("last_ping_response_ms")
        age_s = current.get("last_ping_age_s")
        nonce = current.get("last_ping_nonce")
        details = []
        try:
            response_value = int(response_ms)
        except (TypeError, ValueError):
            response_value = -1
        if response_value >= 0:
            details.append(f"response {response_value} ms")
        try:
            age_value = int(age_s)
        except (TypeError, ValueError):
            age_value = -1
        if age_value >= 0:
            details.append(f"reported {age_value}s ago")
        if nonce not in (None, "", 0):
            details.append(f"nonce {nonce}")
        detail_text = f" ({', '.join(details)})" if details else ""
        return f"{prefix}: {target} {status}{detail_text}."

    def live_node_status_message(current, status, state):
        master_label = str(status.get("master_status_label") or "--").strip().lower()
        slave_label = str(status.get("slave_status_label") or "--").strip().lower()
        saved_channel = peer_channel_detail(saved_peer_channel(current), "not set")
        config_channel = peer_channel_detail(current.get("direct_peer_config_channel"), "firmware not reporting")
        wifi_channel = peer_channel_detail(current.get("direct_peer_wifi_channel"), "firmware not reporting")
        age_text = peer_age_detail(current)
        sync_text = peer_sync_detail(current)
        if state == "reachable":
            peer_text = f"peer reachable, last slave packet {age_text}"
        elif state == "stale":
            peer_text = f"peer stale, last slave packet {age_text}"
        elif state == "waiting":
            peer_text = "peer waiting for accepted slave packet"
        else:
            peer_text = "peer disabled"
        remote_parts = []
        remote_mac = str(current.get("direct_peer_remote_mac") or "").strip()
        remote_ip = str(current.get("direct_peer_remote_ip") or "").strip()
        if remote_mac:
            remote_parts.append(f"MAC {remote_mac}")
        if remote_ip:
            remote_parts.append(f"IP {remote_ip}")
        remote_text = f"; {', '.join(remote_parts)}" if remote_parts else ""
        return (
            f"Live node status: master {master_label}, slave {slave_label}; "
            f"{peer_text}; saved ch {saved_channel}, configured ch {config_channel}, "
            f"Wi-Fi ch {wifi_channel}; sync {sync_text}{remote_text}."
        )

    def live_node_status_details(current, status, state, extra=None):
        saved_channel = saved_peer_channel(current)
        details = node_reachability_details(status)
        details.update(
            peer_event_details(
                current,
                {
                    "telemetry_status": current.get("telemetry_status"),
                    "peer_state": state,
                    "saved_peer_channel": saved_channel,
                    "saved_peer_channel_label": peer_channel_detail(saved_channel, "not set"),
                    "direct_peer_config_channel_label": peer_channel_detail(
                        current.get("direct_peer_config_channel"),
                        "firmware not reporting",
                    ),
                    "direct_peer_wifi_channel_label": peer_channel_detail(
                        current.get("direct_peer_wifi_channel"),
                        "firmware not reporting",
                    ),
                    "direct_peer_last_packet_age_label": peer_age_detail(current),
                    "direct_peer_sync_status": peer_sync_detail(current),
                },
            )
        )
        details.update({key: value for key, value in (extra or {}).items() if value is not None})
        return details

    def live_config_status_message(current):
        uses_slave = config_enabled(event_service_config.get("slave_device_enabled"), default=True)
        slave_upper = uses_slave and config_enabled(
            event_service_config.get("slave_upper_sensor_enabled"),
            default=uses_slave,
        )
        config_type = "Master + Slave" if uses_slave else "Master Only"
        upper_source = "Slave" if slave_upper else "Master"
        source_monitoring = config_on_off(event_service_config.get("source_tank_monitoring_enabled"), default=True)
        relay = config_on_off(event_service_config.get("relay_enabled"), default=True)
        auto_mode = config_on_off(event_service_config.get("auto_mode_enabled"), default=False)
        cloud_feed = config_on_off(event_service_config.get("cloud_feed_enabled"), default=True)
        saved_channel = peer_channel_detail(saved_peer_channel(current), "not set")
        config_channel = peer_channel_detail(current.get("direct_peer_config_channel"), "firmware not reporting")
        wifi_channel = peer_channel_detail(current.get("direct_peer_wifi_channel"), "firmware not reporting")
        return (
            f"Live device config: {config_type}; upper sensor {upper_source}; lower sensor {source_monitoring}; "
            f"relay {relay}; auto {auto_mode}; cloud {cloud_feed}; firmware telemetry "
            f"{firmware_service_label(current.get('telemetry_service'))}, command "
            f"{firmware_service_label(current.get('command_service'))}; saved ch {saved_channel}, "
            f"configured ch {config_channel}, Wi-Fi ch {wifi_channel}."
        )

    def live_config_status_details(current, extra=None):
        uses_slave = config_enabled(event_service_config.get("slave_device_enabled"), default=True)
        slave_upper = uses_slave and config_enabled(
            event_service_config.get("slave_upper_sensor_enabled"),
            default=uses_slave,
        )
        saved_channel = saved_peer_channel(current)
        details = peer_event_details(
            current,
            {
                "configuration_type": "Master + Slave" if uses_slave else "Master Only",
                "upper_sensor_source": "slave" if slave_upper else "master",
                "slave_device_enabled_label": config_on_off(
                    event_service_config.get("slave_device_enabled"),
                    default=True,
                ),
                "source_tank_monitoring_label": config_on_off(
                    event_service_config.get("source_tank_monitoring_enabled"),
                    default=True,
                ),
                "relay_enabled_label": config_on_off(event_service_config.get("relay_enabled"), default=True),
                "auto_mode_enabled_label": config_on_off(event_service_config.get("auto_mode_enabled"), default=False),
                "cloud_feed_enabled_label": config_on_off(event_service_config.get("cloud_feed_enabled"), default=True),
                "telemetry_service_label": firmware_service_label(current.get("telemetry_service")),
                "command_service_label": firmware_service_label(current.get("command_service")),
                "ota_service_label": firmware_service_label(current.get("ota_service")),
                "saved_peer_channel": saved_channel,
                "saved_peer_channel_label": peer_channel_detail(saved_channel, "not set"),
                "direct_peer_config_channel_label": peer_channel_detail(
                    current.get("direct_peer_config_channel"),
                    "firmware not reporting",
                ),
                "direct_peer_wifi_channel_label": peer_channel_detail(
                    current.get("direct_peer_wifi_channel"),
                    "firmware not reporting",
                ),
                "config_summary": live_config_status_message(current),
            },
        )
        details.update({key: value for key, value in (extra or {}).items() if value is not None})
        return details

    def node_reachability(current):
        return admin_node_status_fields(current or {}, event_service_config)

    def node_reachability_message(status):
        master_label = str(status.get("master_status_label") or "--").strip()
        slave_label = str(status.get("slave_status_label") or "--").strip()
        if master_label == "--":
            return f"Slave {slave_label.lower()}."
        return f"Master {master_label.lower()}, slave {slave_label.lower()}."

    def node_reachability_severity(status):
        master_label = str(status.get("master_status_label") or "").strip().lower()
        slave_label = str(status.get("slave_status_label") or "").strip().lower()
        if "unreachable" in {master_label, slave_label}:
            return "warning"
        if master_label == "reachable" and slave_label in {"reachable", "disabled"}:
            return "success"
        return "info"

    def node_reachability_details(status, extra=None):
        details = {
            "master_status_label": status.get("master_status_label"),
            "master_status_tone": status.get("master_status_tone"),
            "slave_status_label": status.get("slave_status_label"),
            "slave_status_tone": status.get("slave_status_tone"),
        }
        details.update({key: value for key, value in (extra or {}).items() if value is not None})
        return details

    for row in reversed(rows):
        current = row_with_event_telemetry_status(row)
        current_time = parse_timestamp(current.get("created_at"))
        previous_time = parse_timestamp(previous.get("created_at")) if previous else None
        level = safe_float(current.get("level"), 0)
        previous_level = safe_float(previous.get("level"), level) if previous else level
        source_level = safe_float(current.get("lower_tank_level"), None)
        previous_source_level = safe_float(previous.get("lower_tank_level"), None) if previous else None
        peer_state = peer_link_state(current)
        previous_peer_state = peer_link_state(previous) if previous else None
        node_status = node_reachability(current)
        previous_node_status = node_reachability(previous) if previous else None

        if previous is None:
            add_event(
                current,
                "success",
                "Device telemetry feed is active.",
                "telemetry_feed_active",
                {"level": round(level, 2)},
            )
            if peer_state != "disabled":
                add_event(
                    current,
                    peer_status_severity(peer_state),
                    peer_status_message(current, peer_state),
                    f"slave_peer_{peer_state}",
                    peer_event_details(current, {"state": peer_state}),
                )
            add_event(
                current,
                node_reachability_severity(node_status),
                node_reachability_message(node_status),
                "node_reachability_status",
                node_reachability_details(node_status),
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

        if previous is not None and peer_state != previous_peer_state and peer_state != "disabled":
            add_event(
                current,
                peer_status_severity(peer_state),
                peer_status_message(current, peer_state),
                f"slave_peer_{peer_state}",
                peer_event_details(current, {"state": peer_state, "previous_state": previous_peer_state}),
            )

        if previous_node_status is not None:
            current_node_key = (
                node_status.get("master_status_label"),
                node_status.get("slave_status_label"),
            )
            previous_node_key = (
                previous_node_status.get("master_status_label"),
                previous_node_status.get("slave_status_label"),
            )
            if current_node_key != previous_node_key:
                add_event(
                    current,
                    node_reachability_severity(node_status),
                    node_reachability_message(node_status),
                    "node_reachability_changed",
                    node_reachability_details(
                        node_status,
                        {
                            "previous_master_status_label": previous_node_status.get("master_status_label"),
                            "previous_slave_status_label": previous_node_status.get("slave_status_label"),
                        },
                    ),
                )

        if previous is not None:
            for peer_field, peer_label in (
                ("direct_peer_config_channel", "Configured peer channel"),
                ("direct_peer_wifi_channel", "Active peer channel"),
            ):
                current_channel = current.get(peer_field)
                previous_channel = previous.get(peer_field)
                if current_channel in (None, "") or current_channel == previous_channel:
                    continue
                add_event(
                    current,
                    "info",
                    f"{peer_label} changed to {current_channel}.",
                    "peer_channel_changed",
                    peer_event_details(
                        current,
                        {
                            "field": peer_field,
                            "value": current_channel,
                            "previous_value": previous_channel,
                        },
                    ),
                )

            current_sync_pending = bool_flag(current.get("direct_peer_sync_pending"))
            previous_sync_pending = bool_flag(previous.get("direct_peer_sync_pending"))
            if current_sync_pending and not previous_sync_pending:
                sync_channel = peer_channel_label(current.get("direct_peer_sync_channel"))
                add_event(
                    current,
                    "warning",
                    f"Peer channel sync is pending for channel {sync_channel}.",
                    "peer_channel_sync_pending",
                    peer_event_details(current, {"state": "pending"}),
                )
            elif previous_sync_pending and not current_sync_pending:
                add_event(
                    current,
                    "success",
                    "Peer channel sync completed.",
                    "peer_channel_sync_completed",
                    peer_event_details(current, {"state": "completed"}),
                )

        current_ping_key = (
            current.get("last_ping_target"),
            current.get("last_ping_status"),
            current.get("last_ping_nonce"),
        )
        previous_ping_key = (
            previous.get("last_ping_target"),
            previous.get("last_ping_status"),
            previous.get("last_ping_nonce"),
        ) if previous else None
        if current.get("last_ping_status") and (previous is None or current_ping_key != previous_ping_key):
            add_event(
                current,
                ping_status_severity(current.get("last_ping_status")),
                ping_result_message(current, "Device reported ping"),
                "device_ping_reported",
                peer_event_details(current, {"event_group": "ping_result"}),
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

    if latest_peer_row:
        live_event_time = now_utc().strftime(TIMESTAMP_FORMAT)
        latest_node_status = node_reachability(latest_peer_row)
        add_event(
            latest_peer_row,
            "info",
            live_config_status_message(latest_peer_row),
            "device_config_current_status",
            live_config_status_details(
                latest_peer_row,
                {
                    "event_key": f"{normalize_device_id(latest_peer_row.get('device_id'))}:device_config_current_status",
                    "current_status": True,
                    "status_checked_at": live_event_time,
                },
            ),
            event_time=live_event_time,
        )
        if latest_peer_row.get("last_ping_status"):
            add_event(
                latest_peer_row,
                ping_status_severity(latest_peer_row.get("last_ping_status")),
                ping_result_message(latest_peer_row, "Last device ping result"),
                "device_ping_current_status",
                peer_event_details(
                    latest_peer_row,
                    {
                        "event_key": f"{normalize_device_id(latest_peer_row.get('device_id'))}:device_ping_current_status",
                        "current_status": True,
                        "status_checked_at": live_event_time,
                        "event_group": "ping_result",
                    },
                ),
                event_time=live_event_time,
            )
        add_event(
            latest_peer_row,
            node_reachability_severity(latest_node_status),
            live_node_status_message(
                latest_peer_row,
                latest_node_status,
                peer_link_state(latest_peer_row),
            ),
            "node_current_status",
            live_node_status_details(
                latest_peer_row,
                latest_node_status,
                peer_link_state(latest_peer_row),
                {
                    "event_key": f"{normalize_device_id(latest_peer_row.get('device_id'))}:node_current_status",
                    "current_status": True,
                    "status_checked_at": live_event_time,
                },
            ),
            event_time=live_event_time,
        )
        latest_peer_state = peer_link_state(latest_peer_row)
        if latest_peer_state != "disabled":
            add_event(
                latest_peer_row,
                peer_status_severity(latest_peer_state),
                peer_status_message(latest_peer_row, latest_peer_state),
                "peer_current_status",
                peer_event_details(
                    latest_peer_row,
                    {
                        "event_key": f"{normalize_device_id(latest_peer_row.get('device_id'))}:peer_current_status",
                        "state": latest_peer_state,
                        "current_status": True,
                        "status_checked_at": live_event_time,
                    },
                ),
                event_time=live_event_time,
            )

    timeline.extend(build_command_events(limit=40, device_id=normalized_device_id))
    timeline.extend(build_ota_events(limit=20, device_id=normalized_device_id))
    live_priority = {
        "device_config_current_status": 0,
        "device_ping_current_status": 1,
        "node_current_status": 2,
        "peer_current_status": 3,
    }
    timeline.sort(
        key=lambda item: (
            -live_priority.get(str(item.get("kind") or ""), 99),
            parse_timestamp(item.get("time")) or datetime.min,
        ),
        reverse=True,
    )
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
        command_activity = describe_command_activity(command)
        target_device = normalize_device_id(row["target_device"])
        created_at = format_timestamp(row["created_at"])
        delivered_at = format_timestamp(row["delivered_at"])
        events.append(
            {
                "time": created_at,
                "severity": "info",
                "message": command_activity["queued_message"],
                "kind": f"{command_activity['kind_suffix']}_queued",
                "details": {
                    "event_group": "command_queued",
                    "source_table": "device_command_queue",
                    "source_row_id": row["id"],
                    "command": command,
                    "command_label": command_activity["label"],
                    "command_summary": command_activity.get("summary"),
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
                    "message": command_activity["ack_message"],
                    "kind": f"{command_activity['kind_suffix']}_acknowledged",
                    "details": {
                        "event_group": "command_acknowledged",
                        "source_table": "device_command_queue",
                        "source_row_id": row["id"],
                        "command": command,
                        "command_label": command_activity["label"],
                        "command_summary": command_activity.get("summary"),
                        "device_id": target_device,
                        "command_id": row["id"],
                    },
                }
            )
        else:
            created_dt = parse_timestamp(row["created_at"])
            age_seconds = int((now - created_dt).total_seconds()) if created_dt else None
            severity = "warning" if age_seconds is not None and age_seconds >= failure_after_seconds else "info"
            kind = (
                f"{command_activity['kind_suffix']}_delivery_failed"
                if severity == "warning"
                else f"{command_activity['kind_suffix']}_delivery_pending"
            )
            message = command_activity["failed_message"] if severity == "warning" else command_activity["pending_message"]
            events.append(
                {
                    "time": created_at,
                    "severity": severity,
                    "message": message,
                    "kind": kind,
                    "details": {
                        "event_group": "command_delivery_failed" if severity == "warning" else "command_delivery_pending",
                        "source_table": "device_command_queue",
                        "source_row_id": row["id"],
                        "command": command,
                        "command_label": command_activity["label"],
                        "command_summary": command_activity.get("summary"),
                        "device_id": target_device,
                        "command_id": row["id"],
                        "age_seconds": age_seconds,
                    },
                }
            )
    return events


def persist_command_activity_events(device_id, limit=8):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return 0
    try:
        return persist_device_events(
            build_command_events(limit=limit, device_id=normalized_device_id),
            default_device_id=normalized_device_id,
        )
    except Exception as exc:
        logger.warning("Unable to persist command activity for %s: %s", normalized_device_id, exc)
        return 0


FIRMWARE_LOG_LINE_PATTERN = re.compile(r"^\[(?P<timestamp>[^\]]+)\]\s+\[(?P<level>[^\]]+)\]\s*(?P<message>.*)$")


def firmware_log_event_time(timestamp_text, fallback_time):
    text = str(timestamp_text or "").strip()
    fallback = format_timestamp(fallback_time) or now_utc().strftime(TIMESTAMP_FORMAT)
    if not text or text.upper().startswith("NO TIME"):
        return fallback
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_timestamp(text)
    if not parsed:
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime(TIMESTAMP_FORMAT)


def firmware_log_severity(level_text, message):
    level = str(level_text or "").strip().upper()
    lowered = str(message or "").lower()
    if level == "ERROR" or any(token in lowered for token in ("failed", "error", "rejected")):
        return "error"
    if level in {"WARNING", "WARN"} or any(token in lowered for token in ("waiting", "stale", "unreachable", "not recognized")):
        return "warning"
    if any(token in lowered for token in ("command executed", "applied", "saved", "acknowledged")):
        return "success"
    return "info"


def firmware_log_kind(message):
    lowered = str(message or "").lower()
    if "peer command applied" in lowered:
        return "firmware_peer_command_applied"
    if "command executed" in lowered or "flask_command" in lowered:
        return "firmware_command_applied"
    if "service config" in lowered or "runtime service" in lowered:
        return "firmware_runtime_config_log"
    if "threshold" in lowered:
        return "firmware_threshold_log"
    if "peer channel" in lowered:
        return "firmware_peer_channel_log"
    if "tank height" in lowered or "capacity" in lowered or "config_upper" in lowered or "config_lower" in lowered:
        return "firmware_tank_setup_log"
    return "firmware_log"


def build_firmware_log_events_from_payload(payload, limit=24, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    payload = payload or {}
    if not normalized_device_id:
        normalized_device_id = normalize_device_id(payload.get("device_id"))
    if not normalized_device_id:
        return []
    logs = payload.get("logs")
    if logs is None:
        logs = payload.get("firmware_logs")
    if not isinstance(logs, list):
        return []

    fetched_at = now_utc().strftime(TIMESTAMP_FORMAT)
    role = str(payload.get("firmware_role") or "master").strip().lower() or "master"
    events = []
    for item in logs[-max(1, int(limit or 24)):]:
        line = str((item or {}).get("line") if isinstance(item, dict) else item or "").strip()
        if not line:
            continue
        match = FIRMWARE_LOG_LINE_PATTERN.match(line)
        timestamp_text = match.group("timestamp") if match else None
        level = match.group("level") if match else "INFO"
        message = (match.group("message") if match else line).strip() or line
        line_hash = hashlib.sha1(f"{role}|{line}".encode("utf-8")).hexdigest()[:24]
        events.append(
            {
                "time": firmware_log_event_time(timestamp_text, fetched_at),
                "severity": firmware_log_severity(level, message),
                "message": f"Firmware {role} log: {message}",
                "kind": firmware_log_kind(message),
                "details": {
                    "event_key": f"{normalized_device_id}:firmware_log:{line_hash}",
                    "event_group": "firmware_local_log",
                    "source_table": "firmware_local_log",
                    "source_row_id": line_hash,
                    "device_id": normalized_device_id,
                    "node_role": role,
                    "firmware_role": role,
                    "firmware_log_level": str(level or "INFO").strip().upper(),
                    "device_local_url": payload.get("device_local_url"),
                    "raw_line": line,
                    "fetched_at": fetched_at,
                },
            }
        )
    return events


def build_local_firmware_log_events(limit=24, device_id=None, snapshot=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []
    snapshot = snapshot if snapshot is not None else fetch_device_snapshot(normalized_device_id)
    snapshot = snapshot or {}
    local_base_url = normalize_device_base_url(
        snapshot.get("device_local_url") or snapshot.get("local_device_url") or snapshot.get("device_ip_url")
    )
    if not local_base_url:
        return []

    try:
        payload = fetch_local_device_logs(local_base_url, device_id=normalized_device_id)
    except (ValueError, requests.RequestException, json.JSONDecodeError) as exc:
        logger.info("Local firmware logs unavailable for %s via %s: %s", normalized_device_id, local_base_url, exc)
        return []

    return build_firmware_log_events_from_payload(payload, limit=limit, device_id=normalized_device_id)


def build_ota_events(limit=20, device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    query = """
        SELECT id, target_device, target_role, version_label, original_filename, created_at
        FROM firmware_artifacts
        WHERE target_device = ?
    """
    params = [normalized_device_id] if normalized_device_id else [""]
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(max(1, int(limit or 20)))

    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    latest_snapshot = fetch_device_snapshot(normalized_device_id) if normalized_device_id else None
    current_master_firmware = str((latest_snapshot or {}).get("firmware_version") or "").strip()
    current_slave_firmware = str((latest_snapshot or {}).get("slave_firmware_version") or "").strip()
    events = []
    now = now_utc()
    ota_failure_after_seconds = 24 * 60 * 60
    for row in rows:
        version_label = str(row["version_label"] or row["original_filename"] or "firmware").strip()
        target_device = normalize_device_id(row["target_device"]) or normalized_device_id
        target_role = normalize_firmware_artifact_role(row["target_role"] or "master")
        current_firmware = current_slave_firmware if target_role == "slave" else current_master_firmware
        target_label = f"{target_device} {target_role}"
        created_at = parse_timestamp(row["created_at"])
        age_seconds = int((now - created_at).total_seconds()) if created_at else None
        events.append(
            {
                "time": format_timestamp(row["created_at"]),
                "severity": "info",
                "message": f"Firmware uploaded for {target_label}: {version_label}. Upgrade package is ready.",
                "kind": "ota_published",
                "details": {
                    "source_table": "firmware_artifacts",
                    "source_row_id": row["id"],
                    "artifact_id": row["id"],
                    "target_device": target_device,
                    "target_role": target_role,
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
                        "message": f"Firmware version is active on device: {current_firmware}. Upgrade finished successfully.",
                        "kind": "ota_succeeded",
                        "details": {
                            "source_table": "firmware_artifacts",
                            "source_row_id": row["id"],
                            "artifact_id": row["id"],
                            "target_device": normalized_device_id,
                            "target_role": target_role,
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
                            else f"Firmware update is pending on device. Waiting for {version_label} to become active."
                        ),
                        "kind": "ota_failed" if failed else "ota_pending",
                        "details": {
                            "source_table": "firmware_artifacts",
                            "source_row_id": row["id"],
                            "artifact_id": row["id"],
                            "target_device": normalized_device_id,
                            "target_role": target_role,
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
    explicit_key = str(details.get("event_key") or "").strip()
    if explicit_key:
        return hashlib.sha256(f"{device_id}|{explicit_key}".encode("utf-8")).hexdigest()[:40]
    source_table = str(details.get("source_table") or "").strip()
    source_row_id = str(details.get("source_row_id") or "").strip()
    event_kind = str(event.get("kind") or "event").strip().lower()
    if source_table and source_row_id:
        return hashlib.sha256(f"{device_id}|{event_kind}|{source_table}|{source_row_id}".encode("utf-8")).hexdigest()[:40]
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

    normalized_events = []
    for event in events:
        if not isinstance(event, dict):
            continue
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        event_kind = str(event.get("kind") or "event").strip().lower() or "event"
        source_table = str(details.get("source_table") or "").strip() or None
        source_row_id = str(details.get("source_row_id") or "").strip() or None
        device_id = normalize_device_id(details.get("device_id") or default_device_id)
        event_key = device_event_key(event, default_device_id=device_id)
        normalized_events.append((
            device_id,
            event_kind,
            source_table or "",
            source_row_id or "",
            event_key,
            event,
        ))

    # Ensure the same deterministic row order for every batch.
    normalized_events.sort(key=lambda item: (item[0] or "", item[1], item[2], item[3], item[4]))

    def persist():
        persisted = 0
        affected_device_ids = set()
        with get_db() as db:
            for device_id, event_kind, source_table, source_row_id, event_key, event in normalized_events:
                details = event.get("details") if isinstance(event.get("details"), dict) else {}
                event_at = normalize_device_event_time(event.get("time"))
                severity = str(event.get("severity") or "info").strip().lower() or "info"
                message = str(event.get("message") or event_kind.replace("_", " ").title()).strip()
                if device_id:
                    affected_device_ids.add(device_id)
                duration_seconds = details.get("duration_seconds")
                try:
                    duration_seconds = int(duration_seconds) if duration_seconds is not None else None
                except (TypeError, ValueError):
                    duration_seconds = None
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
                        source_table,
                        source_row_id,
                        normalize_device_event_time(details.get("started_at")) if details.get("started_at") else None,
                        normalize_device_event_time(details.get("ended_at")) if details.get("ended_at") else None,
                        duration_seconds,
                        event_at,
                    ),
                )
                persisted += 1
        return persisted, affected_device_ids

    persisted, affected_device_ids = run_with_database_lock_retries(
        persist,
        operation_name="persist device events",
        attempts=4,
        initial_delay_s=0.25,
    )

    for affected_device_id in affected_device_ids:
        schedule_dashboard_summary_refresh(affected_device_id)

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
        activity_device_ids = paired_activity_device_ids(normalized_device_id)
        placeholders = ",".join("?" for _ in activity_device_ids)
        query += f" AND device_id IN ({placeholders})"
        params.extend(activity_device_ids)
    query += """
        ORDER BY CASE
                   WHEN event_kind IN (
                     'device_config_current_status',
                     'device_ping_current_status',
                     'device_ping_reported',
                     'node_current_status',
                     'node_reachability_status',
                     'node_reachability_changed',
                     'master_ping_reachable',
                     'master_ping_unreachable',
                     'slave_ping_reachable',
                     'slave_ping_unreachable',
                     'slave_ping_disabled',
                     'master_ping_command_queued',
                     'master_ping_command_acknowledged',
                     'master_ping_command_delivery_pending',
                     'master_ping_command_delivery_failed',
                     'slave_ping_command_queued',
                     'slave_ping_command_acknowledged',
                     'slave_ping_command_delivery_pending',
                     'slave_ping_command_delivery_failed',
                     'peer_current_status',
                     'slave_peer_waiting',
                     'slave_peer_stale',
                     'slave_peer_reachable',
                     'peer_channel_changed',
                     'peer_channel_sync_pending',
                     'peer_channel_sync_completed',
                     'peer_channel_update_queued',
                     'peer_channel_update_acknowledged',
                     'peer_channel_update_delivery_pending',
                     'peer_channel_update_delivery_failed'
                   ) THEN 0
                   ELSE 1
                 END,
                 event_at DESC,
                 id DESC
        LIMIT ?
    """
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


def device_event_sync_markers(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None, None
    source_clause, source_params = device_source_where_clause()
    live_status_kinds = (
        "device_config_current_status",
        "device_ping_current_status",
        "node_current_status",
        "peer_current_status",
    )
    placeholders = ",".join("?" for _ in live_status_kinds)
    with get_db() as db:
        latest_telemetry = db.execute(
            f"""
            SELECT created_at
            FROM tank_data
            WHERE device_id = ?
              AND {source_clause}
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id, *source_params),
        ).fetchone()
        if not latest_telemetry:
            return None, None
        historical_event = db.execute(
            f"""
            SELECT id
            FROM device_events
            WHERE device_id = ?
              AND source_table = 'tank_data'
              AND event_kind NOT IN ({placeholders})
            LIMIT 1
            """,
            (normalized_device_id, *live_status_kinds),
        ).fetchone()
        if not historical_event:
            return str(latest_telemetry["created_at"] or "").strip() or None, None
        latest_event = db.execute(
            """
            SELECT event_at
            FROM device_events
            WHERE device_id = ?
              AND source_table = 'tank_data'
            ORDER BY event_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
    latest_telemetry_at = str(latest_telemetry["created_at"] or "").strip() or None
    latest_event_at = str(latest_event["event_at"] or "").strip() or None if latest_event else None
    return latest_telemetry_at, latest_event_at


def device_event_sync_is_current(device_id):
    latest_telemetry_at, latest_event_at = device_event_sync_markers(device_id)
    if not latest_telemetry_at or not latest_event_at:
        return False
    return latest_event_at >= latest_telemetry_at


def sync_device_events(device_id=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []
    if device_event_sync_is_current(normalized_device_id):
        return []
    generated_events = build_generated_device_events(limit=320, device_id=normalized_device_id, row_limit=240)
    persist_device_events(generated_events, default_device_id=normalized_device_id)
    return generated_events


def activity_event_identity(event):
    details = event.get("details") if isinstance(event, dict) and isinstance(event.get("details"), dict) else {}
    explicit_key = str(details.get("event_key") or "").strip()
    device_id = normalize_device_id(details.get("device_id") or event.get("device_id")) if isinstance(event, dict) else ""
    event_kind = str((event or {}).get("kind") or "event").strip().lower()
    if explicit_key:
        return ("event_key", device_id, explicit_key)
    source_table = str(details.get("source_table") or (event or {}).get("source_table") or "").strip()
    source_row_id = str(details.get("source_row_id") or (event or {}).get("source_row_id") or "").strip()
    if source_table and source_row_id:
        return ("source", device_id, event_kind, source_table, source_row_id)
    return (
        "message",
        device_id,
        event_kind,
        str((event or {}).get("time") or "").strip(),
        str((event or {}).get("message") or "").strip(),
    )


def activity_event_sort_key(event):
    live_priority = {
        "device_config_current_status": 0,
        "device_ping_current_status": 1,
        "node_current_status": 2,
        "peer_current_status": 3,
    }
    kind = str((event or {}).get("kind") or "").strip()
    return (
        -live_priority.get(kind, 99),
        parse_timestamp((event or {}).get("time")) or datetime.min,
        str((event or {}).get("message") or ""),
    )


def merge_activity_events(*event_lists, limit=12):
    seen = set()
    merged = []
    for event_list in event_lists:
        for event in event_list or []:
            if not isinstance(event, dict):
                continue
            identity = activity_event_identity(event)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(event)
    merged.sort(key=activity_event_sort_key, reverse=True)
    return merged[: max(1, int(limit or 12))]


def build_events(limit=12, device_id=None, sync=True, generated=True, include_local_logs=False):
    normalized_limit = max(1, int(limit or 12))
    if sync:
        sync_device_events(device_id=device_id)
    stored_events = fetch_device_events(limit=normalized_limit, device_id=device_id)
    generated_events = []
    if generated:
        generated_events = build_generated_device_events(
            limit=normalized_limit,
            device_id=device_id,
            row_limit=max(40, min(120, normalized_limit * 4)),
        )
    local_log_events = []
    if include_local_logs:
        local_log_events = build_local_firmware_log_events(limit=min(50, normalized_limit), device_id=device_id)
        if local_log_events:
            try:
                persist_device_events(local_log_events, default_device_id=device_id)
            except Exception as exc:
                logger.warning("Unable to persist local firmware logs for %s: %s", normalize_device_id(device_id), exc)
    events = merge_activity_events(generated_events, local_log_events, stored_events, limit=normalized_limit)
    if events:
        return events
    return build_snapshot_activity_events(limit=normalized_limit, device_id=device_id)


def build_snapshot_activity_events(
    limit=12,
    device_id=None,
    snapshot=None,
    service_config=None,
    automation_settings=None,
    system_status=None,
):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []

    if snapshot is None:
        snapshot = fetch_device_snapshot(normalized_device_id) or build_empty_snapshot_payload(normalized_device_id)
    else:
        snapshot = snapshot or build_empty_snapshot_payload(normalized_device_id)
    if service_config is None or automation_settings is None:
        saved_config = build_current_saved_config(normalized_device_id)
        service_config = service_config if service_config is not None else (saved_config.get("service_config") or {})
        automation_settings = (
            automation_settings
            if automation_settings is not None
            else (saved_config.get("automation_settings") or {})
        )
    else:
        service_config = service_config or {}
        automation_settings = automation_settings or {}
    system_status = system_status or build_system_status_payload(
        snapshot,
        device_id=normalized_device_id,
        service_config=service_config,
    )
    telemetry_status = str(
        (snapshot or {}).get("telemetry_status") or system_status.get("telemetry_status") or "no-data"
    ).strip().lower()
    checked_at = now_utc().strftime(TIMESTAMP_FORMAT)
    event_time = (
        format_timestamp(snapshot.get("created_at"))
        or format_timestamp(snapshot.get("last_sync_at"))
        or checked_at
    )
    live_data = snapshot_has_live_device_data(snapshot)
    uses_slave = boolish_enabled(service_config.get("slave_device_enabled"), default=True)
    source_monitoring = boolish_enabled(service_config.get("source_tank_monitoring_enabled"), default=True)
    events = []

    def add_event(kind, severity, message, details=None, node_role="master"):
        events.append(
            {
                "time": event_time,
                "severity": severity,
                "message": message,
                "kind": kind,
                "details": {
                    "event_key": f"{normalized_device_id}:snapshot:{kind}",
                    "source_table": "live_snapshot",
                    "source_row_id": normalized_device_id,
                    "device_id": normalized_device_id,
                    "node_role": node_role,
                    "current_status": True,
                    "status_checked_at": checked_at,
                    "telemetry_status": telemetry_status,
                    **(details or {}),
                },
            }
        )

    config_type = "Master + Slave" if uses_slave else "Master Only"
    upper_source = "Slave" if uses_slave and boolish_enabled(service_config.get("slave_upper_sensor_enabled"), default=True) else "Master"
    add_event(
        "device_config_current_status",
        "info",
        (
            f"Live device config: {config_type}; upper sensor {upper_source}; "
            f"source tank {'enabled' if source_monitoring else 'disabled'}; "
            f"relay {device_detail_card_bool(service_config.get('relay_enabled'), default=True)}; "
            f"auto {device_detail_card_bool(service_config.get('auto_mode_enabled'), default=False)}."
        ),
        {
            "device_state": system_status.get("device"),
            "status_code": system_status.get("device_status_code"),
            "master_status_label": system_status.get("master_status_label"),
            "slave_status_label": system_status.get("slave_status_label"),
            "configuration_type": config_type,
            "upper_sensor_source": upper_source.lower(),
            "auto_mode_enabled": service_config.get("auto_mode_enabled"),
            "slave_device_enabled": service_config.get("slave_device_enabled"),
            "source_tank_monitoring_enabled": service_config.get("source_tank_monitoring_enabled"),
            "relay_enabled": service_config.get("relay_enabled"),
            "ai_analysis_enabled": service_config.get("ai_analysis_enabled"),
            "cloud_feed_mode": service_config.get("cloud_feed_mode"),
            "auto_start_pct": automation_settings.get("auto_start_pct"),
            "auto_stop_pct": automation_settings.get("auto_stop_pct"),
        },
    )

    level = safe_float(snapshot.get("level"), None)
    remaining_liters = safe_float(snapshot.get("remaining_liters"), None)
    capacity_liters = safe_float(snapshot.get("capacity_liters"), None)
    if level is not None:
        water_parts = [f"Tank level is {level:.1f}%"]
        if remaining_liters is not None:
            water_parts.append(f"{remaining_liters:.1f} L available")
        if capacity_liters is not None:
            water_parts.append(f"out of {capacity_liters:.1f} L capacity")
        add_event(
            "tank_level_current_status",
            "success" if level > 20 else "warning",
            "; ".join(water_parts) + ".",
            {
                "level": round(level, 2),
                "remaining_liters": round(remaining_liters, 2) if remaining_liters is not None else None,
                "capacity_liters": round(capacity_liters, 2) if capacity_liters is not None else None,
                "tank_health_status": snapshot.get("tank_health_status"),
            },
        )

    motor = str(snapshot.get("motor") or "").strip().upper()
    mode = str(snapshot.get("mode") or "").strip().upper()
    if motor or mode:
        add_event(
            "pump_current_status",
            "info",
            f"Pump is {motor or 'not reported'} in {mode or 'unknown'} mode.",
            {"motor": motor, "mode": mode},
        )

    master_status = system_status.get("master_status_label") or "Not reported"
    slave_status = system_status.get("slave_status_label") or ("Not reported" if uses_slave else "Disabled")
    add_event(
        "node_current_status",
        "success" if str(master_status).lower() == "reachable" and str(slave_status).lower() in {"reachable", "disabled"} else "warning",
        f"Master {str(master_status).lower()}, slave {str(slave_status).lower()}.",
        {
            "master_status_label": master_status,
            "slave_status_label": slave_status,
            "master_status_tone": system_status.get("master_status_tone"),
            "slave_status_tone": system_status.get("slave_status_tone"),
        },
    )

    direct_peer = str(snapshot.get("direct_peer") or "").strip()
    peer_age = safe_float(snapshot.get("direct_peer_last_packet_age_s"), None)
    peer_channel = snapshot.get("direct_peer_wifi_channel") or snapshot.get("direct_peer_config_channel") or service_config.get("direct_peer_wifi_channel")
    if uses_slave or direct_peer:
        peer_parts = [f"Direct peer {direct_peer or 'not reported'}"]
        if peer_channel not in (None, ""):
            peer_parts.append(f"channel {peer_channel}")
        if peer_age is not None:
            peer_parts.append(f"last packet {format_compact_uptime(peer_age)} ago")
        if snapshot.get("direct_peer_remote_mac"):
            peer_parts.append(f"MAC {snapshot.get('direct_peer_remote_mac')}")
        add_event(
            "peer_current_status",
            "success" if peer_age is not None and peer_age <= DIRECT_PEER_STALE_AFTER_SECONDS else "warning",
            "; ".join(peer_parts) + ".",
            {
                "direct_peer": direct_peer,
                "direct_peer_config_channel": snapshot.get("direct_peer_config_channel"),
                "direct_peer_wifi_channel": snapshot.get("direct_peer_wifi_channel"),
                "direct_peer_last_packet_age_s": snapshot.get("direct_peer_last_packet_age_s"),
                "direct_peer_remote_mac": snapshot.get("direct_peer_remote_mac"),
                "direct_peer_remote_ip": snapshot.get("direct_peer_remote_ip"),
            },
        )

    firmware_parts = []
    if snapshot.get("firmware_version"):
        firmware_parts.append(f"master {snapshot.get('firmware_version')}")
    if snapshot.get("slave_firmware_version"):
        firmware_parts.append(f"slave {snapshot.get('slave_firmware_version')}")
    if firmware_parts:
        add_event(
            "firmware_current_status",
            "info",
            f"Firmware status: {', '.join(firmware_parts)}.",
            {
                "firmware_version": snapshot.get("firmware_version"),
                "slave_firmware_version": snapshot.get("slave_firmware_version"),
            },
        )

    heap_parts = []
    if snapshot.get("free_heap") is not None:
        heap_parts.append(f"master {device_detail_card_heap(snapshot.get('free_heap'))}")
    if snapshot.get("slave_free_heap") is not None:
        heap_parts.append(f"slave {device_detail_card_heap(snapshot.get('slave_free_heap'))}")
    if heap_parts:
        add_event(
            "memory_current_status",
            "info",
            f"Memory status: {', '.join(heap_parts)}.",
            {
                "free_heap": snapshot.get("free_heap"),
                "slave_free_heap": snapshot.get("slave_free_heap"),
            },
        )

    source_level = safe_float(snapshot.get("lower_tank_level"), None)
    if source_monitoring and source_level is not None:
        add_event(
            "source_tank_current_status",
            "success" if source_level > 20 else "warning",
            f"Source tank level is {source_level:.1f}%.",
            {"source_tank_level": round(source_level, 2)},
        )

    service_parts = [
        f"telemetry {device_detail_card_title(snapshot.get('telemetry_service'), 'not reported')}",
        f"command {device_detail_card_title(snapshot.get('command_service'), 'not reported')}",
        f"local firmware upload {device_detail_card_bool(service_config.get('local_firmware_upload_enabled'), default=True)}",
        f"simulator {device_detail_card_title(snapshot.get('simulator') or snapshot.get('simulator_status'), 'OFF')}",
    ]
    add_event(
        "services_current_status",
        "info",
        f"Service status: {'; '.join(service_parts)}.",
        {
            "telemetry_service_label": snapshot.get("telemetry_service"),
            "command_service_label": snapshot.get("command_service"),
            "local_firmware_upload_enabled": service_config.get("local_firmware_upload_enabled"),
            "simulator": snapshot.get("simulator") or snapshot.get("simulator_status"),
        },
    )

    if automation_settings or service_config:
        add_event(
            "thresholds_current_status",
            "info",
            (
                "Auto thresholds: start at or below "
                f"{device_detail_card_percent(automation_settings.get('auto_start_pct') or service_config.get('auto_start_pct'), 'not reported')}; "
                "stop at or above "
                f"{device_detail_card_percent(automation_settings.get('auto_stop_pct') or service_config.get('auto_stop_pct'), 'not reported')}."
            ),
            {
                "auto_start_pct": automation_settings.get("auto_start_pct") or service_config.get("auto_start_pct"),
                "auto_stop_pct": automation_settings.get("auto_stop_pct") or service_config.get("auto_stop_pct"),
            },
        )

    if not live_data:
        add_event(
            "saved_config_current_status",
            "info",
            f"No historical activity yet for {normalized_device_id}. Showing saved configuration rows from Flask.",
            {"live_data": False},
        )

    return merge_activity_events(events, limit=limit)


def ping_age_label(value):
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return "--"
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{seconds}s"
    return format_compact_uptime(seconds)


def build_device_ping_result(device_id, target):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("Device id is required.")

    normalized_target = str(target or "").strip().lower()
    if normalized_target not in {"master", "slave"}:
        raise ValueError("Ping target must be master or slave.")

    snapshot = fetch_device_snapshot(normalized_device_id) or build_empty_snapshot_payload(normalized_device_id)
    service_config = resolve_device_service_config(normalized_device_id, snapshot=snapshot)
    expected_ping_nonce = secrets.randbelow(2147483646) + 1
    queued_command = f"PING_{normalized_target.upper()}:{expected_ping_nonce}"
    queue_result = queue_command(queued_command, target_device=normalized_device_id)
    if isinstance(queue_result, tuple):
        error_payload, _status_code = queue_result
        raise ValueError(error_payload.get("error") or "Unable to queue ping command.")

    status_payload = build_system_status_payload(
        snapshot,
        device_id=normalized_device_id,
        service_config=service_config,
    )
    node_status = {
        "master_status_label": status_payload.get("master_status_label"),
        "master_status_tone": status_payload.get("master_status_tone"),
        "slave_status_label": status_payload.get("slave_status_label"),
        "slave_status_tone": status_payload.get("slave_status_tone"),
    }
    target_label = normalized_target.title()
    observed_status_label = str(status_payload.get(f"{normalized_target}_status_label") or "Unreachable").strip()
    observed_status_tone = str(status_payload.get(f"{normalized_target}_status_tone") or "offline").strip()
    disabled = observed_status_label.lower() == "disabled"
    severity = "info"
    event_time = now_utc()
    event_time_text = event_time.strftime(TIMESTAMP_FORMAT)
    telemetry_status_value = str(snapshot.get("telemetry_status") or "no-data").strip()
    seconds_since_sync = snapshot.get("seconds_since_sync")
    peer_age = snapshot.get("direct_peer_last_packet_age_s")
    peer_age_text = ping_age_label(peer_age)
    last_sync_at = snapshot.get("last_sync_at")
    config_channel = snapshot.get("direct_peer_config_channel")
    active_channel = snapshot.get("direct_peer_wifi_channel")

    def ping_detail_value(value, fallback="Not reported"):
        text = str(value if value is not None else "").strip()
        return text if text else fallback

    def ping_channel_value(value, fallback="Not reported"):
        try:
            channel = int(value)
        except (TypeError, ValueError):
            return fallback
        return str(channel) if 1 <= channel <= 13 else fallback

    saved_channel = (
        service_config.get("direct_peer_wifi_channel")
        or snapshot.get("direct_peer_config_channel")
        or snapshot.get("direct_peer_wifi_channel")
        or 6
    )

    details = {
        "event_key": f"{normalized_device_id}:ping_command:{normalized_target}:{queue_result.get('command_id') or event_time.strftime('%Y%m%d%H%M%S%f')}",
        "device_id": normalized_device_id,
        "current_status": True,
        "status_checked_at": event_time_text,
        "event_group": "ping_command",
        "ping_target": normalized_target,
        "ping_status": "Queued",
        "ping_status_tone": "pending",
        "queued_command": queued_command,
        "command": queued_command,
        "command_id": queue_result.get("command_id"),
        "expected_ping_nonce": expected_ping_nonce,
        "target_device": normalized_device_id,
        "telemetry_status": telemetry_status_value,
        "last_sync_at": last_sync_at,
        "seconds_since_sync": seconds_since_sync,
        "saved_peer_channel": saved_channel,
        "saved_peer_channel_label": ping_channel_value(saved_channel, "not set"),
        "direct_peer": snapshot.get("direct_peer"),
        "direct_peer_config_channel": snapshot.get("direct_peer_config_channel"),
        "direct_peer_config_channel_label": ping_channel_value(config_channel, "firmware not reporting"),
        "direct_peer_wifi_channel": snapshot.get("direct_peer_wifi_channel"),
        "direct_peer_wifi_channel_label": ping_channel_value(active_channel, "firmware not reporting"),
        "direct_peer_last_packet_age_s": peer_age,
        "direct_peer_last_packet_age_label": peer_age_text if peer_age_text != "--" else "no accepted packet",
        "direct_peer_last_pong_age_s": snapshot.get("direct_peer_last_pong_age_s"),
        "direct_peer_last_pong_nonce": snapshot.get("direct_peer_last_pong_nonce"),
        "last_ping_target": snapshot.get("last_ping_target"),
        "last_ping_status": snapshot.get("last_ping_status"),
        "last_ping_response_ms": snapshot.get("last_ping_response_ms"),
        "last_ping_age_s": snapshot.get("last_ping_age_s"),
        "last_ping_nonce": snapshot.get("last_ping_nonce"),
        "direct_peer_remote_ip": snapshot.get("direct_peer_remote_ip"),
        "direct_peer_remote_mac": snapshot.get("direct_peer_remote_mac"),
        **node_status,
    }

    if normalized_target == "master":
        message = (
            f"Ping master command queued for {normalized_device_id}. "
            "The master will execute it on its next command poll and acknowledge it."
        )
    elif disabled:
        message = (
            f"Ping slave command queued, but saved configuration currently marks slave as disabled. "
            f"The command is still queued as {queued_command}; enable slave runtime config if this is unexpected."
        )
    else:
        message = (
            f"Ping slave command queued for {normalized_device_id}. "
            "The master will send an ESP-NOW ping to the slave on its next command poll."
        )

    detail_lines = [
        f"Target: {target_label}",
        "Result: Command queued",
        f"Queued command: {queued_command}",
        f"Command id: {queue_result.get('command_id') or '--'}",
        f"Ping nonce: {expected_ping_nonce}",
        f"Queued at: {event_time_text}",
        "Execution: waiting for master command poll",
        f"Master reachability: {node_status.get('master_status_label') or 'Unreachable'}",
        f"Slave reachability: {node_status.get('slave_status_label') or 'Unreachable'}",
        f"Telemetry: {telemetry_status_value or 'not available'}",
        f"Last sync: {ping_detail_value(last_sync_at)}",
    ]
    if normalized_target == "slave":
        detail_lines.extend(
            [
                f"Last slave packet: {peer_age_text if peer_age_text != '--' else 'No accepted packet'}",
                f"Saved peer channel: {ping_channel_value(saved_channel, 'not set')}",
                f"Configured peer channel: {ping_channel_value(config_channel, 'Firmware not reporting')}",
                f"Master Wi-Fi channel: {ping_channel_value(active_channel, 'Firmware not reporting')}",
                f"Last peer pong: {ping_age_label(snapshot.get('direct_peer_last_pong_age_s')) if ping_age_label(snapshot.get('direct_peer_last_pong_age_s')) != '--' else 'No pong reported yet'}",
                f"Last firmware ping result: {ping_detail_value(snapshot.get('last_ping_status'), 'Not reported yet')}",
                f"Peer MAC: {ping_detail_value(snapshot.get('direct_peer_remote_mac'), 'Unknown until peer packet')}",
                f"Peer IP: {ping_detail_value(snapshot.get('direct_peer_remote_ip'), 'Unknown until peer packet')}",
            ]
        )

    event = {
        "time": event_time_text,
        "severity": severity,
        "message": message,
        "kind": f"{normalized_target}_ping_command_queued",
        "details": details,
    }
    return {
        "target": normalized_target,
        "reachable": False,
        "disabled": disabled,
        "status": "Queued",
        "title": f"Ping {target_label} Queued",
        "message": message,
        "detail_lines": detail_lines,
        "saved_peer_channel": saved_channel,
        "queued_command": queued_command,
        "command_id": queue_result.get("command_id"),
        "expected_ping_nonce": expected_ping_nonce,
        "event": event,
    }


def describe_command_activity(command):
    normalized = str(command or "").strip().upper()
    details = {
        "label": normalized or "COMMAND",
        "queued_message": f"Command queued: {normalized or 'COMMAND'}.",
        "ack_message": f"Command acknowledged by device: {normalized or 'COMMAND'}.",
        "pending_message": f"Command waiting for device acknowledgement: {normalized or 'COMMAND'}.",
        "failed_message": f"Command has not been acknowledged yet: {normalized or 'COMMAND'}.",
        "kind_suffix": "command",
    }

    if normalized == "ON":
        details.update(
            {
                "label": "Pump start",
                "queued_message": "Pump start requested.",
                "ack_message": "Pump start acknowledged by device.",
                "pending_message": "Pump start is waiting for device acknowledgement.",
                "failed_message": "Pump start has not been acknowledged yet.",
                "kind_suffix": "pump_start",
            }
        )
        return details

    if normalized in {"PING_MASTER", "PING:MASTER", "PING_MASTER_NODE"} or normalized.startswith("PING_MASTER:"):
        details.update(
            {
                "label": "Ping master",
                "queued_message": "Ping master requested: real command queued for the master controller.",
                "ack_message": "Ping master command acknowledged by device.",
                "pending_message": "Ping master is waiting for the master command poll.",
                "failed_message": "Ping master has not been acknowledged yet by the device.",
                "kind_suffix": "master_ping_command",
                "summary": "Master local ping command. Result is confirmed after device acknowledgement/telemetry.",
            }
        )
        return details

    if (
        normalized in {"PING_SLAVE", "PING:SLAVE", "PING_PEER", "PEER_PING", "PING_SLAVE_NODE"}
        or normalized.startswith("PING_SLAVE:")
        or normalized.startswith("PING_PEER:")
        or normalized.startswith("PEER_PING:")
    ):
        details.update(
            {
                "label": "Ping slave",
                "queued_message": "Ping slave requested: real ESP-NOW ping command queued for the master controller.",
                "ack_message": "Ping slave command acknowledged by device.",
                "pending_message": "Ping slave is waiting for the master command poll.",
                "failed_message": "Ping slave has not been acknowledged yet by the device.",
                "kind_suffix": "slave_ping_command",
                "summary": "Master will send an ESP-NOW ping to the slave; result appears after telemetry reports it.",
            }
        )
        return details

    if normalized == "OFF":
        details.update(
            {
                "label": "Pump stop",
                "queued_message": "Pump stop requested.",
                "ack_message": "Pump stop acknowledged by device.",
                "pending_message": "Pump stop is waiting for device acknowledgement.",
                "failed_message": "Pump stop has not been acknowledged yet.",
                "kind_suffix": "pump_stop",
            }
        )
        return details

    if normalized == "REBOOT":
        details.update(
            {
                "label": "Device restart",
                "queued_message": "Device restart requested.",
                "ack_message": "Device restart acknowledged by device.",
                "pending_message": "Device restart is waiting for device acknowledgement.",
                "failed_message": "Device restart has not been acknowledged yet.",
                "kind_suffix": "device_restart",
            }
        )
        return details

    if normalized in {"SIMULATOR_ON", "SIMULATOR_OFF"}:
        simulator_state = "enable" if normalized.endswith("_ON") else "disable"
        simulator_state_past = "enabled" if normalized.endswith("_ON") else "disabled"
        details.update(
            {
                "label": f"Simulator {simulator_state}",
                "queued_message": f"Simulator {simulator_state} requested.",
                "ack_message": f"Simulator {simulator_state_past} by device acknowledgement.",
                "pending_message": f"Simulator {simulator_state} is waiting for device acknowledgement.",
                "failed_message": f"Simulator {simulator_state} has not been acknowledged yet.",
                "kind_suffix": "simulator_toggle",
            }
        )
        return details

    if normalized.startswith("THRESHOLDS:"):
        _prefix, _sep, payload = normalized.partition(":")
        start_text, _sep2, stop_text = payload.partition(":")
        threshold_note = (
            f"start {start_text.strip()}% / stop {stop_text.strip()}%"
            if start_text.strip() and stop_text.strip()
            else "device thresholds"
        )
        details.update(
            {
                "label": "Auto thresholds update",
                "queued_message": f"Auto thresholds update requested: {threshold_note}.",
                "ack_message": f"Auto thresholds acknowledged by device: {threshold_note}.",
                "pending_message": f"Auto thresholds update is waiting for device acknowledgement: {threshold_note}.",
                "failed_message": f"Auto thresholds update has not been acknowledged yet: {threshold_note}.",
                "kind_suffix": "threshold_update",
            }
        )
        return details

    if normalized.startswith("PEER_CHANNEL:"):
        _prefix, _sep, channel_text = normalized.partition(":")
        channel_note = f"channel {channel_text.strip()}" if channel_text.strip() else "peer channel"
        details.update(
            {
                "label": "Peer channel update",
                "queued_message": f"Peer channel update requested: {channel_note}.",
                "ack_message": f"Peer channel update acknowledged by device: {channel_note}.",
                "pending_message": f"Peer channel update is waiting for device acknowledgement: {channel_note}.",
                "failed_message": f"Peer channel update has not been acknowledged yet: {channel_note}.",
                "kind_suffix": "peer_channel_update",
            }
        )
        return details

    if normalized.startswith("SERVICECFG11:") or normalized.startswith("SERVICECFG10:") or normalized.startswith("SERVICECFG9:") or normalized.startswith("SERVICECFG8:") or normalized.startswith("SERVICECFG7:") or normalized.startswith("SERVICECFG6:") or normalized.startswith("SERVICECFG5:") or normalized.startswith("SERVICECFG4:"):
        values = normalized.split(":")[1:]
        labels = [
            "master upper",
            "slave upper",
            "source tank",
            "relay",
            "buzzer",
            "LED",
            "OTA",
            "local upload",
            "auto mode",
        ]
        if normalized.startswith("SERVICECFG11:") or normalized.startswith("SERVICECFG10:") or normalized.startswith("SERVICECFG9:") or normalized.startswith("SERVICECFG8:") or normalized.startswith("SERVICECFG7:") or normalized.startswith("SERVICECFG6:"):
            labels.append("municipal sensor")
        if normalized.startswith("SERVICECFG11:") or normalized.startswith("SERVICECFG10:") or normalized.startswith("SERVICECFG9:") or normalized.startswith("SERVICECFG8:"):
            labels.extend(("lower turbidity", "upper turbidity"))
        elif normalized.startswith("SERVICECFG7:"):
            labels.append("turbidity monitoring")
        if normalized.startswith("SERVICECFG11:") or normalized.startswith("SERVICECFG10:"):
            labels.extend(("inlet motorized valve", "outlet motorized valve"))
            if normalized.startswith("SERVICECFG11:"):
                labels.extend(("starter auxiliary sensor", "motor current sensor", "water flow sensor", "water pressure sensor"))
        elif normalized.startswith("SERVICECFG9:"):
            labels.append("inlet motorized valve")

        def service_state_label(value):
            return "ON" if str(value or "").strip().upper() in {"1", "ON", "TRUE", "ENABLED"} else "OFF"

        service_summary = ", ".join(
            f"{label} {service_state_label(value)}"
            for label, value in zip(labels, values)
            if str(value or "").strip()
        )
        summary_note = f": {service_summary}" if service_summary else ""
        details.update(
            {
                "label": "Runtime service configuration",
                "summary": service_summary,
                "queued_message": f"Runtime service configuration update requested{summary_note}.",
                "ack_message": f"Runtime service configuration acknowledged by device{summary_note}.",
                "pending_message": f"Runtime service configuration is waiting for device acknowledgement{summary_note}.",
                "failed_message": f"Runtime service configuration has not been acknowledged yet{summary_note}.",
                "kind_suffix": "service_config_update",
            }
        )
        return details

    if normalized.startswith("CONFIG_UPPER:") or normalized.startswith("CONFIG_LOWER:"):
        parts = normalized.split(":")
        sensor_label = "Upper" if normalized.startswith("CONFIG_UPPER:") else "Lower/source"
        setup_note = ""
        if len(parts) >= 3:
            setup_note = f" height {parts[1]} cm / capacity {parts[2]} L"
        details.update(
            {
                "label": f"{sensor_label} tank setup",
                "queued_message": f"{sensor_label} tank setup requested.{setup_note}",
                "ack_message": f"{sensor_label} tank setup acknowledged by device.{setup_note}",
                "pending_message": f"{sensor_label} tank setup is waiting for device acknowledgement.{setup_note}",
                "failed_message": f"{sensor_label} tank setup has not been acknowledged yet.{setup_note}",
                "kind_suffix": "tank_setup",
            }
        )
        return details

    if normalized.startswith("CONFIG_CAPACITY:") or normalized.startswith("CONFIG_LOWER_CAPACITY:"):
        parts = normalized.split(":")
        sensor_label = "Main tank" if normalized.startswith("CONFIG_CAPACITY:") else "Lower/source tank"
        capacity_note = f" {parts[1]} L" if len(parts) >= 2 and parts[1].strip() else ""
        details.update(
            {
                "label": f"{sensor_label} capacity",
                "queued_message": f"{sensor_label} capacity update requested.{capacity_note}",
                "ack_message": f"{sensor_label} capacity update acknowledged by device.{capacity_note}",
                "pending_message": f"{sensor_label} capacity update is waiting for device acknowledgement.{capacity_note}",
                "failed_message": f"{sensor_label} capacity update has not been acknowledged yet.{capacity_note}",
                "kind_suffix": "tank_capacity_update",
            }
        )
        return details

    if normalized == "CALIBRATE":
        details.update(
            {
                "label": "Sensor calibration",
                "queued_message": "Sensor calibration requested.",
                "ack_message": "Sensor calibration acknowledged by device.",
                "pending_message": "Sensor calibration is waiting for device acknowledgement.",
                "failed_message": "Sensor calibration has not been acknowledged yet.",
                "kind_suffix": "sensor_calibration",
            }
        )
    return details


def deliver_alert_webhook(payload, raise_on_failure=False):
    failures = []
    if ALERT_WEBHOOK_URL:
        try:
            requests.post(ALERT_WEBHOOK_URL, json=payload, timeout=(3, 8))
        except requests.RequestException as exc:
            logger.warning("Generic alert webhook failed: %s", exc)
            failures.append(str(exc))

    if SLACK_WEBHOOK_URL:
        try:
            text = f"[{payload.get('severity', 'info').upper()}] {payload.get('kind', 'alert')}: {payload.get('message', '')}"
            if payload.get("device_id"):
                text += f" (device {payload['device_id']})"
            requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=(3, 8))
        except requests.RequestException as exc:
            logger.warning("Slack alert webhook failed: %s", exc)
            failures.append(str(exc))

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
            failures.append(str(exc))

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
            failures.append(str(exc))
    if failures and raise_on_failure:
        raise RuntimeError("; ".join(failures))
    return not failures


def send_alert_webhook(payload):
    if CAPACITY_FEATURES.enabled("database_job_queue"):
        from flask_app.capacity_jobs import enqueue_job

        with get_db() as db:
            enqueue_job(
                db.cursor(),
                "alert_webhook",
                payload,
                device_id=normalize_device_id(payload.get("device_id")),
            )
        return True
    return deliver_alert_webhook(payload)


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
            active_rows = db.execute(
                """
                SELECT id, active, message, severity
                FROM ops_alerts
                WHERE kind = ? AND COALESCE(device_id, '') = COALESCE(?, '')
                  AND active = 1
                ORDER BY updated_at DESC, id DESC
                """,
                (kind, normalized_device_id),
            ).fetchall()
            existing_active = active_rows[0] if active_rows else None
            existing = existing_active or db.execute(
                """
                SELECT id, active, message, severity
                FROM ops_alerts
                WHERE kind = ? AND COALESCE(device_id, '') = COALESCE(?, '')
                ORDER BY id DESC
                LIMIT 1
                """,
                (kind, normalized_device_id),
            ).fetchone()
            duplicate_active_ids = [row["id"] for row in active_rows[1:]]

            if active:
                if existing_active:
                    db.execute(
                        """
                        UPDATE ops_alerts
                        SET severity = ?, message = ?, active = 1, resolved_at = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (severity, message, existing_active["id"]),
                    )
                    if duplicate_active_ids:
                        placeholders = ",".join("?" for _ in duplicate_active_ids)
                        db.execute(
                            f"""
                            UPDATE ops_alerts
                            SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                            WHERE id IN ({placeholders})
                            """,
                            tuple(duplicate_active_ids),
                        )
                elif existing:
                    db.execute(
                        """
                        UPDATE ops_alerts
                        SET severity = ?, message = ?, active = 1, resolved_at = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (severity, message, existing["id"]),
                    )
                    webhook_payload = {
                        "id": existing["id"],
                        "device_id": normalized_device_id,
                        "kind": kind,
                        "severity": severity,
                        "message": message,
                        "active": True,
                    }
                else:
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
            elif active_rows:
                active_ids = [row["id"] for row in active_rows]
                placeholders = ",".join("?" for _ in active_ids)
                db.execute(
                    f"""
                    UPDATE ops_alerts
                    SET active = 0, resolved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    tuple(active_ids),
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


def latest_active_alert_filter(alias="alert"):
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM ops_alerts AS newer_alert
            WHERE newer_alert.active = 1
              AND newer_alert.kind = {alias}.kind
              AND COALESCE(newer_alert.device_id, '') = COALESCE({alias}.device_id, '')
              AND (
                  newer_alert.updated_at > {alias}.updated_at
                  OR (newer_alert.updated_at = {alias}.updated_at AND newer_alert.id > {alias}.id)
              )
        )
    """


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
        active=effective_pump_failure_active(snapshot),
        best_effort=True,
    )
    set_alert(
        "dry_run",
        "danger",
        "Dry-run protection triggered.",
        device_id=device_id,
        active=effective_dry_run_active(snapshot),
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
    query = f"""
        SELECT alert.id, alert.device_id, alert.kind, alert.severity, alert.message, alert.created_at, alert.updated_at
        FROM ops_alerts AS alert
        WHERE alert.active = 1
          AND {latest_active_alert_filter("alert")}
    """
    params = []
    normalized_device_id = normalize_device_id(device_id)
    if normalized_device_id:
        query += " AND COALESCE(alert.device_id, '') = COALESCE(?, '')"
        params.append(normalized_device_id)
    query += " ORDER BY alert.updated_at DESC, alert.id DESC LIMIT ?"
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()
    return [dict(row) for row in rows]


def fetch_filtered_alerts(limit=20, severity=None, device_id=None, updated_since=None):
    query = f"""
        SELECT alert.id, alert.device_id, alert.kind, alert.severity, alert.message, alert.created_at, alert.updated_at
        FROM ops_alerts AS alert
        WHERE alert.active = 1
          AND {latest_active_alert_filter("alert")}
    """
    params = []
    if severity:
        query += " AND LOWER(alert.severity) = ?"
        params.append(str(severity).lower())
    if device_id:
        query += " AND COALESCE(alert.device_id, '') = COALESCE(?, '')"
        params.append(normalize_device_id(device_id))
    if updated_since:
        query += " AND alert.updated_at >= ?"
        params.append(updated_since)
    query += " ORDER BY alert.updated_at DESC, alert.id DESC LIMIT ?"
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
    source_clause, source_params = device_source_where_clause(column="ranked_source.device_source")
    query = """
        SELECT tank_data.*
        FROM tank_data
        JOIN (
            SELECT ranked_source.id,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(ranked_source.device_id, '')
                       ORDER BY ranked_source.created_at DESC, ranked_source.id DESC
                   ) AS device_row_number
            FROM tank_data AS ranked_source
            WHERE
    """
    query += source_clause
    params = list(source_params)
    if normalized_device_ids:
        placeholders = ",".join("?" for _ in normalized_device_ids)
        query += f" AND COALESCE(ranked_source.device_id, '') IN ({placeholders})"
        params.extend(normalized_device_ids)
    query += """
        ) AS ranked_inventory ON ranked_inventory.id = tank_data.id
        WHERE ranked_inventory.device_row_number = 1
        ORDER BY tank_data.id DESC
        LIMIT ?
    """
    params.append(limit)
    with get_db() as db:
        rows = db.execute(query, tuple(params)).fetchall()

    inventory = []
    for row in rows:
        snapshot = enrich_snapshot(dict(row))
        inventory.append(build_admin_device_entry(snapshot.get("device_id") or "unassigned", snapshot=snapshot))
    return inventory


def fetch_device_snapshot(device_id, include_transition_counts=True):
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
        counts = None
        if include_transition_counts:
            counts = db.execute(
                f"""
            SELECT
                COALESCE(SUM(CASE WHEN motor='ON' AND COALESCE(prev_motor,'OFF')!='ON' THEN 1 ELSE 0 END), 0) AS motor_cycles,
                COALESCE(SUM(CASE WHEN pipe_leak='YES' AND COALESCE(prev_pipe_leak,'NO')!='YES' THEN 1 ELSE 0 END), 0) AS leak_events
            FROM (
                SELECT motor,
                       pipe_leak,
                       LAG(motor) OVER (ORDER BY id) AS prev_motor,
                       LAG(pipe_leak) OVER (ORDER BY id) AS prev_pipe_leak
                FROM (
                    SELECT id, motor, pipe_leak
                    FROM tank_data
                    WHERE device_id = ?
                      AND {source_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT 200
                ) recent_rows
                ORDER BY id
            ) transitions
                """,
                (normalized_device_id, *source_params),
            ).fetchone()
    return enrich_snapshot(
        dict(row),
        int((counts or {}).get("motor_cycles") or 0),
        int((counts or {}).get("leak_events") or 0),
    )


def fetch_device_history(device_id, limit=48):
    if not TELEMETRY_HISTORY_ENABLED:
        return []

    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return []
    active_mode = get_device_source_mode()
    if CAPACITY_FEATURES.enabled("history_read_narrow_table"):
        try:
            with get_db() as db:
                rows = fetch_narrow_history_rows(db.cursor(), normalized_device_id, active_mode, limit)
            capacity_history = [
                {
                    "time": format_timestamp(row["recorded_at"]),
                    "level": row["level"],
                    "lower_tank_level": row["lower_tank_level"],
                    "source_tank_level": row["lower_tank_level"],
                    "motor": row["motor"],
                    "sensor": row["sensor"],
                    "wifi_rssi": None,
                    "free_heap": None,
                    "cpu_utilization_pct": None,
                    "slave_free_heap": None,
                    "slave_cpu_utilization_pct": None,
                    "node_role": None,
                    "device_type": None,
                }
                for row in reversed(rows)
            ]
            if capacity_history:
                return capacity_history
        except Exception as exc:
            logger.warning("Narrow history read unavailable for %s; using legacy history: %s", normalized_device_id, exc)
    source_clause, source_params = device_source_where_clause()
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT level, lower_tank_level, motor, sensor, wifi_rssi,
                   free_heap, cpu_utilization_pct, slave_free_heap,
                   slave_cpu_utilization_pct, node_role, device_type, created_at
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
                "free_heap": row["free_heap"],
                "cpu_utilization_pct": row["cpu_utilization_pct"],
                "slave_free_heap": row["slave_free_heap"],
                "slave_cpu_utilization_pct": row["slave_cpu_utilization_pct"],
                "node_role": row["node_role"],
                "device_type": row["device_type"],
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


def local_device_logs_url(base_url):
    normalized = normalize_device_base_url(base_url)
    if not normalized:
        return None
    return f"{normalized}/api/logs"


def fetch_local_device_status(base_url, device_id=None):
    if not is_private_device_base_url(base_url):
        raise ValueError("local device URL must be a private LAN address")

    status_url = local_device_status_url(base_url)
    if not status_url:
        raise ValueError("local device URL is not configured")

    normalized_device_id = normalize_device_id(device_id)
    headers = {}
    device_key = configured_device_key_for_id(normalized_device_id)
    if normalized_device_id and device_key:
        headers = {"X-Device-Id": normalized_device_id, "X-Device-Key": device_key}
    username = os.environ.get("SWT_LOCAL_WEB_AUTH_USERNAME", "").strip() or "swtadmin"
    password = fetch_device_local_web_password(normalized_device_id)
    timeout = max(0.5, env_float("LOCAL_DEVICE_STATUS_TIMEOUT_SECONDS", 1.5))
    auth = (username, password) if username and password else None
    response = requests.get(status_url, headers=headers, timeout=timeout)
    if response.status_code in {401, 403}:
        response = requests.get(status_url, headers=headers, auth=auth, timeout=timeout)
    if response.status_code in {401, 403} and username and password:
        session_client = requests.Session()
        session_client.post(
            f"{normalize_device_base_url(base_url)}/login",
            data={"username": username, "password": password},
            timeout=timeout,
        )
        response = session_client.get(status_url, headers=headers, auth=auth, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("local device returned invalid status")
    expected_device_id = normalized_device_id
    returned_device_id = normalize_device_id(payload.get("device_id"))
    if expected_device_id and returned_device_id and returned_device_id != expected_device_id:
        raise ValueError("local device_id does not match requested device")
    payload["device_local_url"] = normalize_device_base_url(base_url)
    return payload


def fetch_local_device_logs(base_url, device_id=None):
    if not is_private_device_base_url(base_url):
        raise ValueError("local device URL must be a private LAN address")

    logs_url = local_device_logs_url(base_url)
    if not logs_url:
        raise ValueError("local device URL is not configured")

    normalized_device_id = normalize_device_id(device_id)
    headers = {}
    device_key = configured_device_key_for_id(normalized_device_id)
    if normalized_device_id and device_key:
        headers = {"X-Device-Id": normalized_device_id, "X-Device-Key": device_key}

    username = os.environ.get("SWT_LOCAL_WEB_AUTH_USERNAME", "").strip() or "swtadmin"
    password = fetch_device_local_web_password(normalized_device_id)
    timeout = max(0.25, env_float("LOCAL_DEVICE_LOG_TIMEOUT_SECONDS", 0.75))
    auth = (username, password) if username and password else None
    response = requests.get(logs_url, headers=headers, auth=auth, timeout=timeout)
    if response.status_code in {401, 403} and username and password:
        session_client = requests.Session()
        session_client.post(
            f"{normalize_device_base_url(base_url)}/login",
            data={"username": username, "password": password},
            timeout=timeout,
        )
        response = session_client.get(logs_url, headers=headers, auth=auth, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("local device returned invalid logs")
    returned_device_id = normalize_device_id(payload.get("device_id"))
    if normalized_device_id and returned_device_id and returned_device_id != normalized_device_id:
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


def fetch_firmware_artifact(artifact_id, device_id=None, role=None):
    try:
        normalized_artifact_id = int(artifact_id)
    except (TypeError, ValueError):
        return None

    if normalized_artifact_id <= 0:
        return None

    normalized_device_id = normalize_device_id(device_id)
    try:
        normalized_role = normalize_firmware_artifact_role(role) if role else None
    except ValueError:
        return None
    query = """
        SELECT id, target_device, target_role, original_filename, stored_filename, version_label, notes,
               md5, size_bytes, content_type, uploaded_by, created_at
        FROM firmware_artifacts
        WHERE id = ?
    """
    params = [normalized_artifact_id]
    if normalized_device_id:
        query += " AND target_device = ?"
        params.append(normalized_device_id)
    if normalized_role:
        query += " AND target_role = ?"
        params.append(normalized_role)

    with get_db() as db:
        row = db.execute(query, tuple(params)).fetchone()
    if not row:
        return None

    artifact = dict(row)
    artifact["storage_path"] = str(firmware_artifact_storage_path(artifact.get("stored_filename")))
    return artifact


def fetch_latest_firmware_artifact(device_id, role="master"):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None
    try:
        normalized_role = normalize_firmware_artifact_role(role)
    except ValueError:
        return None

    with get_db() as db:
        row = db.execute(
            """
            SELECT id
            FROM firmware_artifacts
            WHERE target_device = ? AND target_role = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (normalized_device_id, normalized_role),
        ).fetchone()
    if not row:
        return None
    return fetch_firmware_artifact(row["id"], device_id=normalized_device_id, role=normalized_role)


def fetch_latest_firmware_artifacts_by_role(device_id):
    return {role: fetch_latest_firmware_artifact(device_id, role=role) for role in FIRMWARE_ARTIFACT_ROLES}


def build_firmware_artifact_payload(artifact, target_device=None, download_endpoint=None):
    return build_firmware_artifact_response_payload(
        artifact,
        target_device=target_device,
        download_endpoint=download_endpoint,
        normalize_device_id=normalize_device_id,
    )


def create_firmware_artifact(device_id, uploaded_file, notes="", uploaded_by="admin", role="master", expected_build_flags=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("Choose a valid device before uploading firmware.")
    normalized_role = normalize_firmware_artifact_role(role)

    upload = read_uploaded_firmware(uploaded_file, FIRMWARE_ARTIFACT_MAX_BYTES)
    payload = upload["payload"]
    validate_firmware_binary_role(payload, normalized_role, upload["original_filename"])
    stored_filename = make_stored_firmware_filename(normalized_device_id, normalized_role)
    storage_path = firmware_artifact_storage_path(stored_filename)
    notes_text = str(notes or "").strip() or None

    try:
        storage_path.write_bytes(payload)
    except OSError as exc:
        raise ValueError("Unable to store the uploaded firmware on disk.") from exc

    try:
        with get_db() as db:
            previous_rows = db.execute(
                """
                SELECT id, stored_filename
                FROM firmware_artifacts
                WHERE target_device = ? AND target_role = ?
                """,
                (normalized_device_id, normalized_role),
            ).fetchall()
            cursor = db.execute(
                """
                INSERT INTO firmware_artifacts(
                    target_device, target_role, original_filename, stored_filename, version_label, notes,
                    md5, size_bytes, content_type, uploaded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_device_id,
                    normalized_role,
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
            db.execute(
                """
                DELETE FROM firmware_artifacts
                WHERE target_device = ? AND target_role = ? AND id <> ?
                """,
                (normalized_device_id, normalized_role, artifact_id),
            )
    except Exception as exc:
        try:
            storage_path.unlink()
        except OSError:
            pass
        raise ValueError("Unable to register the uploaded firmware artifact.") from exc

    artifact = fetch_firmware_artifact(artifact_id, device_id=normalized_device_id, role=normalized_role)
    if not artifact:
        raise ValueError("Uploaded firmware artifact could not be loaded after it was saved.")
    active_path = storage_path.resolve()
    for row in previous_rows:
        old_filename = str(row["stored_filename"] or "").strip()
        if not old_filename:
            continue
        old_path = firmware_artifact_storage_path(old_filename).resolve()
        if old_path == active_path:
            continue
        try:
            old_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Unable to remove replaced firmware artifact %s: %s", old_path, exc)
    return artifact


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


def normalize_android_upload_filename_for_release_channel(filename):
    raw_name = Path(str(filename or "")).name.strip() or "smart-water-tank.apk"
    cleaned = re.sub(r"(?i)(debug|unsigned)", "build", raw_name)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", cleaned).strip(" .-_")
    if not cleaned:
        cleaned = "smart-water-tank"
    if not cleaned.lower().endswith(".apk"):
        cleaned = f"{cleaned}.apk"
    return cleaned


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
            previous_rows = db.execute(
                """
                SELECT id, stored_filename
                FROM android_app_releases
                """
            ).fetchall()
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
            db.execute(
                """
                DELETE FROM android_app_releases
                WHERE id <> ?
                """,
                (release_id,),
            )
    except Exception as exc:
        try:
            storage_path.unlink()
        except OSError:
            pass
        raise ValueError("Unable to register the uploaded Android app release.") from exc

    release = fetch_android_app_release(release_id)
    if not release:
        raise ValueError("Uploaded Android app release could not be loaded after it was saved.")
    active_path = storage_path.resolve()
    for row in previous_rows:
        old_filename = str(row["stored_filename"] or "").strip()
        if not old_filename:
            continue
        old_path = android_release_storage_path(old_filename).resolve()
        if old_path == active_path:
            continue
        try:
            old_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Unable to remove replaced Android release %s: %s", old_path, exc)
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


def device_command_family(command):
    normalized_command = str(command or "").strip().upper()
    compact = normalized_command.replace("-", "_").replace(" ", "_")
    if compact in {"ON", "OFF"}:
        return "pump"
    if compact == "REBOOT" or compact.startswith("RESTART"):
        return "reboot"
    if compact.startswith("SERVICECFG"):
        return "service_config"
    if compact.startswith("THRESHOLDS:") or compact.startswith("CONFIG_THRESHOLDS:") or compact.startswith("CONFIG_AUTO:"):
        return "thresholds"
    if (
        compact.startswith("CONFIG_AUTO_START")
        or compact.startswith("AUTO_START")
        or compact.startswith("START_LEVEL")
    ):
        return "threshold_start"
    if (
        compact.startswith("CONFIG_AUTO_STOP")
        or compact.startswith("AUTO_STOP")
        or compact.startswith("STOP_LEVEL")
    ):
        return "threshold_stop"
    if compact.startswith("PEER_CHANNEL:") or compact.startswith("CONFIG_PEER_CHANNEL:"):
        return "peer_channel"
    if (
        compact.startswith("CONFIG_UPPER:")
        or compact.startswith("CONFIG:")
        or compact.startswith("CONFIG_HEIGHT:")
        or compact.startswith("CONFIG_CAPACITY:")
    ):
        return "upper_tank_setup"
    if (
        compact.startswith("CONFIG_LOWER:")
        or compact.startswith("CONFIG_SOURCE:")
        or compact.startswith("CONFIG_LOWER_HEIGHT:")
        or compact.startswith("CONFIG_SOURCE_HEIGHT:")
        or compact.startswith("CONFIG_LOWER_CAPACITY:")
        or compact.startswith("CONFIG_SOURCE_CAPACITY:")
    ):
        return "lower_tank_setup"
    if compact in {"CALIBRATE", "CALIBRATE_UPPER"}:
        return "upper_calibration"
    if compact == "CALIBRATE_LOWER":
        return "lower_calibration"
    if compact.startswith("PING_MASTER") or compact.startswith("PING:MASTER") or compact.startswith("MASTER_PING"):
        return "ping_master"
    if (
        compact.startswith("PING_SLAVE")
        or compact.startswith("PING:SLAVE")
        or compact.startswith("PING_PEER")
        or compact.startswith("PEER_PING")
    ):
        return "ping_slave"
    # Simulator controls are independent.  Do not coalesce them into one
    # queue family: doing so makes a later municipal/valve/turbidity request
    # delete a pending tank-simulator request (and vice versa) before the
    # device has had a chance to poll it.
    if compact in {"SIMULATOR_ON", "SIMULATOR_OFF"}:
        return "simulator:tank"
    if compact.endswith("_SIMULATOR_ON") or compact.endswith("_SIMULATOR_OFF"):
        return f"simulator:{compact.rsplit('_', 1)[0].lower()}"
    return f"command:{compact}"


def queue_device_command(command, target_device, request_id=None, expires_in_seconds=None):
    normalized_target_device = normalize_device_id(target_device)
    if not normalized_target_device:
        raise ValueError("A target device is required for a queued command.")
    normalized_command = str(command or "").strip().upper()
    normalized_family = device_command_family(normalized_command)
    desired_state = "ON" if normalized_command == "ON" or normalized_command.startswith("ON_FOR:") else ("OFF" if normalized_command == "OFF" else None)
    request_id = str(request_id or secrets.token_hex(16))
    if expires_in_seconds is None:
        # A controller can be rebooting or temporarily backing off after a
        # failed HTTPS request.  A one-minute start-command lifetime made the
        # dashboard report "queued" while the device never had another chance
        # to receive it.  OFF remains high priority; starts remain valid long
        # enough for the normal reconnect/poll recovery path.
        expires_in_seconds = 600 if desired_state in {"ON", "OFF"} else 300
    expires_at = (now_utc() + timedelta(seconds=max(1, int(expires_in_seconds)))).strftime(TIMESTAMP_FORMAT)
    # Diagnostic pings must not sit behind a batch of simulator/configuration
    # commands. The browser waits for a nonce-matched result, so deliver pings
    # after safety-critical pump commands but ahead of routine configuration.
    if normalized_family in {"ping_master", "ping_slave"}:
        priority = 40
    else:
        priority = 100 if desired_state == "OFF" else (50 if desired_state == "ON" else 10)
    with get_db() as db:
        existing_request = db.execute(
            "SELECT id FROM device_command_queue WHERE target_device=? AND request_id=? LIMIT 1",
            (normalized_target_device, request_id),
        ).fetchone()
        if existing_request:
            logger.info(
                "Reusing queued command id=%s for device=%s command=%s request_id=%s",
                existing_request["id"], normalized_target_device, normalized_command, request_id,
            )
            return existing_request["id"]
        if desired_state == "OFF":
            cancelled = db.execute(
                """
                DELETE FROM device_command_queue
                WHERE target_device = ?
                  AND delivered_at IS NULL
                  AND (UPPER(command) = 'ON' OR SUBSTR(UPPER(command), 1, 7) = 'ON_FOR:')
                """,
                (normalized_target_device,),
            )
            if cancelled.rowcount:
                logger.info(
                    "Cancelled %s pending pump-start command(s) for device=%s because OFF was queued",
                    cancelled.rowcount, normalized_target_device,
                )
        pending_rows = db.execute(
            """
            SELECT id, command
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL
            ORDER BY id ASC
            """,
            (normalized_target_device,),
        ).fetchall()
        duplicate_ids = [
            row["id"]
            for row in pending_rows
            if device_command_family(row["command"]) == normalized_family
        ]
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            db.execute(
                f"""
                DELETE FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL AND id IN ({placeholders})
                """,
                (normalized_target_device, *duplicate_ids),
            )
        cursor = db.execute(
            """
            INSERT INTO device_command_queue (
                target_device, command, request_id, desired_state, status, priority, expires_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (normalized_target_device, normalized_command, request_id, desired_state, priority, expires_at),
        )
        db.execute(
            """
            DELETE FROM device_command_queue
            WHERE delivered_at IS NOT NULL
              AND delivered_at < datetime('now', '-7 day')
            """
        )
        logger.info(
            "Queued device command id=%s device=%s command=%s priority=%s expires_at=%s request_id=%s",
            cursor.lastrowid, normalized_target_device, normalized_command, priority, expires_at, request_id,
        )
        return cursor.lastrowid


def recent_device_command_row(db, device_id, seconds, command_prefixes=None):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id or seconds <= 0:
        return None

    params = [normalized_device_id, f"-{int(seconds)} seconds"]
    command_clause = ""
    if command_prefixes:
        clauses = []
        for prefix in command_prefixes:
            clauses.append("command LIKE ?")
            params.append(f"{str(prefix or '').strip().upper()}%")
        command_clause = f" AND ({' OR '.join(clauses)})"

    return db.execute(
        f"""
        SELECT id, command, created_at, delivered_at
        FROM device_command_queue
        WHERE target_device = ?
          AND created_at >= datetime('now', ?)
          {command_clause}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def runtime_sync_command_allowed(db, device_id):
    if recent_device_command_row(db, device_id, RUNTIME_SYNC_MANUAL_COMMAND_COOLDOWN_SECONDS):
        return False
    if recent_device_command_row(
        db,
        device_id,
        RUNTIME_SYNC_COMMAND_MIN_INTERVAL_SECONDS,
        command_prefixes=(
            "SERVICECFG",
            "THRESHOLDS:",
            "PEER_CHANNEL:",
            "CONFIG_UPPER:",
            "CONFIG_LOWER:",
            "CONFIG_CAPACITY:",
            "CONFIG_LOWER_CAPACITY:",
        ),
    ):
        return False
    return True


def peek_queued_command(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    with get_db() as db:
        row = db.execute(
            """
            SELECT id, command, request_id, desired_state, expires_at
            FROM device_command_queue
            WHERE target_device = ? AND delivered_at IS NULL
              AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            ORDER BY priority DESC, id ASC
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
        if row:
            db.execute(
                "UPDATE device_command_queue SET status='delivered' WHERE id=?",
                (row["id"],),
            )
            return dict(row)

    with get_db() as db:
        if not runtime_sync_command_allowed(db, normalized_device_id):
            return None

    sync_command = build_runtime_sync_command(normalized_device_id)
    if sync_command and sync_command.get("command"):
        queue_device_command(sync_command["command"], normalized_device_id)
        persist_command_activity_events(normalized_device_id)
        logger.info(
            "Queued runtime sync command for %s: %s (%s)",
            normalized_device_id,
            sync_command["command"],
            sync_command.get("reason") or "runtime_sync",
        )
        with get_db() as db:
            row = db.execute(
                """
                SELECT id, command
                FROM device_command_queue
                WHERE target_device = ? AND delivered_at IS NULL
                ORDER BY id ASC
                LIMIT 1
                """,
                (normalized_device_id,),
            ).fetchone()
            if row:
                return {"id": row["id"], "command": row["command"]}
    return None


def acknowledge_queued_command_id(device_id, command_id, result=None):
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
            WHERE target_device = ? AND id = ?
            LIMIT 1
            """,
            (normalized_device_id, normalized_command_id),
        ).fetchone()
        if not row:
            return False
        result = result if isinstance(result, dict) else {}
        device_status = str(result.get("status") or "accepted").strip().lower()
        final_status = "rejected" if device_status == "rejected" else "accepted"
        motor_state = str(result.get("motor_state") or "").strip().upper()
        if final_status == "accepted" and motor_state in {"RUNNING", "ON", "OFF", "STOPPED"}:
            final_status = "running" if motor_state in {"RUNNING", "ON"} else "stopped"
        db.execute(
            """
            UPDATE device_command_queue
            SET delivered_at = COALESCE(delivered_at, CURRENT_TIMESTAMP),
                accepted_at = CASE WHEN ? <> 'rejected' THEN CURRENT_TIMESTAMP ELSE accepted_at END,
                completed_at = CASE WHEN ? IN ('rejected','running','stopped') THEN CURRENT_TIMESTAMP ELSE completed_at END,
                status = ?, result_reason = ?, result_json = ?
            WHERE id = ?
            """,
            (final_status, final_status, final_status, result.get("reason"), json.dumps(result, default=str), row["id"]),
        )
    persist_command_activity_events(normalized_device_id)
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
    persist_command_activity_events(normalized_device_id)
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


def queue_command(command, target_device=None, request_id=None):
    device_command_target = resolve_command_target(target_device)
    if not device_command_target:
        return {
            "status": "error",
            "error": "No target device is available for this command yet.",
            "command": command,
            "control_policy": CONTROL_POLICY,
        }, 400

    normalized_command = str(command or "").strip().upper()
    request_id = str(request_id or secrets.token_hex(16))
    command_id = queue_device_command(normalized_command, device_command_target, request_id=request_id)
    persist_command_activity_events(device_command_target)
    mqtt_published = publish_mqtt_command(normalized_command, device_command_target)
    result = {
        "status": "queued",
        "command": normalized_command,
        "command_id": command_id,
        "request_id": request_id,
        "command_status": "queued",
        "target_device": device_command_target,
        "queued_at": now_utc().strftime(TIMESTAMP_FORMAT),
        "control_policy": CONTROL_POLICY,
        "mqtt_delivery": "published" if mqtt_published else ("pending" if mqtt_feature_enabled() else "disabled"),
    }
    return result


MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE = "START_FIRMWARE_UPGRADE"


def normalize_mobile_device_action(action):
    normalized_action = str(action or "").strip().upper().replace("-", "_").replace(" ", "_")
    if normalized_action in {"START_FIRMWARE_UPGRADE", "FIRMWARE_UPGRADE", "START_OTA", "OTA_UPGRADE"}:
        return MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE
    raise ValueError("Unsupported mobile action")


def queue_device_mobile_action(action, target_device, payload=None):
    normalized_target_device = normalize_device_id(target_device)
    if not normalized_target_device:
        return {
            "status": "error",
            "error": "No target device is available for this mobile action yet.",
            "action": action,
        }, 400

    try:
        normalized_action = normalize_mobile_device_action(action)
    except ValueError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "action": action,
            "target_device": normalized_target_device,
        }, 400

    payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    with get_db() as db:
        db.execute(
            """
            DELETE FROM device_mobile_action_queue
            WHERE target_device = ? AND delivered_at IS NULL AND UPPER(action) = ?
            """,
            (normalized_target_device, normalized_action),
        )
        insert_cursor = db.execute(
            """
            INSERT INTO device_mobile_action_queue (target_device, action, payload_json)
            VALUES (?, ?, ?)
            """,
            (normalized_target_device, normalized_action, payload_json),
        )
        db.execute(
            """
            DELETE FROM device_mobile_action_queue
            WHERE delivered_at IS NOT NULL
              AND delivered_at < datetime('now', '-7 day')
            """
        )
        queue_id = insert_cursor.lastrowid

    return {
        "status": "queued",
        "queue_id": queue_id,
        "action": normalized_action,
        "target_device": normalized_target_device,
        "payload": payload or {},
        "queued_at": now_utc().strftime(TIMESTAMP_FORMAT),
    }


def pop_device_mobile_action(device_id):
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return None

    with get_db() as db:
        row = db.execute(
            """
            SELECT id, action, payload_json, created_at
            FROM device_mobile_action_queue
            WHERE target_device = ? AND delivered_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (normalized_device_id,),
        ).fetchone()
        if not row:
            return None
        db.execute(
            """
            UPDATE device_mobile_action_queue
            SET delivered_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (row["id"],),
        )

    payload = {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    return {
        "id": row["id"],
        "action": row["action"],
        "payload": payload,
        "queued_at": row["created_at"],
        "delivered_at": now_utc().strftime(TIMESTAMP_FORMAT),
    }


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
                    logger.debug("Relayed telemetry to %s", url)
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
                logger.debug("Relay telemetry failed (%s): %s", url, response.status_code)
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
                logger.debug("Relay telemetry failed (%s): %s", url, exc)
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
                    logger.debug("Relay command failed (%s): %s", url, response.status_code)
                    continue
                payload = response.json()
                command = payload.get("command")
                command_id = payload.get("command_id")
                if command:
                    logger.info("Relayed command from %s: %s", url, command)
                return {"command": command, "command_id": command_id}
            except requests.RequestException as exc:
                logger.debug("Relay command failed (%s): %s", url, exc)
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


def parse_pump_run_duration(value):
    raw_value = str(value or "0").strip()
    if "?" in raw_value:
        # Compatibility for dashboard pages cached before scopedUrl learned to
        # append '&' to paths that already contained duration_minutes.
        raw_value, embedded_query = raw_value.split("?", 1)
        if not embedded_query.startswith("device_id="):
            return -1
    try:
        duration_minutes = int(raw_value or 0)
    except (TypeError, ValueError):
        return -1
    return duration_minutes if duration_minutes in {0, 15, 30} else -1


@app.route("/motor/on", methods=["POST"])
@login_required
@csrf_protect
def motor_on():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    target_device = current_scope_device_id(request.args.get("device_id", type=str))
    summary = load_persisted_dashboard_summary(target_device) or {}
    snapshot = summary.get("snapshot") or {}
    telemetry = str(snapshot.get("telemetry_status") or "no-data").lower()
    if telemetry not in {"live", "recent", "fresh"}:
        return jsonify({"error": "Pump start rejected: device telemetry is not fresh.", "reason": "telemetry_stale"}), 409
    if str(snapshot.get("relay_service") or "ON").upper() not in {"ON", "ENABLED", "ACTIVE"}:
        return jsonify({"error": "Pump start rejected: relay service is disabled.", "reason": "relay_service_disabled"}), 409
    lower_service = str(snapshot.get("lower_tank_service") or "OFF").upper() == "ON"
    if lower_service:
        if str(snapshot.get("lower_sensor") or "").upper() != "OK":
            return jsonify({"error": "Pump start rejected: source sensor is unavailable.", "reason": "source_sensor_stale"}), 409
        if safe_float(snapshot.get("lower_tank_level"), -1) < 20:
            return jsonify({"error": "Pump start rejected: source tank is below 20%.", "reason": "source_tank_below_safe_level"}), 409
    stop_pct = safe_float(snapshot.get("auto_stop_pct"), DEFAULT_DEVICE_AUTO_STOP_PCT)
    if safe_float(snapshot.get("level"), 0) >= stop_pct:
        return jsonify({"error": "Pump start rejected: upper tank is already full.", "reason": "upper_tank_full"}), 409
    request_payload = request.get_json(silent=True) or {}
    duration_minutes = request_payload.get("duration_minutes", request.values.get("duration_minutes", 0))
    duration_minutes = parse_pump_run_duration(duration_minutes)
    if duration_minutes < 0:
        return jsonify({"error": "Run duration must be until full, 15 minutes, or 30 minutes."}), 400
    command = "ON" if duration_minutes == 0 else f"ON_FOR:{duration_minutes * 60}"
    return queue_command(command, target_device=target_device, request_id=request_payload.get("request_id"))


@app.route("/motor/off", methods=["POST"])
@login_required
@csrf_protect
def motor_off():
    response = customer_cloud_feed_block_response()
    if response:
        return response
    return queue_command("OFF", target_device=current_scope_device_id(request.args.get("device_id", type=str)))


@app.route("/motor/command-status/<request_id>")
@login_required
def motor_command_status(request_id):
    target_device = current_scope_device_id(request.args.get("device_id", type=str))
    with get_db() as db:
        row = db.execute(
            """
            SELECT request_id, command, status, created_at AS queued_at, delivered_at,
                   accepted_at, completed_at, result_reason, result_json, expires_at
            FROM device_command_queue
            WHERE target_device = ? AND request_id = ?
            LIMIT 1
            """,
            (target_device, str(request_id)),
        ).fetchone()
        if not row:
            return jsonify({"error": "command not found"}), 404
        result = dict(row)
        if result["status"] in {"queued", "delivered"} and result.get("expires_at") and str(result["expires_at"]) <= now_utc().strftime(TIMESTAMP_FORMAT):
            db.execute("UPDATE device_command_queue SET status='timed_out', completed_at=CURRENT_TIMESTAMP WHERE request_id=?", (str(request_id),))
            result["status"] = "timed_out"
        try:
            result["device_result"] = json.loads(result.pop("result_json") or "{}")
        except (TypeError, ValueError):
            result["device_result"] = {}
    return jsonify(result)


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
            "android_sso_session_limit": authenticated_user.get("android_sso_session_limit", DEFAULT_ANDROID_SSO_SESSION_LIMIT),
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
    if authenticated_user.get("role") == "admin":
        return jsonify(
            {
                "error": "Admin login is only available from the web dashboard.",
                "code": "admin_mobile_login_not_allowed",
            }
        ), 403
    return jsonify(build_mobile_auth_response_payload(authenticated_user))


@app.route("/api/mobile/auth/logout", methods=["POST"])
@mobile_auth_required
def mobile_auth_logout():
    user = resolve_mobile_user() or {}
    clear_active_platform_session(
        SESSION_PLATFORM_ANDROID,
        user.get("role"),
        username=user.get("username"),
        device_id=user.get("device_id"),
        session_id=user.get("platform_session_id"),
    )
    return jsonify({"ok": True})


@app.route("/api/mobile/bootstrap")
@mobile_auth_required
def mobile_bootstrap():
    event_limit = max(1, min(request.args.get("event_limit", default=5, type=int), 30))
    audit_limit = max(1, min(request.args.get("audit_limit", default=5, type=int), 30))
    include_analytics = str(request.args.get("include_analytics", "0")).strip().lower() in {"1", "true", "yes", "on"}
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_mobile_scope_device_id(request.args.get("device_id", type=str))
    viewer = resolve_mobile_user() or {}
    summary = load_persisted_dashboard_summary(scoped_device_id) or empty_dashboard_summary(scoped_device_id)
    # Events, audit, analytics, and monitoring remain materialized for a fast
    # cPanel response, but the Android overview must use the same latest
    # telemetry snapshot as the Flask device table.  Otherwise the configured
    # summary refresh window can leave Android several minutes behind Flask.
    latest_snapshot = load_dashboard_snapshot(scoped_device_id)
    if snapshot_has_live_device_data(latest_snapshot):
        summary = dict(summary)
        summary["snapshot"] = strip_ip_address_fields(
            latest_snapshot,
            keep_device_local_url=True,
        )
    summary = overlay_capacity_snapshot(summary, scoped_device_id, "mobile_read_latest_state")
    public_snapshot = summary.get("snapshot") or build_empty_snapshot_payload(scoped_device_id)
    snapshot = public_snapshot
    service_config = resolve_device_service_config(scoped_device_id, snapshot=public_snapshot)
    payload = dict(summary)
    payload.update({
        "events": list(summary.get("events") or [])[:event_limit],
        "audit": list(summary.get("audit") or [])[:audit_limit],
        "viewer": viewer,
        "service_config": service_config,
        "automation_settings": fetch_device_automation_settings(scoped_device_id, snapshot=snapshot),
        "current_saved_config": build_current_saved_config(scoped_device_id),
        "mobile_action": pop_device_mobile_action(scoped_device_id),
    })
    if viewer.get("role") == "admin":
        payload["ops"] = build_ops_dashboard_payload(snapshot, device_id=scoped_device_id, audit_limit=audit_limit)
    if include_analytics and current_customer_ai_analysis_enabled():
        try:
            start_dt, end_exclusive, label = resolve_date_window()
            payload["analytics"] = build_dashboard_analytics(start_dt, end_exclusive, label, device_id=scoped_device_id)
        except Exception as exc:
            logger.exception("Mobile bootstrap analytics fallback used for %s: %s", scoped_device_id, exc)
            try:
                start_dt, end_exclusive, label = resolve_date_window()
                payload["analytics"] = build_analytics_fallback_payload(
                    start_dt,
                    end_exclusive,
                    label,
                    device_id=scoped_device_id,
                    reason="AI analysis is using the last successful result while fresh analytics catches up.",
                )
            except Exception:
                payload["analytics_warning"] = "AI analysis is temporarily unavailable."
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
    try:
        payload = build_dashboard_analytics_singleflight(start_dt, end_exclusive, label, device_id=scoped_device_id)
    except Exception as exc:
        logger.exception("Mobile analytics fallback used for %s: %s", scoped_device_id, exc)
        payload = build_analytics_fallback_payload(
            start_dt,
            end_exclusive,
            label,
            device_id=scoped_device_id,
            reason="AI analysis is using the last successful result while fresh analytics catches up.",
        )
    payload = attach_default_chart_windows(payload, device_id=scoped_device_id)
    return jsonify(payload)


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
    # Keep the mobile request on the same short ingestion path as device HTTP
    # telemetry. Event synchronization, alert evaluation, and dashboard
    # materialization are database-heavy and must not occupy an LSAPI child.
    cleaned = process_telemetry_payload(
        data,
        source_ip="android_local_wifi",
        transport="android_local_wifi",
        defer_postprocess=True,
    )
    snapshot = load_dashboard_snapshot(scoped_device_id)
    sync_result = str(cleaned.get("_telemetry_sync_result") or "saved")
    return jsonify(
        {
            "status": "ok" if sync_result != "duplicate" else "duplicate",
            "result": sync_result,
            "duplicate": sync_result == "duplicate",
            "device_id": scoped_device_id,
            "sync_fingerprint": cleaned.get("telemetry_fingerprint"),
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
        return jsonify({"error": "Admin mobile access is not available.", "code": "admin_mobile_not_allowed"}), 403

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
    snapshot = load_dashboard_snapshot(
        scoped_device_id,
        prefer_capacity=CAPACITY_FEATURES.enabled("mobile_read_latest_state"),
    )
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


@app.route("/api/mobile/device/local-auth/reset", methods=["POST"])
@mobile_auth_required
def mobile_device_local_auth_reset():
    response = mobile_customer_cloud_feed_block_response()
    if response:
        return response
    return jsonify({"ok": True, "message": "Local firmware credentials are fixed in the app and firmware build."})


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
    upsert_device_service_config(
        target_device,
        tank_height_cm=height_cm,
        tank_capacity_liters=capacity_liters,
        upper_tank_height_cm=height_cm,
        upper_tank_capacity_liters=capacity_liters,
    )
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
    snapshot = load_dashboard_snapshot(
        scoped_device_id,
        prefer_capacity=CAPACITY_FEATURES.enabled("mobile_read_latest_state"),
    )
    service_config = resolve_device_service_config(scoped_device_id, snapshot=snapshot)
    return jsonify({
        "snapshot": strip_ip_address_fields(snapshot, keep_device_local_url=True),
        "system_status": build_system_status_payload(snapshot, device_id=scoped_device_id),
        "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
        "service_config": service_config,
        "automation_settings": fetch_device_automation_settings(scoped_device_id, snapshot=snapshot),
        "current_saved_config": build_current_saved_config(scoped_device_id),
        "mobile_action": pop_device_mobile_action(scoped_device_id),
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
    target_device = current_mobile_scope_device_id(requested_device_id)
    if not target_device:
        return jsonify({"error": "device not found"}), 404

    if request.method == "GET":
        snapshot = fetch_device_snapshot(target_device)
        return jsonify(
            {
                "device_id": target_device,
                "config": fetch_device_service_config(target_device, snapshot=snapshot),
                "automation_settings": fetch_device_automation_settings(target_device, snapshot=snapshot),
                "current_saved_config": build_current_saved_config(target_device),
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
        master_upper_sensor_enabled=source_payload.get("master_upper_sensor_enabled"),
        slave_device_enabled=source_payload.get("slave_device_enabled"),
        slave_upper_sensor_enabled=source_payload.get("slave_upper_sensor_enabled"),
        source_tank_monitoring_enabled=source_payload.get("source_tank_monitoring_enabled"),
        relay_enabled=source_payload.get("relay_enabled"),
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
        "current_saved_config": build_current_saved_config(target_device),
        "queued_command": command,
    }
    if isinstance(queue_result, tuple):
        error_payload, status_code = queue_result
        response_payload.update({"queue_error": error_payload.get("error")})
        return jsonify(response_payload), status_code
    response_payload.update(queue_result)
    return jsonify(response_payload)


@app.route("/api/mobile/device/thresholds", methods=["GET", "POST"])
@mobile_auth_required
def mobile_device_thresholds():
    source_payload = request.get_json(silent=True) or {}
    requested_device_id = (
        source_payload.get("device_id")
        if request.method == "POST"
        else request.args.get("device_id", type=str)
    )
    target_device = current_mobile_scope_device_id(requested_device_id)
    if not target_device:
        return jsonify({"error": "device not found"}), 404

    snapshot = fetch_device_snapshot(target_device)
    if request.method == "GET":
        return jsonify(
            {
                "device_id": target_device,
                "settings": fetch_device_automation_settings(target_device, snapshot=snapshot),
                "current_saved_config": build_current_saved_config(target_device),
            }
        )

    user = resolve_mobile_user()
    if not user or user.get("role") not in {"customer", "admin"}:
        return jsonify({"error": "mobile access required"}), 403

    try:
        updated_settings = upsert_device_automation_settings(
            target_device,
            auto_start_pct=_resolve_threshold_value(
                source_payload,
                "auto_start_pct",
                "auto_start_level_pct",
                "lower_threshold_pct",
            ),
            auto_stop_pct=_resolve_threshold_value(
                source_payload,
                "auto_stop_pct",
                "auto_stop_level_pct",
                "upper_threshold_pct",
            ),
            snapshot=snapshot,
            source="mobile_api",
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    command = build_device_automation_command(updated_settings)
    queue_result = queue_command(command, target_device=target_device)
    log_audit_event(
        actor=user.get("username") or current_actor_username(),
        action="update_device_thresholds_mobile",
        target_type="device",
        target_id=target_device,
        device_id=target_device,
        details={
            "automation_settings": updated_settings,
            "queued_command": command,
            "source": "mobile_api",
        },
    )
    response_payload = {
        "message": (
            f"Tank thresholds saved for {target_device}. "
            f"Start at {updated_settings['auto_start_pct']:g}% and stop at {updated_settings['auto_stop_pct']:g}%."
        ),
        "device_id": target_device,
        "settings": updated_settings,
        "current_saved_config": build_current_saved_config(target_device),
        "queued_command": command,
    }
    if isinstance(queue_result, tuple):
        error_payload, status_code = queue_result
        response_payload.update({"queue_error": error_payload.get("error")})
        return jsonify(response_payload), status_code
    response_payload.update(queue_result)
    return jsonify(response_payload)


@app.route("/api/mobile/device/peer-channel", methods=["GET", "POST"])
@mobile_auth_required
def mobile_device_peer_channel():
    source_payload = request.get_json(silent=True) or {}
    requested_device_id = (
        source_payload.get("device_id")
        if request.method == "POST"
        else request.args.get("device_id", type=str)
    )
    target_device = current_mobile_scope_device_id(requested_device_id)
    if not target_device:
        return jsonify({"error": "device not found"}), 404

    snapshot = fetch_device_snapshot(target_device)
    if request.method == "GET":
        return jsonify(
            {
                "device_id": target_device,
                "direct_peer_wifi_channel": (
                    build_current_saved_config(target_device).get("direct_peer_wifi_channel")
                    or (snapshot or {}).get("direct_peer_config_channel")
                    or (snapshot or {}).get("direct_peer_wifi_channel")
                ),
                "current_saved_config": build_current_saved_config(target_device),
            }
        )

    user = resolve_mobile_user()
    if not user or user.get("role") not in {"customer", "admin"}:
        return jsonify({"error": "mobile access required"}), 403

    try:
        requested_channel = _coerce_optional_peer_channel_value(
            source_payload.get("direct_peer_wifi_channel", source_payload.get("peer_channel"))
        )
        if requested_channel is None:
            raise ValueError("Peer channel is required.")
        updated_config = upsert_device_service_config(
            target_device,
            direct_peer_wifi_channel=requested_channel,
        )
        queued_command = build_device_peer_channel_command(requested_channel)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    queue_result = queue_command(queued_command, target_device=target_device)
    log_audit_event(
        actor=user.get("username") or current_actor_username(),
        action="update_device_peer_channel_mobile",
        target_type="device",
        target_id=target_device,
        device_id=target_device,
        details={
            "direct_peer_wifi_channel": requested_channel,
            "service_config": updated_config,
            "queued_command": queued_command,
            "source": "mobile_api",
        },
    )
    response_payload = {
        "message": f"Peer channel saved for {target_device}. Channel {requested_channel} will apply on the next device command poll.",
        "device_id": target_device,
        "direct_peer_wifi_channel": requested_channel,
        "service_config": updated_config,
        "current_saved_config": build_current_saved_config(target_device),
        "queued_command": queued_command,
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
    fetch_device_service_config=fetch_device_service_config,
    build_firmware_artifact_payload=build_firmware_artifact_payload,
    configured_device_key_for_id=configured_device_key_for_id,
    firmware_artifact_storage_path=firmware_artifact_storage_path,
    build_firmware_artifact_file_response=build_firmware_artifact_file_response,
    logger=logger,
)


def device_sync_next_interval(telemetry):
    # Keep the optional combined-sync hint aligned with firmware, Android, and
    # dashboard cloud polling. Local safety/control loops remain independent.
    return CLOUD_POLL_INTERVAL_SECONDS


@app.route("/api/device/sync", methods=["POST"])
def device_sync():
    if not CAPACITY_FEATURES.enabled("device_sync_api"):
        return jsonify({"error": "device sync protocol is not enabled", "fallback": "/status"}), 404

    if CAPACITY_FEATURES.enabled("device_request_limits"):
        max_bytes = max(1024, min(env_int("DEVICE_SYNC_MAX_PAYLOAD_BYTES", 8192), 65536))
        if request.content_length is not None and request.content_length > max_bytes:
            return jsonify({"error": "device sync payload is too large", "max_bytes": max_bytes}), 413

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid json"}), 400
    try:
        protocol_version = int(payload.get("protocol_version", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "protocol_version must be an integer"}), 400
    if protocol_version != 1:
        return jsonify({"error": "unsupported protocol_version", "supported_versions": [1]}), 400
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict):
        return jsonify({"error": "telemetry object is required"}), 400

    auth_payload_source = dict(payload)
    auth_payload_source.setdefault("device_id", telemetry.get("device_id"))
    auth_ok, auth_payload, auth_status = authenticate_device_request(auth_payload_source)
    if not auth_ok:
        return auth_payload, auth_status
    device_id = auth_payload
    if CAPACITY_FEATURES.enabled("device_rate_limiting"):
        allowed, retry_after = DEVICE_SYNC_RATE_LIMITER.allow(device_id)
        if not allowed:
            response = jsonify({"error": "device sync rate limit exceeded", "retry_after": retry_after})
            response.status_code = 429
            response.headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
            return response
    telemetry = dict(telemetry)
    telemetry["device_id"] = device_id
    for field in ("boot_id", "sequence_number", "sequence", "device_reported_at"):
        if field in payload and field not in telemetry:
            telemetry[field] = payload[field]
    try:
        telemetry["device_source"] = resolve_request_device_source(telemetry)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    sequence_result = "new"
    if CAPACITY_FEATURES.enabled("sequence_deduplication"):
        with get_db() as db:
            sequence_result = sequence_status(
                db.cursor(), device_id, telemetry["device_source"], telemetry
            )
        if sequence_result == "missing":
            return jsonify({"error": "boot_id and sequence_number are required"}), 400
        if sequence_result == "replay" and CAPACITY_FEATURES.enabled("replay_protection"):
            return jsonify({"error": "replayed sequence_number", "device_id": device_id}), 409

    command_ack = payload.get("command_ack")
    acknowledgement = None
    if CAPACITY_FEATURES.enabled("sync_command_ack") and isinstance(command_ack, dict):
        command_source = str(command_ack.get("command_source") or "queue").strip().lower()
        if command_source == "relay":
            acknowledged = acknowledge_relay_command(
                device_id,
                command_ack.get("command_id"),
                device_source=telemetry["device_source"],
            )
        else:
            acknowledged = acknowledge_queued_command_id(
                device_id,
                command_ack.get("command_id"),
                result=command_ack,
            )
            if acknowledged:
                clear_mqtt_command(device_id)
        acknowledgement = {
            "acknowledged": bool(acknowledged),
            "command_id": command_ack.get("command_id"),
            "command_source": command_source,
        }

    if sequence_result == "duplicate":
        ingestion = DeviceIngestionResult(telemetry=telemetry, outcome="duplicate")
    else:
        ingestion = ingest_device_sync(
            telemetry,
            authenticated_device_id=device_id,
            source_ip=request.remote_addr,
            transport="device_sync",
            defer_postprocess=True,
        )
    command = None
    if CAPACITY_FEATURES.enabled("sync_command_delivery"):
        queued = peek_queued_command(device_id)
        if queued:
            command = {
                "command": queued.get("command"),
                "command_id": queued.get("id"),
                "request_id": queued.get("request_id"),
                "desired_state": queued.get("desired_state"),
                "expires_at": queued.get("expires_at"),
                "command_source": "queue",
            }

    response_payload = {
        "accepted": True,
        "duplicate": ingestion.duplicate,
        "result": ingestion.outcome,
        "protocol_version": protocol_version,
        "server_time": now_utc().strftime(TIMESTAMP_FORMAT),
        "device_id": device_id,
        "device_source": telemetry["device_source"],
        "configuration_version": None,
        "command": command,
    }
    if CAPACITY_FEATURES.enabled("sync_interval_hints"):
        response_payload["next_sync_seconds"] = device_sync_next_interval(telemetry)
    if acknowledgement is not None:
        response_payload["command_ack"] = acknowledgement
    if CAPACITY_FEATURES.enabled("staged_rollout"):
        response_payload["rollout"] = evaluate_device_rollout(device_id, os.environ)
    return jsonify(response_payload)


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
            "request_id": queued.get("request_id"),
            "desired_state": queued.get("desired_state"),
            "expires_at": queued.get("expires_at"),
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
        acknowledged = acknowledge_queued_command_id(device_id, command_id, result=payload)
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


@app.route("/device/firmware/<int:artifact_id>/download")
def device_firmware_artifact_download(artifact_id):
    auth_ok, auth_payload, auth_status = authenticate_device_request()
    if not auth_ok:
        return auth_payload, auth_status
    device_id = auth_payload
    try:
        target_role = normalize_firmware_artifact_role(request.args.get("role", "master", type=str))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    artifact = fetch_firmware_artifact(artifact_id, device_id=device_id, role=target_role)
    if not artifact:
        return jsonify({"error": "firmware artifact not found"}), 404

    storage_path = firmware_artifact_storage_path(artifact.get("stored_filename"))
    if not storage_path.is_file():
        logger.warning("Firmware artifact %s is registered but missing on disk: %s", artifact_id, storage_path)
        return jsonify({"error": "firmware artifact file is missing"}), 404

    return build_firmware_artifact_file_response(artifact, storage_path)


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
    sales_success = None
    if request.args.get("enquiry") == "success":
        sales_success = "Thanks for your enquiry. Our team will follow up with pricing, installation guidance, or a demo."
    elif request.args.get("enquiry") == "saved_email_pending":
        sales_success = "Thanks for your enquiry. Your request was saved, but support email delivery needs SMTP checking."
    return render_template(
        "pricing.html",
        sales_success=sales_success,
        sales_error=None,
        sales_form=sales_form_from_pricing_query(),
        open_booking_modal=bool(sales_success),
    )


@app.route("/integrations/whatsapp/send", methods=["POST"])
def whatsapp_send_integration():
    if not WHATSAPP_WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "webhook_secret_not_configured"}), 503
    supplied_secret = request.headers.get("X-SaleWell-Webhook-Secret", "")
    if not hmac.compare_digest(supplied_secret, WHATSAPP_WEBHOOK_SECRET):
        return jsonify({"ok": False, "error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    if payload.get("channel") != "whatsapp":
        return jsonify({"ok": False, "error": "invalid_channel"}), 400

    sent, status, http_status = send_meta_whatsapp_payload(payload)
    response_status = 200 if sent else http_status
    return jsonify({"ok": sent, "status": status, "provider": WHATSAPP_PROVIDER}), response_status


@app.route("/sales/enquiry", methods=["GET", "POST"])
@app.route("/sales/enquiry/", methods=["GET", "POST"])
@app.route("/book-demo", methods=["GET", "POST"])
@csrf_protect
def sales_enquiry():
    if request.method == "GET":
        return render_template(
            "pricing.html",
            sales_success=None,
            sales_error=None,
            sales_form=sales_form_from_pricing_query(),
            open_booking_modal=True,
        )

    landing_mode = "admin" if request.form.get("landing_mode") == "admin" else "customer"
    next_url = resolve_next_url(dashboard_home_url("customer"))
    return_to = str(request.form.get("return_to") or "").strip().lower()
    if return_to not in {"pricing", "homepage"}:
        referrer_path = urlparse(request.referrer or "").path
        return_to = "homepage" if referrer_path in {url_for("dashboard"), url_for("homepage")} else "pricing"
    cleaned, errors = validate_sales_enquiry_payload(request.form)

    if errors:
        sales_error = " ".join(errors)
        if return_to == "pricing":
            return render_template(
                "pricing.html",
                sales_success=None,
                sales_error=sales_error,
                sales_form=cleaned,
                open_booking_modal=True,
            ), 400
        return render_login_page(
            mode=landing_mode,
            next_url=next_url,
            sales_error=sales_error,
            sales_form=cleaned,
            show_login_modal=False,
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
    whatsapp_team_sent, whatsapp_customer_sent = send_sales_enquiry_whatsapp_messages(cleaned, lead_details)
    append_sales_enquiry_backup(
        cleaned,
        lead_details,
        support_email_sent=support_email_sent,
        confirmation_email_sent=confirmation_email_sent,
        whatsapp_team_sent=whatsapp_team_sent,
        whatsapp_customer_sent=whatsapp_customer_sent,
    )
    logger.info(
        "Sales enquiry submitted for %s (%s). support_email_sent=%s confirmation_email_sent=%s whatsapp_team_sent=%s whatsapp_customer_sent=%s backup=%s",
        cleaned["name"],
        cleaned["phone"],
        support_email_sent,
        confirmation_email_sent,
        whatsapp_team_sent,
        whatsapp_customer_sent,
        SALES_ENQUIRY_BACKUP_PATH,
    )
    enquiry_status = "success" if support_email_sent else "saved_email_pending"
    if return_to == "pricing":
        return redirect(url_for("pricing_page", enquiry=enquiry_status))
    return redirect(url_for("dashboard", enquiry=enquiry_status))


@app.route("/logout", methods=["POST"])
@login_required
@csrf_protect
def logout():
    clear_active_platform_session(
        SESSION_PLATFORM_DASHBOARD,
        session.get("role"),
        username=session.get("username"),
        device_id=session.get("device_id"),
        session_id=session.get("platform_session_id"),
    )
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
    error = request.args.get("error", "", type=str) or None
    success = request.args.get("success", "", type=str) or None
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


@app.route("/admin/customers/device-table.json")
@admin_required
def admin_customers_device_table_json():
    accounts = list_customer_accounts(limit=100)
    available_devices = load_admin_known_devices(accounts, inventory_limit=100)
    device_summary = build_admin_device_summary(available_devices)
    devices = []
    for device in available_devices:
        master_node_label = str(device.get("master_status_label") or "Unreachable").replace("Master ", "").replace("Slave ", "")
        slave_node_label = str(device.get("slave_status_label") or "Unreachable").replace("Master ", "").replace("Slave ", "")
        last_sync_at = device.get("last_sync_at") or ""
        has_sync = bool(last_sync_at)
        level = device.get("level")
        active_alert_count = device.get("active_alert_count") or 0
        devices.append(
            {
                "device_id": device.get("device_id") or "",
                "search": (
                    f"{device.get('device_id') or ''} {device.get('source_ip') or ''} "
                    f"{device.get('device_local_host') or ''} {device.get('display_name') or ''} "
                    f"{device.get('email') or ''} {device.get('cloud_device_id') or ''} "
                    f"{device.get('firmware_version') or device.get('swt_version') or ''}"
                ).lower(),
                "status_rank": device.get("status_sort_value") or 1,
                "alert_count": active_alert_count,
                "has_alerts": active_alert_count > 0,
                "master_status": master_node_label,
                "master_status_tone": device.get("master_status_tone") or "offline",
                "slave_status": slave_node_label,
                "slave_status_tone": device.get("slave_status_tone") or "offline",
                "telemetry_status": device.get("telemetry_status") or "no-data",
                "telemetry_status_label": device.get("telemetry_status_label") or "--",
                "wifi_rssi": device.get("wifi_rssi") if device.get("wifi_rssi") is not None else "--",
                "upper_sensor_status": device.get("upper_sensor_status_label") or "Unreachable",
                "upper_sensor_status_tone": device.get("upper_sensor_status_tone") or "offline",
                "lower_sensor_status": device.get("lower_sensor_status_label") or "Unreachable",
                "lower_sensor_status_tone": device.get("lower_sensor_status_tone") or "offline",
                "municipal_sensor_status": device.get("municipal_sensor_status_label") or "Disabled",
                "municipal_sensor_status_tone": device.get("municipal_sensor_status_tone") or "clear",
                "tank_level": f"{level}%" if has_sync and level is not None else "--",
                "pump_mode": f"Pump {device.get('motor') or '--'} / {device.get('mode') or '--'}" if has_sync else "Pump --",
                "depth_echo": (
                    f"Depth {device.get('water_depth_label') or '--'} / Echo {device.get('sensor_distance_label') or '--'}"
                    if has_sync else "Depth -- / Echo --"
                ),
                "alert_label": device.get("warning_alert_label") or "Clear",
                "alert_tone": (
                    "danger" if device.get("latest_alert_severity") == "danger"
                    else "warning" if device.get("latest_alert_severity") == "warning"
                    else "clear" if active_alert_count == 0
                    else "info"
                ),
                "health_score": int(device.get("admin_health_score") or 0),
                "health_tone": device.get("admin_health_tone") or "danger",
                "last_seen_age_seconds": device.get("last_seen_age_seconds"),
                "last_sync_at": last_sync_at,
            }
        )
    return jsonify({
        "devices": devices,
        "count": len(devices),
        "summary": device_summary,
        "updated_at": now_utc().isoformat(),
    })


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
        role = request.form.get("firmware_role", "master")
        allow_profile_mismatch = any(
            boolish_enabled(value, default=False)
            for value in request.form.getlist("allow_profile_mismatch")
        )
        try:
            normalized_role = normalize_firmware_artifact_role(role)
            profile_validation_bypassed = normalized_role == "master"
            artifact = create_firmware_artifact(
                normalized_device_id,
                firmware_file,
                notes=notes,
                uploaded_by=current_actor_username(),
                role=normalized_role,
                expected_build_flags=None,
            )
            firmware_role = normalize_firmware_artifact_role(artifact.get("target_role") or normalized_role)
            log_audit_event(
                actor=current_actor_username(),
                action="upload_device_firmware_artifact",
                target_type="device",
                target_id=normalized_device_id,
                device_id=normalized_device_id,
                details={
                    "artifact_id": artifact["id"],
                    "role": firmware_role,
                    "version_label": artifact.get("version_label"),
                    "original_filename": artifact.get("original_filename"),
                    "md5": artifact.get("md5"),
                    "size_bytes": artifact.get("size_bytes"),
                    "notes": artifact.get("notes"),
                    "delivery": "android_local_wifi",
                    "profile_validation": "bypassed" if profile_validation_bypassed else "not_applicable",
                    "allow_profile_mismatch": bool(allow_profile_mismatch or profile_validation_bypassed),
                },
            )
            version_suffix = f" ({artifact['version_label']})" if artifact.get("version_label") else ""
            mismatch_note = (
                " Install-profile validation is advisory only; save and apply the correct runtime configuration before upgrading the device."
                if profile_validation_bypassed
                else ""
            )
            success = (
                f"{firmware_role.title()} firmware uploaded for {normalized_device_id}. "
                f"{artifact['original_filename']}{version_suffix} is now available to the Android app for local Wi-Fi upgrades."
                f"{mismatch_note}"
            )
        except ValueError as exc:
            error = str(exc)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        payload = {
            "ok": not bool(error),
            "message": success or "",
            "error": error or "",
        }
        return jsonify(payload), 200 if not error else 400

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


@app.route("/admin/releases/android", methods=["POST"])
@admin_required
@csrf_protect
def admin_android_release_upload():
    error = None
    success = None
    search_query = request.values.get("q", "", type=str) or ""

    apk_file = request.files.get("apk_file")
    if apk_file is not None and apk_file.filename:
        apk_file.filename = normalize_android_upload_filename_for_release_channel(apk_file.filename)
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

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        payload = {
            "ok": not bool(error),
            "message": success or "",
            "error": error or "",
        }
        return jsonify(payload), 200 if not error else 400

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
        municipal_feature_enabled = "municipal_sensor_enabled" in request.form
        source_tank_enabled = "source_tank_monitoring_enabled" in request.form
        updated_config = upsert_device_service_config(
            normalized_device_id,
            main_sensor_enabled=("main_sensor_enabled" in request.form),
            slave_device_enabled=("slave_device_enabled" in request.form),
            source_tank_monitoring_enabled=source_tank_enabled,
            municipal_sensor_enabled=municipal_feature_enabled,
            municipal_valve_enabled=municipal_feature_enabled and source_tank_enabled and ("municipal_valve_enabled" in request.form),
            source_outlet_valve_enabled=municipal_feature_enabled and source_tank_enabled and ("source_outlet_valve_enabled" in request.form),
            master_turbidity_enabled=("master_turbidity_enabled" in request.form),
            slave_turbidity_enabled=("slave_turbidity_enabled" in request.form),
            ai_analysis_enabled=ai_analysis_enabled,
            cloud_feed_mode=cloud_feed_mode,
            ota_enabled=False,
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


@app.route("/devices/<device_id>/mobile/firmware-upgrade", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_mobile_firmware_upgrade(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    config_error = None
    config_message = None

    if not scoped_device_id:
        config_error = "Choose a valid device before queueing an Android OTA trigger."
    else:
        service_config = fetch_device_service_config(scoped_device_id)
        if not service_config.get("cloud_feed_enabled", True):
            config_error = "Enable Cloud Feed before queueing an Android OTA trigger."
        elif not service_config.get("local_firmware_upload_enabled", False):
            config_error = "Enable Local firmware upload before queueing an Android OTA trigger."
        else:
            queue_result = queue_device_mobile_action(
                MOBILE_DEVICE_ACTION_START_FIRMWARE_UPGRADE,
                scoped_device_id,
                payload={
                    "message": "Flask requested a firmware upgrade.",
                    "device_id": scoped_device_id,
                    "source": "device_detail",
                },
            )
            if isinstance(queue_result, tuple):
                payload, _status_code = queue_result
                config_error = payload.get("error") or f"Unable to queue an Android OTA trigger for {scoped_device_id}."
            else:
                log_audit_event(
                    actor=current_actor_username(),
                    action="queue_android_firmware_upgrade",
                    target_type="device",
                    target_id=scoped_device_id,
                    device_id=scoped_device_id,
                    details={
                        "action": queue_result.get("action"),
                        "queued_at": queue_result.get("queued_at"),
                        "payload": queue_result.get("payload"),
                    },
                )
                config_message = (
                    f"Android OTA trigger queued for {scoped_device_id}. "
                    "The Android app will start its next firmware upgrade sync on the next cloud refresh."
                )

    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id or device_id,
            config_error=config_error or "",
            config_message=config_message or "",
        )
    )


@app.route("/admin/customers/<device_id>/delete", methods=["GET", "POST"])
@admin_required
@csrf_protect
def admin_delete_known_device(device_id):
    search_query = request.values.get("q", "", type=str) or ""
    normalized_device_id = normalize_device_id(device_id)
    if request.method == "GET":
        return redirect(
            url_for(
                "admin_customers",
                q=search_query,
                error=f"Delete for {normalized_device_id} must be submitted from the admin dashboard form.",
            )
        )

    logger.info("Admin device delete requested for %s", normalized_device_id)

    try:
        deleted_counts = delete_known_device(normalized_device_id)
        total_deleted = deleted_row_total(deleted_counts)
        log_audit_event(
            actor=current_actor_username(),
            action="delete_known_device",
            target_type="device",
            target_id=normalized_device_id,
            details={
                "deleted_rows": total_deleted,
                "deleted_counts": deleted_counts,
            },
        )
        success = (
            f"Deleted device {normalized_device_id} and removed {total_deleted} stored row"
            f"{'' if total_deleted == 1 else 's'}. Future check-ins are ignored until the device is registered again."
        )
    except ValueError as exc:
        return redirect(
            url_for(
                "admin_customers",
                q=search_query,
                error=str(exc),
            )
        )
    except Exception:
        logger.exception("Admin device delete failed for %s", normalized_device_id)
        return redirect(
            url_for(
                "admin_customers",
                q=search_query,
                error=f"Delete failed for {normalized_device_id}. Check the server log for details.",
            )
        )

    return redirect(
        url_for(
            "admin_customers",
            q=search_query,
            success=success,
        )
    )


@app.route("/")
def dashboard():
    homepage_visitor_count = increment_homepage_visitor_count()
    return render_login_page(
        mode="customer",
        next_url=resolve_next_url(dashboard_home_url("customer")),
        homepage_visitor_count=homepage_visitor_count,
    )


@app.route("/homepage")
def homepage():
    homepage_visitor_count = increment_homepage_visitor_count()
    return render_login_page(
        mode="customer",
        next_url=resolve_next_url(dashboard_home_url("customer")),
        homepage_visitor_count=homepage_visitor_count,
    )


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    return redirect(url_for("admin_customers"))


SURVEY_QUESTION_LABELS = {
    "survey_date": "Preferred survey date", "preferred_visit_time": "Preferred visit time",
    "alternate_phone": "Alternate phone", "installation_address": "Installation address",
    "landmark_city_pin": "Landmark, city and PIN", "property_type": "Property type",
    "tank_access": "Tank access", "roof_height": "Roof/platform height from ground",
    "safe_working_space": "Safe working space", "site_hazards": "Site hazards",
    "access_safety_notes": "Access/safety notes", "water_source": "Water source",
    "pump_control": "Existing pump control", "pump_rating": "Pump rating (HP/kW)",
    "pump_location": "Pump location", "rising_pipe": "Rising pipe size/material",
    "pipe_run": "Approximate pipe run", "current_issues": "Current water-system issues",
    "existing_system_notes": "Existing valves, automation or issue details",
    "tank1_details": "Tank 1: capacity, elevations, pipes, overflow and condition",
    "tank2_details": "Tank 2: capacity, elevations, pipes, overflow and condition",
    "tank1_capacity": "Tank 1 capacity (litres)", "different_height_notes": "Different-height tank assessment",
    "bottoms_connected": "Tank bottoms connected", "lower_tank_overflows": "Lower tank overflows",
    "recommended_control": "Recommended control arrangement", "hydraulic_notes": "Hydraulic arrangement and reason",
    "power_near_controller": "Power near controller", "earthing": "Earthing available",
    "connectivity": "Connectivity", "supply_voltage": "Supply voltage",
    "weatherproof_enclosure": "Weatherproof enclosure needed", "controller_location": "Controller location",
    "cable_route_length": "Cable route length", "sensor_locations": "Tank sensor types/locations",
    "customer_requirements": "Customer requirements", "preferred_installation_date": "Special requirements / preferred installation date",
    "consent": "Quotation-preparation consent",
}
SURVEY_TEST_DEVICE_ID = "swt-test-000-000-001"


def parse_survey_answers(response):
    try:
        payload = json.loads((response or {}).get("answers_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def survey_device_setup_defaults(response, answers=None):
    """Create an editable setup proposal from survey facts; it is not registered yet."""
    answers = answers or parse_survey_answers(response)
    water_source = str(answers.get("water_source") or "").strip().lower()
    tank2 = str(answers.get("tank2_details") or "").strip().lower()
    requirements = " ".join(
        str(answers.get(key) or "")
        for key in ("customer_requirements", "current_issues", "recommended_control")
    ).lower()
    municipal = water_source in {"municipal", "multiple sources"}
    source_tank = water_source != "municipal"
    multiple_sources = water_source == "multiple sources"
    has_second_tank = bool(tank2 and tank2 not in {"not applicable", "n/a", "none", "no"})
    capacity = normalize_optional_config_float(answers.get("tank1_capacity"))
    return {
        "device_setup_type": "hybrid" if multiple_sources else ("municipal_only" if municipal and not source_tank else "source_only"),
        "source_tank_monitoring_enabled": source_tank,
        "municipal_sensor_enabled": municipal,
        "municipal_valve_enabled": multiple_sources,
        "source_outlet_valve_enabled": multiple_sources,
        "slave_device_enabled": has_second_tank,
        "auto_mode_enabled": "automatic" in requirements or "auto" in requirements,
        "ai_analysis_enabled": True,
        "cloud_feed_mode": DEVICE_SERVICE_CLOUD_FEED_FULL,
        "relay_enabled": True,
        "buzzer_enabled": True,
        "led_display_enabled": True,
        "local_firmware_upload_enabled": True,
        "upper_tank_capacity_liters": capacity,
        "tank_capacity_liters": capacity,
    }


def survey_registration_float(form, name, minimum=None, maximum=None):
    raw = str(form.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name.replace('_', ' ').title()} must be a number.") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name.replace('_', ' ').title()} must be at least {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name.replace('_', ' ').title()} must not exceed {maximum:g}.")
    return value


def survey_test_user_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not activate_dashboard_identity("customer"):
            return redirect(url_for("customer_login", next=request.path))
        if current_user_role() != "customer" or current_customer_device_id() != SURVEY_TEST_DEVICE_ID:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def survey_form_values():
    fields = ("name", "email", "contact_number", *SURVEY_QUESTION_LABELS.keys(), "comments")
    return {key: str(request.form.get(key, "")).strip() for key in fields}


def validate_survey_form(values):
    errors = {}
    required = {
        "name": "Please enter the customer name.", "contact_number": "Please enter the phone or WhatsApp number.",
        "installation_address": "Please enter the installation address.",
        "landmark_city_pin": "Please enter the city and PIN code.", "property_type": "Please select the property type.",
        "tank_access": "Please select the tank access method.", "water_source": "Please select the water source.",
        "pump_control": "Please select the pump-control method.", "tank1_capacity": "Please enter the primary tank capacity.",
        "customer_requirements": "Please select the required features.",
        "consent": "Consent is required to prepare a quotation.",
    }
    for field, message in required.items():
        if not values.get(field):
            errors[field] = message
    if len(values.get("name", "")) > 160:
        errors["name"] = "Name must be 160 characters or fewer."
    if len(values.get("email", "")) > 255:
        errors["email"] = "Email must be 255 characters or fewer."
    if len(values.get("comments", "")) > 5000:
        errors["comments"] = "Comments must be 5,000 characters or fewer."
    if values.get("email") and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
        errors["email"] = "Please enter a valid email address."
    if values.get("contact_number") and not re.fullmatch(r"[0-9+()\-\s]{7,40}", values["contact_number"]):
        errors["contact_number"] = "Please enter a valid contact number."
    return errors


@app.route("/survey", methods=["GET", "POST"])
@survey_test_user_required
@csrf_protect
def survey():
    values = {}
    errors = {}
    success = request.args.get("submitted") == "1"
    submission_token = str(request.form.get("submission_token") or secrets.token_hex(24))
    if request.method == "POST":
        values = survey_form_values()
        errors = validate_survey_form(values)
        if not re.fullmatch(r"[a-f0-9]{48}", submission_token):
            errors["form"] = "This survey session is invalid. Please reload and try again."
        if not errors:
            answers = {key: values[key] for key in SURVEY_QUESTION_LABELS}
            logged_in = is_logged_in()
            with get_db() as db:
                existing = db.execute(
                    "SELECT id FROM survey_responses WHERE submission_token = ? LIMIT 1", (submission_token,),
                ).fetchone()
                if not existing:
                    db.execute(
                        """
                        INSERT INTO survey_responses(
                            submission_token, name, email, contact_number, overall_experience,
                            primary_use, most_valuable_feature, reliability_rating, ease_of_use_rating,
                            would_recommend, answers_json, comments, submitted_by_role,
                            submitted_by_username, submitted_by_device_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            submission_token, values["name"], values["email"] or None,
                            values["contact_number"] or None, "Site assessment",
                            values["property_type"], values["customer_requirements"][:64],
                            0, 0, "Not applicable", json.dumps(answers, separators=(",", ":")),
                            values["comments"] or None, session.get("role") if logged_in else None,
                            session.get("username") if logged_in else None,
                            session.get("device_id") if logged_in else None,
                        ),
                    )
            return redirect(url_for("survey", submitted=1))
    return render_template("survey.html", values=values, errors=errors, success=success, submission_token=submission_token)


@app.route("/admin/surveys")
@admin_required
def admin_survey_responses():
    search_query = str(request.args.get("q", "")).strip()
    like_query = f"%{search_query}%"
    with get_db() as db:
        if search_query:
            rows = db.execute(
                """
                SELECT * FROM survey_responses
                WHERE name LIKE ? OR COALESCE(email, '') LIKE ? OR COALESCE(contact_number, '') LIKE ?
                   OR COALESCE(comments, '') LIKE ? OR COALESCE(submitted_by_username, '') LIKE ?
                ORDER BY created_at DESC, id DESC LIMIT 500
                """,
                (like_query, like_query, like_query, like_query, like_query),
            ).fetchall()
        else:
            rows = db.execute("SELECT * FROM survey_responses ORDER BY created_at DESC, id DESC LIMIT 500").fetchall()
    return render_template("admin_survey_responses.html", responses=[dict(row) for row in rows], search_query=search_query)


@app.route("/admin/surveys/<int:response_id>")
@admin_required
def admin_survey_response_detail(response_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM survey_responses WHERE id = ?", (response_id,)).fetchone()
    if not row:
        abort(404)
    response = dict(row)
    response["review_status"] = str(response.get("review_status") or "pending").strip().lower()
    answers = parse_survey_answers(response)
    registered_device_id = normalize_device_id(response.get("registered_device_id"))
    registered_device = None
    registered_config = None
    if registered_device_id:
        with get_db() as db:
            registered_row = db.execute(
                "SELECT device_id, registration_source, first_seen_at, last_seen_at, updated_at FROM registered_devices WHERE device_id = ? LIMIT 1",
                (registered_device_id,),
            ).fetchone()
        registered_device = dict(registered_row) if registered_row else {"device_id": registered_device_id}
        registered_config = fetch_device_service_config(registered_device_id)
    setup_defaults = survey_device_setup_defaults(response, answers)
    return render_template(
        "admin_survey_response_detail.html", response=response, answers=answers,
        question_labels=SURVEY_QUESTION_LABELS,
        setup_defaults=setup_defaults,
        registered_device=registered_device,
        registered_config=registered_config,
        registration_success=request.args.get("registered") == "1",
        deletion_success=request.args.get("deleted") == "1",
        registration_error=request.args.get("error", "", type=str),
        review_success=request.args.get("reviewed", "", type=str),
    )


@app.route("/admin/customers/<device_id>/artifact-intake", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_artifact_intake(device_id):
    """Accept firmware through JSON when a hosting WAF rejects multipart binaries."""
    normalized_device_id = normalize_device_id(device_id)
    if not normalized_device_id:
        return jsonify({"ok": False, "error": "Choose a valid device before uploading firmware."}), 400

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "Upload request must be valid JSON."}), 400

    encoded_payload = str(body.get("content_base64") or "")
    # Reject oversized encoded input before decoding it. Base64 is at most 4/3
    # of the binary size, plus a few bytes of padding.
    encoded_limit = ((FIRMWARE_ARTIFACT_MAX_BYTES + 2) // 3) * 4
    if not encoded_payload or len(encoded_payload) > encoded_limit:
        return jsonify({"ok": False, "error": "Firmware upload is empty or too large."}), 400
    try:
        binary_payload = base64.b64decode(encoded_payload, validate=True)
    except (ValueError, binascii.Error):
        return jsonify({"ok": False, "error": "Firmware upload encoding is invalid."}), 400

    filename = Path(str(body.get("filename") or "firmware.bin")).name
    role = body.get("firmware_role", "master")
    notes = body.get("notes", "")
    uploaded_file = type(
        "JsonFirmwareUpload",
        (),
        {
            "filename": filename,
            "stream": io.BytesIO(binary_payload),
            "mimetype": "application/octet-stream",
        },
    )()
    try:
        normalized_role = normalize_firmware_artifact_role(role)
        artifact = create_firmware_artifact(
            normalized_device_id,
            uploaded_file,
            notes=notes,
            uploaded_by=current_actor_username(),
            role=normalized_role,
            expected_build_flags=None,
        )
        firmware_role = normalize_firmware_artifact_role(artifact.get("target_role") or normalized_role)
        log_audit_event(
            actor=current_actor_username(),
            action="upload_device_firmware_artifact",
            target_type="device",
            target_id=normalized_device_id,
            device_id=normalized_device_id,
            details={
                "artifact_id": artifact["id"],
                "role": firmware_role,
                "version_label": artifact.get("version_label"),
                "original_filename": artifact.get("original_filename"),
                "md5": artifact.get("md5"),
                "size_bytes": artifact.get("size_bytes"),
                "notes": artifact.get("notes"),
                "delivery": "android_local_wifi",
                "transport": "json_base64_waf_fallback",
            },
        )
        version_suffix = f" ({artifact['version_label']})" if artifact.get("version_label") else ""
        message = (
            f"{firmware_role.title()} firmware uploaded for {normalized_device_id}. "
            f"{artifact['original_filename']}{version_suffix} is now available to the Android app for local Wi-Fi upgrades."
        )
        return jsonify({"ok": True, "message": message}), 200
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/admin/surveys/<int:response_id>/review", methods=["POST"])
@admin_required
@csrf_protect
def admin_survey_review(response_id):
    decision = str(request.form.get("decision") or "").strip().lower()
    if decision not in {"accepted", "rejected"}:
        abort(400)
    with get_db() as db:
        row = db.execute("SELECT id, registered_device_id FROM survey_responses WHERE id = ?", (response_id,)).fetchone()
        if not row:
            abort(404)
        if row["registered_device_id"] and decision == "rejected":
            return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="Delete the registered device before rejecting this survey."))
        db.execute(
            "UPDATE survey_responses SET review_status = ?, reviewed_at = CURRENT_TIMESTAMP, reviewed_by = ? WHERE id = ?",
            (decision, current_actor_username(), response_id),
        )
    log_audit_event(
        actor=current_actor_username(), action=f"{decision[:-2]}_survey_response",
        target_type="survey_response", target_id=str(response_id),
        details={"review_status": decision},
    )
    return redirect(url_for("admin_survey_response_detail", response_id=response_id, reviewed=decision))


@app.route("/admin/surveys/<int:response_id>/delete", methods=["POST"])
@admin_required
@csrf_protect
def admin_survey_delete(response_id):
    with get_db() as db:
        row = db.execute("SELECT id, registered_device_id FROM survey_responses WHERE id = ?", (response_id,)).fetchone()
        if not row:
            abort(404)
        if normalize_device_id(row["registered_device_id"]):
            return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="Delete the registered device before deleting this survey."))
        db.execute("DELETE FROM survey_responses WHERE id = ?", (response_id,))
    log_audit_event(
        actor=current_actor_username(), action="delete_survey_response",
        target_type="survey_response", target_id=str(response_id), details={},
    )
    return redirect(url_for("admin_survey_responses", deleted=1))


@app.route("/admin/surveys/<int:response_id>/register-device", methods=["POST"])
@admin_required
@csrf_protect
def admin_survey_register_device(response_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM survey_responses WHERE id = ?", (response_id,)).fetchone()
    if not row:
        abort(404)
    response = dict(row)
    if str(response.get("review_status") or "pending").strip().lower() != "accepted":
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="Accept this survey before registering a device."))
    if normalize_device_id(response.get("registered_device_id")):
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="This survey already has a registered device."))

    device_id = request.form.get("device_id", "")
    device_key = request.form.get("device_key", "")
    password = request.form.get("password", "")
    try:
        normalized_device_id = normalize_device_id(device_id)
        if not normalized_device_id:
            raise ValueError("Device ID is required.")
        if not str(device_key or "").strip():
            raise ValueError("Device key is required.")
        if not str(password or "").strip():
            raise ValueError("Customer password is required.")
        with get_db() as db:
            linked = db.execute(
                "SELECT id FROM survey_responses WHERE registered_device_id = ? AND id <> ? LIMIT 1",
                (normalized_device_id, response_id),
            ).fetchone()
        if linked:
            raise ValueError("That device is already linked to another survey response.")

        upper_height = survey_registration_float(request.form, "upper_tank_height_cm", 30, 500)
        upper_capacity = survey_registration_float(request.form, "upper_tank_capacity_liters", 50, 50000)
        lower_height = survey_registration_float(request.form, "lower_tank_height_cm", 30, 500)
        lower_capacity = survey_registration_float(request.form, "lower_tank_capacity_liters", 50, 50000)
        slave_enabled = form_flag("slave_device_enabled", default=False)
        source_enabled = form_flag("source_tank_monitoring_enabled", default=False)
        municipal_enabled = form_flag("municipal_sensor_enabled", default=False)
        setup_payload = {
            "device_setup_type": str(request.form.get("device_setup_type") or "custom").strip().lower(),
            "main_sensor_enabled": True,
            "master_upper_sensor_enabled": not slave_enabled,
            "slave_device_enabled": slave_enabled,
            "slave_upper_sensor_enabled": slave_enabled,
            "source_tank_monitoring_enabled": source_enabled,
            "municipal_sensor_enabled": municipal_enabled,
            "municipal_valve_enabled": municipal_enabled and source_enabled and form_flag("municipal_valve_enabled", default=False),
            "source_outlet_valve_enabled": source_enabled and form_flag("source_outlet_valve_enabled", default=False),
            "relay_enabled": form_flag("relay_enabled", default=False),
            "ai_analysis_enabled": form_flag("ai_analysis_enabled", default=False),
            "cloud_feed_mode": normalize_device_service_cloud_mode(request.form.get("cloud_feed_mode")),
            "local_firmware_upload_enabled": form_flag("local_firmware_upload_enabled", default=False),
            "buzzer_enabled": form_flag("buzzer_enabled", default=False),
            "led_display_enabled": form_flag("led_display_enabled", default=False),
            "auto_mode_enabled": form_flag("auto_mode_enabled", default=False),
            "tank_height_cm": upper_height,
            "tank_capacity_liters": upper_capacity,
            "upper_tank_height_cm": upper_height,
            "upper_tank_capacity_liters": upper_capacity,
            "lower_tank_height_cm": lower_height,
            "lower_tank_capacity_liters": lower_capacity,
        }

        register_device_credentials(normalized_device_id, device_key, registration_source="admin_survey", remote_addr=request.remote_addr)
        account = upsert_customer_account(
            normalized_device_id,
            password,
            display_name=request.form.get("display_name") or response.get("name"),
            email=request.form.get("email") or response.get("email"),
            service_updates_enabled=form_flag("service_updates_enabled", default=True),
            marketing_emails_enabled=form_flag("marketing_emails_enabled", default=False),
        )
        saved_config = upsert_device_service_config(normalized_device_id, **setup_payload)
        with get_db() as db:
            db.execute(
                """
                UPDATE survey_responses
                SET registered_device_id = ?, registration_config_json = ?, registered_at = CURRENT_TIMESTAMP
                WHERE id = ? AND registered_device_id IS NULL
                """,
                (normalized_device_id, json.dumps(saved_config, separators=(",", ":")), response_id),
            )
        log_audit_event(
            actor=current_actor_username(), action="register_device_from_survey",
            target_type="survey_response", target_id=str(response_id), device_id=normalized_device_id,
            details={"customer_email": account.get("email"), "service_config": saved_config},
        )
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, registered=1))
    except ValueError as exc:
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, error=str(exc)))


@app.route("/admin/surveys/<int:response_id>/delete-device", methods=["POST"])
@admin_required
@csrf_protect
def admin_survey_delete_device(response_id):
    with get_db() as db:
        row = db.execute("SELECT registered_device_id FROM survey_responses WHERE id = ?", (response_id,)).fetchone()
    if not row:
        abort(404)
    normalized_device_id = normalize_device_id(row["registered_device_id"])
    if not normalized_device_id:
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="This survey has no registered device."))
    try:
        delete_known_device(normalized_device_id)
        with get_db() as db:
            db.execute(
                "UPDATE survey_responses SET registered_device_id = NULL, registration_config_json = NULL, registered_at = NULL WHERE id = ?",
                (response_id,),
            )
        log_audit_event(
            actor=current_actor_username(), action="delete_survey_registered_device",
            target_type="survey_response", target_id=str(response_id), device_id=normalized_device_id,
            details={"deleted_device_id": normalized_device_id},
        )
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, deleted=1))
    except Exception:
        logger.exception("Survey device delete failed for %s", normalized_device_id)
        return redirect(url_for("admin_survey_response_detail", response_id=response_id, error="Device deletion failed. Review the server log and try again."))


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


@app.route("/admin/db-cleanup", methods=["POST"])
@admin_required
@csrf_protect
def admin_db_cleanup():
    pruned = maybe_prune_retained_rows(force=True)
    pruned_rows = sum(int(value or 0) for value in pruned.values())
    payload = build_db_summary_payload()
    payload["cleanup"] = {
        "pruned": pruned,
        "pruned_rows": pruned_rows,
        "maintenance": {
            "last_action": db_maintenance_state.get("last_action"),
            "last_error": db_maintenance_state.get("last_error"),
            "last_reason": db_maintenance_state.get("last_reason"),
            "last_tables": db_maintenance_state.get("last_tables"),
        },
    }
    return jsonify(payload)


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


def device_detail_card_display(value, fallback="Not reported"):
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text and text != "--" else fallback


def device_detail_card_title(value, fallback="Not reported"):
    text = device_detail_card_display(value, fallback="")
    return text.replace("_", " ").replace("-", " ").title() if text else fallback


def device_detail_card_bool(value, default=False):
    return "Enabled" if boolish_enabled(value, default=default) else "Disabled"


def device_detail_card_liters(value, fallback="Not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return f"{number:.1f} L"


def device_detail_card_cm(value, fallback="Not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return f"{number:.1f} cm"


def device_detail_card_percent(value, fallback="Not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return f"{number:g}%"


def device_detail_card_duration_ms(value, fallback="Not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or number < 0:
        return fallback
    if number >= 1000:
        return format_compact_uptime(number / 1000.0)
    return f"{number:g} ms"


def device_detail_card_duration_seconds(value, fallback="Not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number) or number < 0:
        return fallback
    return format_compact_uptime(number)


def device_detail_card_heap(value, fallback="Heap not reported"):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    if abs(number) >= 1024 * 1024:
        return f"{number / (1024 * 1024):.1f} MB free"
    if abs(number) >= 1024:
        return f"{number / 1024:.1f} KB free"
    return f"{int(number)} B free"


def build_device_detail_info_cards(snapshot, system_status, service_config, automation_settings, current_saved_config):
    snapshot = snapshot or {}
    system_status = system_status or {}
    service_config = service_config or {}
    current_saved_config = current_saved_config or {}
    saved_service_config = current_saved_config.get("service_config") or service_config
    saved_automation_settings = current_saved_config.get("automation_settings") or automation_settings or {}
    uses_slave = boolish_enabled(saved_service_config.get("slave_device_enabled"), default=True)
    slave_upper = uses_slave and boolish_enabled(
        saved_service_config.get("slave_upper_sensor_enabled"),
        default=uses_slave,
    )
    source_monitoring = boolish_enabled(
        saved_service_config.get("source_tank_monitoring_enabled"),
        default=True,
    )
    telemetry_online = str(snapshot.get("telemetry_status") or system_status.get("telemetry_status") or "").strip().lower() in {
        "live", "recent", "fresh", "online"
    }

    def municipal_detection_status(enabled, detected, detected_label):
        if not boolish_enabled(enabled, default=False):
            return "Disabled"
        if not telemetry_online:
            return "Offline"
        if boolish_enabled(snapshot.get("municipal_sensor_simulated"), default=False):
            return "Online · Simulated"
        return f"Online · {detected_label}" if boolish_enabled(detected, default=False) else "Offline"

    def sensor_signal_status(enabled, valid_signal, simulated=False, detail="Signal detected"):
        if not boolish_enabled(enabled, default=False):
            return "Disabled"
        if not telemetry_online or not (
            boolish_enabled(simulated, default=False) or boolish_enabled(valid_signal, default=False)
        ):
            return "Offline"
        return "Online · Simulated" if boolish_enabled(simulated, default=False) else f"Online · {detail}"
    upper_source = snapshot.get("upper_sensor_source") or ("slave" if slave_upper else "master")
    auto_start = (
        current_saved_config.get("auto_start_pct")
        or saved_automation_settings.get("auto_start_pct")
        or saved_service_config.get("auto_start_pct")
        or snapshot.get("auto_start_pct")
        or snapshot.get("auto_start_level_pct")
        or snapshot.get("lower_threshold_pct")
    )
    auto_stop = (
        current_saved_config.get("auto_stop_pct")
        or saved_automation_settings.get("auto_stop_pct")
        or saved_service_config.get("auto_stop_pct")
        or snapshot.get("auto_stop_pct")
        or snapshot.get("auto_stop_level_pct")
        or snapshot.get("upper_threshold_pct")
    )
    tank_capacity = (
        current_saved_config.get("tank_capacity_liters")
        or saved_service_config.get("tank_capacity_liters")
        or snapshot.get("capacity_liters")
        or snapshot.get("tank_capacity_liters")
        or system_status.get("capacity_liters")
    )
    tank_height = (
        current_saved_config.get("tank_height_cm")
        or saved_service_config.get("tank_height_cm")
        or snapshot.get("tank_height_cm")
    )
    source_capacity = (
        current_saved_config.get("lower_tank_capacity_liters")
        or saved_service_config.get("lower_tank_capacity_liters")
        or snapshot.get("lower_tank_capacity_liters")
        or snapshot.get("source_tank_capacity_liters")
    )
    source_height = (
        current_saved_config.get("lower_tank_height_cm")
        or saved_service_config.get("lower_tank_height_cm")
        or snapshot.get("lower_tank_height_cm")
    )
    motorized_valve_enabled = boolish_enabled(
        snapshot.get("municipal_valve_enabled"),
        default=boolish_enabled(saved_service_config.get("municipal_valve_enabled"), default=False),
    )
    motorized_valve_route = str(
        snapshot.get("inlet_valve_route") or snapshot.get("municipal_valve_route") or ""
    ).strip().lower()
    motorized_valve_path = {
        "municipal": "Municipal Water",
        "source": "Source Tank",
    }.get(motorized_valve_route, "Not reported")
    motorized_valve_state = device_detail_card_title(
        snapshot.get("inlet_valve_state") or snapshot.get("municipal_valve_state"),
        "Not reported",
    )
    outlet_valve_enabled = boolish_enabled(
        snapshot.get("source_pump_fill_feature_enabled"),
        default=boolish_enabled(saved_service_config.get("source_outlet_valve_enabled"), default=False),
    )
    outlet_valve_route = device_detail_card_title(snapshot.get("source_outlet_route"), "Not reported")
    outlet_valve_state = device_detail_card_title(snapshot.get("source_outlet_valve_state"), "Not reported")
    municipal_direct_upper = boolish_enabled(
        saved_service_config.get("municipal_sensor_enabled"), default=False
    ) and not source_monitoring
    water_routing_mode = (
        "Municipal Water -> Pump -> Upper Tank (direct; no valves)"
        if municipal_direct_upper
        else (
            "Source/municipal multi-tank routing"
            if source_monitoring
            else "Pump -> Upper Tank"
        )
    )
    card_values = [
        ("Firmware", device_detail_card_display(snapshot.get("firmware_version"))),
        ("Configuration", "Master + Slave" if uses_slave else "Master Only"),
        ("Water Routing", water_routing_mode),
        ("Master Reachability", device_detail_card_display(system_status.get("master_status_label"), "Unreachable")),
        ("Slave Reachability", device_detail_card_display(system_status.get("slave_status_label"), "Reachable" if uses_slave else "Disabled")),
        ("Device Role", device_detail_card_title(snapshot.get("node_role"), "Master Control" if uses_slave else "Master")),
        ("Architecture", f"Arch {snapshot.get('arch_id')}" if snapshot.get("arch_id") not in (None, "") else device_detail_card_display(snapshot.get("architecture_mode"))),
        ("Source Mode", device_detail_card_title(snapshot.get("device_source") or system_status.get("device_source_mode"), "Real")),
        ("Upper Sensor", sensor_signal_status(True, admin_sensor_reachable(snapshot.get("sensor") or snapshot.get("upper_sensor")), snapshot.get("upper_tank_simulator") or snapshot.get("simulator"), device_detail_card_title(snapshot.get("sensor") or snapshot.get("upper_sensor"), "OK"))),
        ("Upper Sensor Source", device_detail_card_title(upper_source, "Slave" if uses_slave else "Master")),
        ("Source Tank Sensor", sensor_signal_status(source_monitoring, admin_sensor_reachable(snapshot.get("lower_sensor")), snapshot.get("lower_tank_simulator"), device_detail_card_title(snapshot.get("lower_sensor"), "OK"))),
        ("Lower Turbidity Sensor", sensor_signal_status(saved_service_config.get("master_turbidity_enabled"), str(snapshot.get("lower_turbidity_sensor") or "").upper() == "OK", snapshot.get("lower_turbidity_simulated"), "OK")),
        ("Upper Turbidity Sensor", sensor_signal_status(saved_service_config.get("slave_turbidity_enabled"), str(snapshot.get("upper_turbidity_sensor") or "").upper() == "OK", snapshot.get("upper_turbidity_simulated"), "OK")),
        ("Auto Start/Stop", device_detail_card_bool(current_saved_config.get("auto_mode_enabled", saved_service_config.get("auto_mode_enabled")), default=False)),
        ("Inlet Motorized Valve", "ON" if motorized_valve_enabled else "OFF"),
        ("Inlet Selected Path", motorized_valve_path if motorized_valve_enabled else "Disabled"),
        ("Inlet Valve Feedback", motorized_valve_state if motorized_valve_enabled else "Disabled"),
        ("Outlet Motorized Valve", "ON" if outlet_valve_enabled else "OFF"),
        ("Outlet Selected Path", outlet_valve_route if outlet_valve_enabled else "Disabled"),
        ("Outlet Valve Feedback", outlet_valve_state if outlet_valve_enabled else "Disabled"),
        ("Tank Capacity", device_detail_card_liters(tank_capacity)),
        ("Tank Height", device_detail_card_cm(tank_height)),
        ("Auto Start Threshold", device_detail_card_percent(auto_start)),
        ("Auto Stop Threshold", device_detail_card_percent(auto_stop)),
        ("Auto Start Delay", device_detail_card_duration_ms(snapshot.get("auto_start_stable_ms"), "5s")),
        ("Level Average Samples", device_detail_card_display(snapshot.get("auto_level_average_samples"), "5")),
        ("Water Depth", device_detail_card_cm(snapshot.get("water_depth_cm"))),
        ("Upper Echo Distance", device_detail_card_cm(snapshot.get("sensor_distance_cm"))),
        ("Command Service", device_detail_card_title(snapshot.get("command_service"), "ON")),
        ("Telemetry Service", device_detail_card_title(snapshot.get("telemetry_service"), "ON")),
        ("Simulator", device_detail_card_title(snapshot.get("simulator") or snapshot.get("simulator_status"), "OFF")),
        ("Direct Peer", device_detail_card_display(snapshot.get("direct_peer"), "enabled" if uses_slave else "disabled")),
        ("Peer Config Channel", device_detail_card_display(snapshot.get("direct_peer_config_channel") or saved_service_config.get("direct_peer_wifi_channel"), "1")),
        ("Peer Active Channel", device_detail_card_display(snapshot.get("direct_peer_wifi_channel"), "1")),
        ("Peer Sync", "Sync OK" if not boolish_enabled(snapshot.get("direct_peer_sync_pending"), default=False) else "Pending"),
        ("Peer Remote IP", device_detail_card_display(snapshot.get("direct_peer_remote_ip"), "Waiting for peer")),
        ("Peer Remote MAC", device_detail_card_display(snapshot.get("direct_peer_remote_mac"), "Waiting for peer")),
        ("Peer Packet Age", device_detail_card_duration_seconds(snapshot.get("direct_peer_last_packet_age_s"), "0s")),
        ("Cloud Feed Mode", device_detail_card_title(saved_service_config.get("cloud_feed_mode"), "Full")),
        ("AI Analysis", device_detail_card_bool(saved_service_config.get("effective_ai_analysis_enabled", saved_service_config.get("ai_analysis_enabled")), default=True)),
        ("Relay Control", device_detail_card_bool(saved_service_config.get("relay_enabled"), default=True)),
        ("Physical Pump State", "Running" if boolish_enabled(snapshot.get("physical_pump_running"), default=False) else "Stopped"),
        ("Pump Confirmation", device_detail_card_title(snapshot.get("pump_confirmation_source"), "Relay command fallback")),
        ("Starter Contactor Sensor", sensor_signal_status(snapshot.get("starter_contactor_sensor_enabled", saved_service_config.get("starter_contactor_sensor_enabled")), snapshot.get("starter_contactor_active"), snapshot.get("pump_feedback_simulated"), "Active")),
        ("Motor Current Sensor", sensor_signal_status(snapshot.get("motor_current_sensor_enabled", saved_service_config.get("motor_current_sensor_enabled")), snapshot.get("motor_current_detected"), snapshot.get("pump_feedback_simulated"), "Current detected")),
        ("Water Flow Sensor", municipal_detection_status(snapshot.get("water_flow_sensor_enabled", saved_service_config.get("water_flow_sensor_enabled")), snapshot.get("water_flow_detected"), "Flow detected")),
        ("Water Pressure Sensor", municipal_detection_status(snapshot.get("water_pressure_sensor_enabled", saved_service_config.get("water_pressure_sensor_enabled")), snapshot.get("water_pressure_detected"), "Pressure detected")),
        ("Authoritative Pump Runtime", device_detail_card_duration_seconds(snapshot.get("pump_total_runtime_s"), "Not reported")),
        ("Last Pump Run", device_detail_card_duration_seconds(snapshot.get("pump_last_run_runtime_s"), "Not reported")),
        ("Pump Cycle Counter", device_detail_card_display(snapshot.get("pump_cycle_count"), "Not reported")),
        ("Source Tank Monitoring", device_detail_card_bool(saved_service_config.get("source_tank_monitoring_enabled"), default=True)),
        ("Buzzer Service", device_detail_card_bool(saved_service_config.get("buzzer_enabled"), default=True)),
        ("LED Display Service", device_detail_card_bool(saved_service_config.get("led_display_enabled"), default=True)),
        ("Local Firmware Upload", device_detail_card_bool(saved_service_config.get("local_firmware_upload_enabled"), default=True)),
        ("Android SSO Limit", device_detail_card_display(saved_service_config.get("android_sso_session_limit"), DEFAULT_ANDROID_SSO_SESSION_LIMIT)),
        ("Network Channel", device_detail_card_title(snapshot.get("channel_mode"), "Cloud")),
        ("Local Device IP", device_detail_card_display(snapshot.get("device_local_url"), "Local IP not reported")),
        ("Master Memory", device_detail_card_heap(snapshot.get("free_heap"))),
    ]
    if uses_slave:
        card_values.extend(
            [
                ("Slave Memory", device_detail_card_heap(snapshot.get("slave_free_heap"), "Slave heap not reported")),
                ("Uptime", device_detail_card_display(snapshot.get("uptime_label"), "Uptime not reported")),
                ("Slave Uptime", device_detail_card_display(snapshot.get("slave_uptime_label"), "Slave uptime not reported")),
            ]
        )
    if source_monitoring:
        card_values.extend(
            [
                ("Source Capacity", device_detail_card_liters(source_capacity)),
                ("Source Height", device_detail_card_cm(source_height)),
                ("Source Water Depth", device_detail_card_cm(snapshot.get("lower_water_depth_cm"))),
                ("Lower Sensor Distance", device_detail_card_cm(snapshot.get("lower_sensor_distance_cm"))),
            ]
        )
    return [{"label": label, "value": value} for label, value in card_values]


@app.route("/devices/<device_id>")
@admin_required
def device_detail_page(device_id):
    # Release guard: keep public/static marketing routes outside this handler.
    # Ending this function early makes Flask return a 500 for every device page.
    scoped_device_id = current_scope_device_id(device_id)
    account = fetch_customer_account(scoped_device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    current_saved_config = build_current_saved_config(scoped_device_id, account=account)
    peer_channel_input_value = (
        current_saved_config.get("direct_peer_wifi_channel")
        or (snapshot or {}).get("direct_peer_config_channel")
        or (snapshot or {}).get("direct_peer_wifi_channel")
        or 6
    )
    service_config = current_saved_config.get("service_config") or default_device_service_config(scoped_device_id, account=account)
    firmware_install_profile = build_device_firmware_install_profile(service_config)
    automation_settings = current_saved_config.get("automation_settings") or default_device_automation_settings(scoped_device_id)
    simulator_state = str(request.args.get("simulator_state", "", type=str) or "").strip().lower()
    simulator_enabled = device_simulator_enabled(scoped_device_id, snapshot=snapshot)
    municipal_simulator_state = str(
        request.args.get("municipal_simulator_state", "", type=str) or ""
    ).strip().lower()
    municipal_simulator_enabled = simulator_enabled_for_feature(
        snapshot,
        service_config,
        "municipal_sensor_simulated",
        "municipal_sensor_enabled",
    )
    if service_config.get("municipal_sensor_enabled") and municipal_simulator_state in {"on", "off"}:
        municipal_simulator_enabled = municipal_simulator_state == "on"
    valve_simulator_state = str(
        request.args.get("valve_simulator_state", "", type=str) or ""
    ).strip().lower()
    valve_simulator_enabled = simulator_enabled_for_feature(
        snapshot,
        service_config,
        "municipal_valve_simulated",
        "municipal_valve_enabled",
    )
    if service_config.get("municipal_valve_enabled") and valve_simulator_state in {"on", "off"}:
        valve_simulator_enabled = valve_simulator_state == "on"
    outlet_valve_simulator_state = str(
        request.args.get("outlet_valve_simulator_state", "", type=str) or ""
    ).strip().lower()
    outlet_valve_simulator_enabled = simulator_enabled_for_feature(
        snapshot,
        service_config,
        "source_outlet_valve_simulated",
        "source_outlet_valve_enabled",
    )
    if service_config.get("source_outlet_valve_enabled") and outlet_valve_simulator_state in {"on", "off"}:
        outlet_valve_simulator_enabled = outlet_valve_simulator_state == "on"
    lower_turbidity_simulator_enabled = simulator_enabled_for_feature(
        snapshot,
        service_config,
        "lower_turbidity_simulated",
        "master_turbidity_enabled",
    )
    upper_turbidity_simulator_enabled = simulator_enabled_for_feature(
        snapshot,
        service_config,
        "upper_turbidity_simulated",
        "slave_turbidity_enabled",
    )
    turbidity_simulator_role = str(
        request.args.get("turbidity_simulator_role", "", type=str) or ""
    ).strip().lower()
    turbidity_simulator_state = str(
        request.args.get("turbidity_simulator_state", "", type=str) or ""
    ).strip().lower()
    if turbidity_simulator_role in {"lower", "upper"} and turbidity_simulator_state in {"on", "off"}:
        if turbidity_simulator_role == "lower":
            if service_config.get("master_turbidity_enabled"):
                lower_turbidity_simulator_enabled = turbidity_simulator_state == "on"
        elif service_config.get("slave_turbidity_enabled"):
            upper_turbidity_simulator_enabled = turbidity_simulator_state == "on"
    live_simulator_status = simulator_payload_status(snapshot)
    if (
        simulator_state in {"on", "off"}
        and (
            live_simulator_status is None
            or str(snapshot.get("telemetry_status") or "").strip().lower() == "no-data"
        )
    ):
        simulator_enabled = simulator_state == "on"
    system_status = build_system_status_payload(snapshot, device_id=scoped_device_id, service_config=service_config)
    # Keep the HTML render path cheap and safe. The browser can synthesize
    # current activity rows from the snapshot below, then hydrate from /events.
    initial_events = []
    initial_info_cards = build_device_detail_info_cards(
        snapshot,
        system_status,
        service_config,
        automation_settings,
        current_saved_config,
    )
    return render_template(
        "device_detail.html",
        device_id=scoped_device_id,
        is_admin=True,
        snapshot=snapshot or {},
        customer_account=account,
        service_config=service_config,
        automation_settings=automation_settings,
        current_saved_config=current_saved_config,
        system_status=system_status,
        peer_channel_input_value=peer_channel_input_value,
        android_sso_active_session_count=active_platform_session_count(
            SESSION_PLATFORM_ANDROID,
            "customer",
            username=scoped_device_id,
            device_id=scoped_device_id,
        ),
        firmware_install_profile=firmware_install_profile,
        simulator_enabled=simulator_enabled,
        simulator_state=simulator_state if simulator_state in {"on", "off"} else "",
        municipal_simulator_enabled=municipal_simulator_enabled,
        valve_simulator_enabled=valve_simulator_enabled,
        outlet_valve_simulator_enabled=outlet_valve_simulator_enabled,
        lower_turbidity_simulator_enabled=lower_turbidity_simulator_enabled,
        upper_turbidity_simulator_enabled=upper_turbidity_simulator_enabled,
        initial_events=initial_events,
        initial_info_cards=initial_info_cards,
        latest_firmware_artifacts=fetch_latest_firmware_artifacts_by_role(scoped_device_id),
        config_message=request.args.get("config_message", "", type=str) or "",
        config_error=request.args.get("config_error", "", type=str) or "",
    )


def device_simulator_state_key(device_id):
    normalized_device_id = normalize_device_id(device_id)
    return f"{DEVICE_SIMULATOR_STATE_PREFIX}{normalized_device_id}" if normalized_device_id else None


SIMULATOR_STATUS_KEYS = (
    "simulator",
    "upper_tank_simulator",
    "main_tank_simulator",
    "source_tank_simulator",
    "lower_tank_simulator",
)


def simulator_payload_status(payload):
    if not payload:
        return None
    saw_disabled = False
    for key in SIMULATOR_STATUS_KEYS:
        value = str(payload.get(key) or "").strip().upper()
        if value in {"ON", "TRUE", "YES", "1"}:
            return True
        if value in {"OFF", "FALSE", "NO", "0"}:
            saw_disabled = True
    return False if saw_disabled else None


def simulator_payload_enabled(payload):
    status = simulator_payload_status(payload)
    if status is not None:
        return status
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


def load_device_simulator_state(device_id):
    state_key = device_simulator_state_key(device_id)
    if not state_key:
        return None
    try:
        raw_state = get_app_setting(state_key)
    except Exception as exc:
        logger.warning("Could not load simulator state for %s: %s", normalize_device_id(device_id), exc)
        return None
    if not raw_state:
        return None
    try:
        state = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def device_simulator_enabled(device_id, snapshot=None):
    live_status = simulator_payload_status(snapshot)
    if live_status is not None and str((snapshot or {}).get("telemetry_status") or "").strip().lower() != "no-data":
        return live_status
    return False


def device_detail_ajax_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def device_detail_action_response(
    device_id,
    message="",
    *,
    error="",
    title="",
    status_code=200,
    detail_lines=None,
    **extra,
):
    normalized_device_id = normalize_device_id(device_id)
    resolved_message = str(message or "").strip()
    resolved_error = str(error or "").strip()
    if device_detail_ajax_request():
        payload = {
            "ok": not bool(resolved_error),
            "title": title or ("Action failed" if resolved_error else "Action saved"),
            "message": resolved_message or ("Request completed." if not resolved_error else ""),
            "error": resolved_error,
            "detail_lines": [line for line in (detail_lines or []) if line],
        }
        payload.update(extra)
        return jsonify(payload), status_code

    query_key = "config_error" if resolved_error else "config_message"
    query_value = resolved_error or resolved_message or "Request completed."
    return redirect(url_for("device_detail_page", device_id=normalized_device_id, **{query_key: query_value}))


def safe_queue_device_detail_command(command, device_id, failure_message):
    try:
        queue_result = queue_command(command, target_device=device_id)
    except Exception as exc:
        logger.exception("Unable to queue device detail command for %s: %s", normalize_device_id(device_id), exc)
        return None, f"{failure_message}: {exc}"
    if isinstance(queue_result, tuple):
        error_payload, _status_code = queue_result
        error_text = (error_payload or {}).get("error") if isinstance(error_payload, dict) else ""
        return None, error_text or failure_message
    return queue_result, ""


SIMULATOR_FEATURE_DEPENDENCIES = (
    ("main_sensor_enabled", "simulator", "SIMULATOR_OFF", "Tank level"),
    ("municipal_sensor_enabled", "municipal_sensor_simulated", "MUNICIPAL_SIMULATOR_OFF", "Municipal water"),
    ("municipal_valve_enabled", "municipal_valve_simulated", "MUNICIPAL_VALVE_SIMULATOR_OFF", "Inlet motorized valve"),
    ("source_outlet_valve_enabled", "source_outlet_valve_simulated", "SOURCE_OUTLET_VALVE_SIMULATOR_OFF", "Outlet motorized valve"),
    ("master_turbidity_enabled", "lower_turbidity_simulated", "LOWER_TURBIDITY_SIMULATOR_OFF", "Lower turbidity"),
    ("slave_turbidity_enabled", "upper_turbidity_simulated", "UPPER_TURBIDITY_SIMULATOR_OFF", "Upper turbidity"),
)

DEVICE_SETUP_TYPE_FEATURES = {
    "source_only": {
        "source_tank_monitoring_enabled": True,
        "municipal_sensor_enabled": False,
        "municipal_valve_enabled": False,
        "source_outlet_valve_enabled": False,
        "simulator_route": "source_only",
        "simulator_upper_level": 20,
        "simulator_source_level": 80,
        "auto_mode_enabled": True,
    },
    "borewell_upper": {
        # A borewell/submersible pump feeds the upper tank directly. It has no
        # separate lower/source tank to monitor.
        "source_tank_monitoring_enabled": False,
        "municipal_sensor_enabled": False,
        "municipal_valve_enabled": False,
        "source_outlet_valve_enabled": False,
        "simulator_route": "borewell",
        "simulator_upper_level": 20,
        "auto_mode_enabled": True,
    },
    "municipal_direct": {
        "source_tank_monitoring_enabled": False,
        "municipal_sensor_enabled": True,
        "municipal_valve_enabled": False,
        "source_outlet_valve_enabled": False,
        "simulator_route": "municipal_direct",
        "simulator_upper_level": 20,
        "auto_mode_enabled": True,
    },
    "dual_source_pumped": {
        "source_tank_monitoring_enabled": True,
        "municipal_sensor_enabled": True,
        "municipal_valve_enabled": True,
        "source_outlet_valve_enabled": True,
        "simulator_route": "dual_source_pumped",
        "simulator_upper_level": 70,
        "simulator_source_level": 20,
        "auto_mode_enabled": True,
    },
}


def automatic_simulator_commands_for_setup(setup_type, service_config):
    preset = DEVICE_SETUP_TYPE_FEATURES.get(setup_type)
    if preset is None:
        return []
    config = service_config or {}
    commands = [
        "SIMULATOR_ON",
        "set:upper_sensor_fault:none",
        "set:source_sensor_fault:none",
        "set:pump_feedback_override:off",
        "set:upper_high_float:off",
        "set:source_low_float:off",
        "set:peer_drop_every:0",
        "set:peer_duplicate_every:0",
        "set:peer_delay_ms:0",
        f"set:simulator_level:{preset['simulator_upper_level']}",
    ]
    if config.get("source_tank_monitoring_enabled"):
        commands.append(f"set:simulator_lower_level:{preset['simulator_source_level']}")
    if config.get("municipal_sensor_enabled"):
        commands.append("MUNICIPAL_SIMULATOR_ON")
    if config.get("municipal_valve_enabled"):
        commands.append("MUNICIPAL_VALVE_SIMULATOR_ON")
    if config.get("source_outlet_valve_enabled"):
        commands.append("SOURCE_OUTLET_VALVE_SIMULATOR_ON")
    if config.get("master_turbidity_enabled"):
        commands.append("LOWER_TURBIDITY_SIMULATOR_ON")
    if config.get("slave_turbidity_enabled"):
        commands.append("UPPER_TURBIDITY_SIMULATOR_ON")
    return commands


def simulator_enabled_for_feature(snapshot, service_config, simulator_key, feature_key):
    if not bool((service_config or {}).get(feature_key)):
        return False
    if simulator_key == "simulator":
        return device_simulator_enabled((snapshot or {}).get("device_id"), snapshot=snapshot)
    return boolish_enabled((snapshot or {}).get(simulator_key), default=False)


def disable_orphaned_device_simulators(device_id, snapshot, service_config):
    queued_commands = []
    queue_errors = []
    for feature_key, simulator_key, command, label in SIMULATOR_FEATURE_DEPENDENCIES:
        feature_enabled = bool((service_config or {}).get(feature_key))
        if simulator_key == "simulator":
            simulator_active = device_simulator_enabled(device_id, snapshot=snapshot)
        else:
            simulator_active = boolish_enabled((snapshot or {}).get(simulator_key), default=False)
        if feature_enabled or not simulator_active:
            continue
        result, error = safe_queue_device_detail_command(
            command,
            device_id,
            f"Unable to disable the {label} simulator",
        )
        if error:
            queue_errors.append(error)
        elif result:
            queued_commands.append(command)
    return queued_commands, queue_errors


@app.route("/devices/<device_id>/configuration", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_configuration(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id) or {}
    slave_device_enabled = "slave_device_enabled" in request.form
    upper_sensor_source = request.form.get("upper_sensor_source")
    if upper_sensor_source in {"master", "slave"}:
        slave_upper_sensor_enabled = slave_device_enabled and upper_sensor_source == "slave"
        master_upper_sensor_enabled = not slave_upper_sensor_enabled
    else:
        slave_upper_sensor_enabled = slave_device_enabled and "slave_upper_sensor_enabled" in request.form
        master_upper_sensor_enabled = "master_upper_sensor_enabled" in request.form
    if not slave_upper_sensor_enabled:
        master_upper_sensor_enabled = True
    elif slave_upper_sensor_enabled:
        master_upper_sensor_enabled = False
    main_sensor_enabled = master_upper_sensor_enabled or slave_upper_sensor_enabled
    setup_type = str(request.form.get("device_setup_type") or "custom").strip().lower()
    setup_features = DEVICE_SETUP_TYPE_FEATURES.get(setup_type)
    municipal_feature_enabled = "municipal_sensor_enabled" in request.form
    water_flow_sensor_enabled = municipal_feature_enabled and "water_flow_sensor_enabled" in request.form
    water_pressure_sensor_enabled = (
        municipal_feature_enabled
        and not water_flow_sensor_enabled
        and "water_pressure_sensor_enabled" in request.form
    )
    if municipal_feature_enabled and not water_flow_sensor_enabled and not water_pressure_sensor_enabled:
        water_flow_sensor_enabled = True
    source_tank_enabled = "source_tank_monitoring_enabled" in request.form
    municipal_valve_enabled = "municipal_valve_enabled" in request.form
    source_outlet_valve_enabled = "source_outlet_valve_enabled" in request.form
    auto_mode_enabled = "auto_mode_enabled" in request.form
    if setup_features is not None:
        source_tank_enabled = setup_features["source_tank_monitoring_enabled"]
        if setup_type not in {"municipal_direct", "dual_source_pumped"}:
            municipal_feature_enabled = False
        if setup_type != "dual_source_pumped":
            municipal_valve_enabled = False
            source_outlet_valve_enabled = False
    try:
        updated_config = upsert_device_service_config(
            scoped_device_id,
            device_setup_type=setup_type,
            main_sensor_enabled=main_sensor_enabled,
            master_upper_sensor_enabled=master_upper_sensor_enabled,
            slave_device_enabled=slave_device_enabled,
            slave_upper_sensor_enabled=slave_upper_sensor_enabled,
            source_tank_monitoring_enabled=source_tank_enabled,
            municipal_sensor_enabled=municipal_feature_enabled,
            municipal_valve_enabled=municipal_valve_enabled,
            source_outlet_valve_enabled=source_outlet_valve_enabled,
            starter_contactor_sensor_enabled=("starter_contactor_sensor_enabled" in request.form),
            motor_current_sensor_enabled=("motor_current_sensor_enabled" in request.form),
            water_flow_sensor_enabled=water_flow_sensor_enabled,
            water_pressure_sensor_enabled=water_pressure_sensor_enabled,
            master_turbidity_enabled=("master_turbidity_enabled" in request.form),
            slave_turbidity_enabled=("slave_turbidity_enabled" in request.form),
            relay_enabled=("relay_enabled" in request.form),
            buzzer_enabled=("buzzer_enabled" in request.form),
            led_display_enabled=("led_display_enabled" in request.form),
            ai_analysis_enabled=("ai_analysis_enabled" in request.form),
            auto_mode_enabled=auto_mode_enabled,
            cloud_feed_mode=(
                DEVICE_SERVICE_CLOUD_FEED_OFF
                if "cloud_feed_disabled" in request.form
                else (
                    DEVICE_SERVICE_CLOUD_FEED_FULL
                    if "ai_analysis_enabled" in request.form
                    else DEVICE_SERVICE_CLOUD_FEED_BASIC
                )
            ),
            ota_enabled=False,
            local_firmware_upload_enabled=("local_firmware_upload_enabled" in request.form),
            android_sso_session_limit=request.form.get("android_sso_session_limit"),
        )
    except ValueError as exc:
        return device_detail_action_response(scoped_device_id, error=str(exc), status_code=400)
    except Exception as exc:
        logger.exception("Could not save runtime configuration for %s", scoped_device_id)
        return device_detail_action_response(
            scoped_device_id,
            error=f"Unable to save runtime configuration: {exc}",
            status_code=500,
        )

    queued_command = build_device_service_command(updated_config)
    logger.info(
        "Admin water features saved: device=%s municipal=%s lower_turbidity=%s upper_turbidity=%s command=%s",
        scoped_device_id,
        "ON" if updated_config.get("municipal_sensor_enabled") else "OFF",
        "ON" if updated_config.get("master_turbidity_enabled") else "OFF",
        "ON" if updated_config.get("slave_turbidity_enabled") else "OFF",
        queued_command,
    )
    queue_result, queue_error = safe_queue_device_detail_command(
        queued_command,
        scoped_device_id,
        "Unable to queue runtime configuration update",
    )
    simulator_off_commands, simulator_off_errors = disable_orphaned_device_simulators(
        scoped_device_id,
        snapshot,
        updated_config,
    )
    if simulator_off_errors:
        queue_error = "; ".join([error for error in [queue_error, *simulator_off_errors] if error])
    automatic_simulator_commands = []
    automatic_simulator_errors = []
    simulator_firmware_detected = any(
        key in snapshot
        for key in ("simulator", "upper_tank_simulator", "lower_tank_simulator")
    )
    if simulator_firmware_detected and setup_features is not None:
        for command in automatic_simulator_commands_for_setup(setup_type, updated_config):
            result, error = safe_queue_device_detail_command(
                command,
                scoped_device_id,
                f"Unable to queue automatic simulator setup command {command}",
            )
            if error:
                automatic_simulator_errors.append(error)
            elif result:
                automatic_simulator_commands.append(command)
    if automatic_simulator_errors:
        queue_error = "; ".join([error for error in [queue_error, *automatic_simulator_errors] if error])
    logger.info(
        "Admin water feature command queue result: device=%s municipal=%s lower_turbidity=%s upper_turbidity=%s queued=%s error=%s",
        scoped_device_id,
        "ON" if updated_config.get("municipal_sensor_enabled") else "OFF",
        "ON" if updated_config.get("master_turbidity_enabled") else "OFF",
        "ON" if updated_config.get("slave_turbidity_enabled") else "OFF",
        bool(queue_result),
        queue_error or "none",
    )
    try:
        log_audit_event(
            actor=current_actor_username(),
            action="update_device_detail_configuration",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "service_config": updated_config,
                "device_setup_type": setup_type,
                "queued_command": queued_command,
                "queue_error": queue_error,
                "simulator_off_commands": simulator_off_commands,
                "automatic_simulator_commands": automatic_simulator_commands,
                "automatic_simulator_errors": automatic_simulator_errors,
                "android_sessions_preserved": True,
            },
        )
    except Exception as exc:
        logger.warning("Unable to log runtime configuration audit event for %s: %s", scoped_device_id, exc)
    auto_mode_label = "Enabled" if updated_config.get("auto_mode_enabled") else "Disabled"
    if queue_error:
        return device_detail_action_response(
            scoped_device_id,
            (
                f"Configuration saved in Flask. Auto Start/Stop is {auto_mode_label}. "
                "The device command was not queued, so the controller will apply it after the next successful sync/queue."
            ),
            title="Configuration saved in Flask",
            detail_lines=[queue_error],
            service_config=updated_config,
            queued_command=queued_command,
        )
    simulator_message = (
        f" Disabled simulator commands queued: {', '.join(simulator_off_commands)}."
        if simulator_off_commands
        else ""
    )
    automatic_simulator_message = (
        f" Automatic simulator scenario queued for {setup_type}."
        if automatic_simulator_commands
        else ""
    )
    message = (
        f"Configuration saved. Auto Start/Stop is {auto_mode_label}. "
        f"Device changes apply on the next command poll.{simulator_message}{automatic_simulator_message}"
    )
    return device_detail_action_response(
        scoped_device_id,
        message,
        service_config=updated_config,
        queued_command=queued_command,
        queue_result=queue_result,
    )


@app.route("/downloads/installation-guide")
@customer_required
def installation_guide_download():
    guide_path = Path(__file__).resolve().parents[1] / "docs" / "SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf"
    if not guide_path.is_file():
        abort(404)
    return send_file(
        guide_path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="SaleWell-Smart-Tank-Customer-Installation-Guide-English-Hindi.pdf",
        max_age=0,
    )


@app.route("/devices/<device_id>/thresholds", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_thresholds(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    try:
        updated_settings = upsert_device_automation_settings(
            scoped_device_id,
            auto_start_pct=request.form.get("auto_start_pct"),
            auto_stop_pct=request.form.get("auto_stop_pct"),
            snapshot=snapshot,
            source="admin_dashboard",
        )
    except ValueError as exc:
        return device_detail_action_response(scoped_device_id, error=str(exc), status_code=400)
    except Exception as exc:
        logger.exception("Could not save tank thresholds for %s", scoped_device_id)
        return device_detail_action_response(
            scoped_device_id,
            error=f"Unable to save tank thresholds: {exc}",
            status_code=500,
        )

    queued_command = build_device_automation_command(updated_settings)
    queue_result, queue_error = safe_queue_device_detail_command(
        queued_command,
        scoped_device_id,
        "Unable to queue threshold update",
    )
    try:
        log_audit_event(
            actor=current_actor_username(),
            action="update_device_detail_thresholds",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "automation_settings": updated_settings,
                "queued_command": queued_command,
                "queue_error": queue_error,
                "source": "admin_dashboard",
            },
        )
    except Exception as exc:
        logger.warning("Unable to log threshold audit event for %s: %s", scoped_device_id, exc)
    message = (
        f"Tank thresholds saved for {scoped_device_id}. "
        f"Start at {updated_settings['auto_start_pct']:g}% and stop at {updated_settings['auto_stop_pct']:g}%. "
        "Device changes apply on the next command poll."
    )
    if queue_error:
        message = (
            f"Tank thresholds saved in Flask for {scoped_device_id}. "
            f"Start at {updated_settings['auto_start_pct']:g}% and stop at {updated_settings['auto_stop_pct']:g}%. "
            "The device command was not queued, so the controller will apply it after the next successful sync/queue."
        )
    return device_detail_action_response(
        scoped_device_id,
        message,
        title="Tank thresholds saved in Flask" if queue_error else "Action saved",
        detail_lines=[queue_error] if queue_error else [],
        automation_settings=updated_settings,
        queued_command=queued_command,
        queue_result=queue_result,
    )


@app.route("/devices/<device_id>/peer-channel", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_peer_channel(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    try:
        requested_channel = _coerce_optional_peer_channel_value(
            request.form.get("direct_peer_wifi_channel") or request.form.get("peer_channel")
        )
        if requested_channel is None:
            raise ValueError("Peer channel is required.")
        updated_config = upsert_device_service_config(
            scoped_device_id,
            direct_peer_wifi_channel=requested_channel,
        )
        queued_command = build_device_peer_channel_command(requested_channel)
    except ValueError as exc:
        return device_detail_action_response(scoped_device_id, error=str(exc), status_code=400)
    except Exception as exc:
        logger.exception("Could not save peer channel for %s", scoped_device_id)
        return device_detail_action_response(
            scoped_device_id,
            error=f"Unable to save peer channel: {exc}",
            status_code=500,
        )

    queue_result, queue_error = safe_queue_device_detail_command(
        queued_command,
        scoped_device_id,
        "Unable to queue peer channel update",
    )
    try:
        log_audit_event(
            actor=current_actor_username(),
            action="update_device_detail_peer_channel",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "direct_peer_wifi_channel": requested_channel,
                "service_config": updated_config,
                "queued_command": queued_command,
                "queue_error": queue_error,
                "source": "admin_dashboard",
            },
        )
    except Exception as exc:
        logger.warning("Unable to log peer channel audit event for %s: %s", scoped_device_id, exc)
    message = (
        f"Peer channel saved for {scoped_device_id}. "
        f"Channel {requested_channel} will apply on the next device command poll."
    )
    if queue_error:
        message = (
            f"Peer channel {requested_channel} saved in Flask for {scoped_device_id}. "
            "The device command was not queued, so the controller will apply it after the next successful sync/queue."
        )
    return device_detail_action_response(
        scoped_device_id,
        message,
        title="Peer channel saved in Flask" if queue_error else "Action saved",
        detail_lines=[queue_error] if queue_error else [],
        service_config=updated_config,
        direct_peer_wifi_channel=requested_channel,
        queued_command=queued_command,
        queue_result=queue_result,
    )


@app.route("/devices/<device_id>/ping", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_ping(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    target = request.form.get("target") or request.form.get("node") or request.form.get("ping_target")
    try:
        ping_result = build_device_ping_result(scoped_device_id, target)
    except Exception as exc:
        logger.exception("Device ping failed for %s target=%s", scoped_device_id, target)
        return jsonify({"ok": False, "error": str(exc) or "Unable to ping device node."}), 200

    try:
        persist_device_events([ping_result["event"]], default_device_id=scoped_device_id)
    except Exception as exc:
        logger.warning("Unable to persist ping event for %s: %s", scoped_device_id, exc)
    try:
        log_audit_event(
            actor=current_actor_username(),
            action="ping_device_node",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "ping_target": ping_result["target"],
                "ping_status": ping_result["status"],
                "reachable": ping_result["reachable"],
                "disabled": ping_result["disabled"],
                "queued_command": ping_result.get("queued_command"),
                "command_id": ping_result.get("command_id"),
            },
        )
    except Exception as exc:
        logger.warning("Unable to log ping audit event for %s: %s", scoped_device_id, exc)

    return jsonify(
        {
            "ok": True,
            "title": ping_result["title"],
            "message": ping_result["message"],
            "detail_lines": ping_result["detail_lines"],
            "saved_peer_channel": ping_result.get("saved_peer_channel"),
            "target": ping_result["target"],
            "status": ping_result["status"],
            "reachable": ping_result["reachable"],
            "disabled": ping_result["disabled"],
            "queued_command": ping_result.get("queued_command"),
            "command_id": ping_result.get("command_id"),
            "expected_ping_nonce": ping_result.get("expected_ping_nonce"),
        }
    )


@app.route("/devices/<device_id>/mobile/logout", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_mobile_logout(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    account = fetch_customer_account(scoped_device_id)
    if not account:
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_error="Customer account not found for this device.",
            )
        )
    clear_active_platform_session(
        SESSION_PLATFORM_ANDROID,
        "customer",
        username=scoped_device_id,
        device_id=scoped_device_id,
    )
    rotate_mobile_session_epoch(scoped_device_id)
    log_audit_event(
        actor=current_actor_username(),
        action="logout_all_mobile_devices",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={"platform": SESSION_PLATFORM_ANDROID},
    )
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message="All Android app sessions were signed out for this customer.",
        )
    )


@app.route("/devices/<device_id>/sensor/calibrate", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_sensor_calibrate(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    service_config = resolve_device_service_config(
        scoped_device_id,
        account=fetch_customer_account(scoped_device_id),
        snapshot=snapshot,
    )
    sensor = str(request.form.get("sensor") or "upper").strip().lower()
    lower_requested = sensor in {"lower", "source", "source_tank"}
    if lower_requested and not bool(service_config.get("source_tank_monitoring_enabled")):
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_error="Lower/source sensor calibration is not available because lower sensor service is disabled.",
            )
        )

    command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"
    command_target = scoped_device_id

    result = queue_command(command, target_device=command_target)
    if isinstance(result, tuple):
        payload, _status_code = result
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_error=payload.get("error") or "Unable to queue calibration command.",
            )
        )
    log_audit_event(
        actor=current_actor_username(),
        action="queue_device_calibration",
        target_type="device",
        target_id=command_target,
        device_id=scoped_device_id,
        details={
            "command": result.get("command"),
            "sensor": "lower" if lower_requested else "upper",
            "command_target": command_target,
            "queued_at": result.get("queued_at"),
        },
    )
    sensor_label = "lower/source" if lower_requested else "upper"
    delivery_note = " The master will forward upper calibration to the slave MCU when slave upper sensing is enabled."
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message=f"{sensor_label.title()} sensor calibration command queued for {command_target}.{'' if lower_requested else delivery_note}",
        )
    )


@app.route("/devices/<device_id>/sensor/configure", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_sensor_configure(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id)
    service_config = resolve_device_service_config(
        scoped_device_id,
        account=fetch_customer_account(scoped_device_id),
        snapshot=snapshot,
    )
    sensor = str(request.values.get("sensor") or "upper").strip().lower()
    lower_requested = sensor in {"lower", "source", "source_tank"}
    height_cm = request.values.get("height_cm", type=float)
    capacity_liters = request.values.get("capacity_liters", type=float)
    action = str(request.values.get("action") or "save").strip().lower()
    if lower_requested and not bool(service_config.get("source_tank_monitoring_enabled")):
        return device_detail_action_response(
            scoped_device_id,
            error="Lower/source tank setup is not available because lower sensor service is disabled.",
            status_code=400,
        )
    if capacity_liters is None:
        return device_detail_action_response(
            scoped_device_id,
            error="Tank capacity is required.",
            status_code=400,
        )
    if action in {"save_calibrate", "save_and_calibrate", "calibrate"} and height_cm is None:
        return device_detail_action_response(
            scoped_device_id,
            error="Tank height is required before calibration.",
            status_code=400,
        )
    if height_cm is not None and (height_cm < 2.1 or height_cm > 500):
        return device_detail_action_response(
            scoped_device_id,
            error="Tank height must be between 2.1 and 500 cm.",
            status_code=400,
        )
    if capacity_liters < 50 or capacity_liters > 50000:
        return device_detail_action_response(
            scoped_device_id,
            error="Tank capacity must be between 50 and 50000 liters.",
            status_code=400,
        )

    try:
        if lower_requested:
            saved_config = upsert_device_service_config(
                scoped_device_id,
                lower_tank_height_cm=height_cm,
                lower_tank_capacity_liters=capacity_liters,
            )
        else:
            saved_config = upsert_device_service_config(
                scoped_device_id,
                tank_height_cm=height_cm,
                tank_capacity_liters=capacity_liters,
                upper_tank_height_cm=height_cm,
                upper_tank_capacity_liters=capacity_liters,
            )
    except Exception as exc:
        logger.exception("Could not save tank setup for %s", scoped_device_id)
        return device_detail_action_response(
            scoped_device_id,
            error=f"Unable to save tank setup: {exc}",
            status_code=500,
        )

    if height_cm is not None:
        command = f"CONFIG_LOWER:{height_cm:.1f}:{capacity_liters:.1f}" if lower_requested else f"CONFIG_UPPER:{height_cm:.1f}:{capacity_liters:.1f}"
    else:
        command = f"CONFIG_LOWER_CAPACITY:{capacity_liters:.1f}" if lower_requested else f"CONFIG_CAPACITY:{capacity_liters:.1f}"
    result, queue_error = safe_queue_device_detail_command(
        command,
        scoped_device_id,
        "Unable to queue tank capacity command",
    )
    try:
        log_audit_event(
            actor=current_actor_username(),
            action="queue_device_tank_capacity",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "command": (result or {}).get("command") or command,
                "sensor": "lower" if lower_requested else "upper",
                "height_cm": round(height_cm, 1) if height_cm is not None else None,
                "capacity_liters": round(capacity_liters, 1),
                "queued_at": (result or {}).get("queued_at"),
                "queue_error": queue_error,
            },
        )
    except Exception as exc:
        logger.warning("Unable to log tank setup audit event for %s: %s", scoped_device_id, exc)
    calibration_result = None
    calibration_queue_error = ""
    if action in {"save_calibrate", "save_and_calibrate", "calibrate"}:
        calibration_command = "CALIBRATE_LOWER" if lower_requested else "CALIBRATE_UPPER"
        calibration_result, calibration_queue_error = safe_queue_device_detail_command(
            calibration_command,
            scoped_device_id,
            "Tank setup saved, but unable to queue calibration",
        )
        try:
            log_audit_event(
                actor=current_actor_username(),
                action="queue_device_calibration",
                target_type="device",
                target_id=scoped_device_id,
                device_id=scoped_device_id,
                details={
                    "command": (calibration_result or {}).get("command") or calibration_command,
                    "sensor": "lower" if lower_requested else "upper",
                    "command_target": scoped_device_id,
                    "queued_at": (calibration_result or {}).get("queued_at"),
                    "queued_after_tank_setup": True,
                    "queue_error": calibration_queue_error,
                },
            )
        except Exception as exc:
            logger.warning("Unable to log sensor calibration audit event for %s: %s", scoped_device_id, exc)

    sensor_label = "Lower/source" if lower_requested else "Upper"
    config_parts = [f"{sensor_label} tank setup saved in Flask: {capacity_liters:.1f} L"]
    if height_cm is not None:
        config_parts.insert(0, f"{sensor_label} tank height saved in Flask: {height_cm:.1f} cm")
    if queue_error:
        config_parts.append("The device setup command was not queued, so the controller will apply it after the next successful sync/queue.")
    else:
        config_parts.append(f"{sensor_label} tank setup command queued.")
    if calibration_result and not calibration_queue_error:
        config_parts.append(f"{sensor_label} calibration command queued.")
    elif calibration_queue_error:
        config_parts.append("Calibration was not queued.")
    detail_lines = [line for line in (queue_error, calibration_queue_error) if line]
    return device_detail_action_response(
        scoped_device_id,
        ". ".join(config_parts),
        title="Tank setup saved in Flask" if detail_lines else "Action saved",
        detail_lines=detail_lines,
        service_config=saved_config,
        queued_command=command,
        queue_result=result,
    )


@app.route("/admin/customers/<device_id>/simulator", methods=["POST"])
@app.route("/devices/<device_id>/simulator", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_simulator(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id) or {}
    service_config = resolve_device_service_config(scoped_device_id, snapshot=snapshot)
    simulator_target = str(request.form.get("simulator_target") or "tank").strip().lower()
    if simulator_target == "all":
        commands = [dependency[2] for dependency in SIMULATOR_FEATURE_DEPENDENCIES]
        queued_commands = []
        queue_errors = []
        for command in commands:
            result, error = safe_queue_device_detail_command(
                command,
                scoped_device_id,
                f"Unable to queue simulator disable command {command}",
            )
            if error:
                queue_errors.append(error)
            elif result:
                queued_commands.append(result.get("command") or command)
        log_audit_event(
            actor=current_actor_username(),
            action="queue_all_simulators_disable",
            target_type="device",
            target_id=scoped_device_id,
            device_id=scoped_device_id,
            details={
                "commands": queued_commands,
                "errors": queue_errors,
            },
        )
        if queue_errors:
            return redirect(
                url_for(
                    "device_detail_page",
                    device_id=scoped_device_id,
                    config_error="Some simulator OFF commands could not be queued: " + "; ".join(queue_errors),
                )
            )
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_message="All simulator disable commands queued for real-hardware testing.",
                simulator_state="off",
                municipal_simulator_state="off",
                valve_simulator_state="off",
                outlet_valve_simulator_state="off",
            )
        )
    target_config = {
        "tank": (device_simulator_enabled(scoped_device_id, snapshot=snapshot), "SIMULATOR", "Tank level", bool(service_config.get("main_sensor_enabled")), None),
        "municipal": (boolish_enabled(snapshot.get("municipal_sensor_simulated"), default=False), "MUNICIPAL_SIMULATOR", "Municipal water", bool(service_config.get("municipal_sensor_enabled")), "municipal_feature_enabled"),
        "water_flow": (boolish_enabled(snapshot.get("municipal_sensor_simulated"), default=False), "MUNICIPAL_SIMULATOR", "Water-flow sensor", bool(service_config.get("municipal_sensor_enabled")) and bool(service_config.get("water_flow_sensor_enabled")), "municipal_feature_enabled"),
        "water_pressure": (boolish_enabled(snapshot.get("municipal_sensor_simulated"), default=False), "MUNICIPAL_SIMULATOR", "Water-pressure sensor", bool(service_config.get("municipal_sensor_enabled")) and bool(service_config.get("water_pressure_sensor_enabled")), "municipal_feature_enabled"),
        "valve": (boolish_enabled(snapshot.get("municipal_valve_simulated"), default=False), "MUNICIPAL_VALVE_SIMULATOR", "Inlet motorized valve", bool(service_config.get("municipal_valve_enabled")), "municipal_valve_feature_enabled"),
        "outlet_valve": (boolish_enabled(snapshot.get("source_outlet_valve_simulated"), default=False), "SOURCE_OUTLET_VALVE_SIMULATOR", "Outlet motorized valve", bool(service_config.get("source_outlet_valve_enabled")), "source_pump_fill_feature_enabled"),
        "lower_turbidity": (boolish_enabled(snapshot.get("lower_turbidity_simulated"), default=False), "LOWER_TURBIDITY_SIMULATOR", "Lower turbidity", bool(service_config.get("master_turbidity_enabled")), "master_turbidity_enabled"),
        "upper_turbidity": (boolish_enabled(snapshot.get("upper_turbidity_simulated"), default=False), "UPPER_TURBIDITY_SIMULATOR", "Upper turbidity", bool(service_config.get("slave_turbidity_enabled")), "slave_turbidity_enabled"),
    }
    if simulator_target not in target_config:
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_error="Unknown simulator target.",
            )
        )
    simulator_enabled, command_prefix, target_label, feature_enabled, live_feature_key = target_config[simulator_target]
    requested_state = str(request.form.get("simulator_enabled") or "").strip().lower()
    desired_enabled = (
        boolish_enabled(requested_state, default=not simulator_enabled)
        if requested_state in {"0", "1", "true", "false", "yes", "no", "on", "off"}
        else not simulator_enabled
    )
    if desired_enabled and not feature_enabled:
        return redirect(
            url_for(
                "device_detail_page",
                device_id=scoped_device_id,
                config_error=f"Enable the {target_label} feature before enabling its simulator.",
            )
        )
    command = f"{command_prefix}_{'ON' if desired_enabled else 'OFF'}"
    logger.info(
        "Simulator request received: device=%s target=%s current=%s command=%s ajax=%s",
        scoped_device_id, simulator_target, "ON" if simulator_enabled else "OFF", command,
        request.headers.get("X-Requested-With") == "XMLHttpRequest",
    )
    prerequisite_command = None
    if (
        desired_enabled
        and live_feature_key
        and not runtime_sync_service_enabled(snapshot, live_feature_key)
    ):
        prerequisite_command = build_device_service_command(service_config)
        prerequisite_result = queue_command(prerequisite_command, target_device=scoped_device_id)
        if isinstance(prerequisite_result, tuple):
            payload, _status_code = prerequisite_result
            error = payload.get("error") or f"Unable to synchronize runtime configuration for {scoped_device_id}."
            return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    result = queue_command(command, target_device=scoped_device_id)
    if isinstance(result, tuple):
        payload, _status_code = result
        error = payload.get("error") or f"Unable to queue simulator command for {scoped_device_id}."
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    log_audit_event(
        actor=current_actor_username(),
        action="queue_device_simulator_toggle" if simulator_target == "tank" else "queue_targeted_simulator_toggle",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={
            "command": result.get("command"),
            "prerequisite_command": prerequisite_command,
            "previous_simulator_enabled": simulator_enabled,
            "simulator_target": simulator_target,
            "mqtt_delivery": result.get("mqtt_delivery"),
            "queued_at": result.get("queued_at"),
        },
    )

    message = f"{target_label} simulator {'enable' if desired_enabled else 'disable'} command queued."
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message=message,
            simulator_state=("on" if desired_enabled else "off") if simulator_target == "tank" else "",
            valve_simulator_state=("on" if desired_enabled else "off") if simulator_target == "valve" else "",
            outlet_valve_simulator_state=("on" if desired_enabled else "off") if simulator_target == "outlet_valve" else "",
        )
    )


@app.route("/admin/customers/<device_id>/municipal-simulator", methods=["POST"])
@app.route("/devices/<device_id>/municipal-simulator", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_municipal_simulator(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id) or {}
    simulator_enabled = boolish_enabled(snapshot.get("municipal_sensor_simulated"), default=False)
    command = "MUNICIPAL_SIMULATOR_OFF" if simulator_enabled else "MUNICIPAL_SIMULATOR_ON"
    logger.info(
        "Municipal sensor simulator request received: device=%s current=%s command=%s ajax=%s",
        scoped_device_id, "ON" if simulator_enabled else "OFF", command,
        request.headers.get("X-Requested-With") == "XMLHttpRequest",
    )
    result = queue_command(command, target_device=scoped_device_id)
    if isinstance(result, tuple):
        payload, _status_code = result
        error = payload.get("error") or f"Unable to queue municipal simulator command for {scoped_device_id}."
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    log_audit_event(
        actor=current_actor_username(),
        action="queue_municipal_sensor_simulator_toggle",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={
            "command": result.get("command"),
            "previous_municipal_simulator_enabled": simulator_enabled,
            "queued_at": result.get("queued_at"),
        },
    )
    enabled = not simulator_enabled
    logger.info(
        "Municipal sensor simulator command queued: device=%s requested=%s command=%s",
        scoped_device_id,
        "ON" if enabled else "OFF",
        command,
    )
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message=(
                "Municipal sensor simulator enable command queued."
                if enabled
                else "Municipal sensor simulator disable command queued."
            ),
            municipal_simulator_state="on" if enabled else "off",
        )
    )


@app.route("/admin/customers/<device_id>/municipal-valve-simulator", methods=["POST"])
@app.route("/devices/<device_id>/municipal-valve-simulator", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_municipal_valve_simulator(device_id):
    scoped_device_id = current_scope_device_id(device_id)
    snapshot = fetch_device_snapshot(scoped_device_id) or {}
    simulator_enabled = boolish_enabled(snapshot.get("municipal_valve_simulated"), default=False)
    enabled = not simulator_enabled
    command = f"MUNICIPAL_VALVE_SIMULATOR_{'ON' if enabled else 'OFF'}"
    logger.info(
        "Municipal valve simulator request received: device=%s current=%s command=%s ajax=%s",
        scoped_device_id, "ON" if simulator_enabled else "OFF", command,
        request.headers.get("X-Requested-With") == "XMLHttpRequest",
    )
    result = queue_command(command, target_device=scoped_device_id)
    if isinstance(result, tuple):
        payload, _status_code = result
        error = payload.get("error") or f"Unable to queue inlet motorized valve simulator command for {scoped_device_id}."
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    log_audit_event(
        actor=current_actor_username(),
        action="queue_municipal_valve_simulator_toggle",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={"command": command, "enabled": enabled, "queued_at": result.get("queued_at")},
    )
    logger.info(
        "Municipal valve simulator command queued: device=%s requested=%s command=%s",
        scoped_device_id, "ON" if enabled else "OFF", command,
    )
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message=f"Inlet motorized valve simulator {'enable' if enabled else 'disable'} command queued.",
            valve_simulator_state="on" if enabled else "off",
        )
    )


@app.route("/admin/customers/<device_id>/turbidity-simulator/<role>", methods=["POST"])
@app.route("/devices/<device_id>/turbidity-simulator/<role>", methods=["POST"])
@admin_required
@csrf_protect
def admin_device_detail_turbidity_simulator(device_id, role):
    scoped_device_id = current_scope_device_id(device_id)
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in {"lower", "upper"}:
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error="Unknown turbidity simulator role."))

    snapshot = fetch_device_snapshot(scoped_device_id) or {}
    simulator_enabled = boolish_enabled(
        snapshot.get(f"{normalized_role}_turbidity_simulated"), default=False
    )
    command = f"{normalized_role.upper()}_TURBIDITY_SIMULATOR_{'OFF' if simulator_enabled else 'ON'}"
    logger.info(
        "Turbidity simulator request received: device=%s role=%s current=%s command=%s ajax=%s",
        scoped_device_id, normalized_role, "ON" if simulator_enabled else "OFF", command,
        request.headers.get("X-Requested-With") == "XMLHttpRequest",
    )
    result = queue_command(command, target_device=scoped_device_id)
    if isinstance(result, tuple):
        payload, _status_code = result
        error = payload.get("error") or f"Unable to queue {normalized_role} turbidity simulator command."
        return redirect(url_for("device_detail_page", device_id=scoped_device_id, config_error=error))

    enabled = not simulator_enabled
    log_audit_event(
        actor=current_actor_username(),
        action="queue_turbidity_sensor_simulator_toggle",
        target_type="device",
        target_id=scoped_device_id,
        device_id=scoped_device_id,
        details={"role": normalized_role, "command": command, "enabled": enabled},
    )
    logger.info(
        "Turbidity simulator command queued: device=%s role=%s requested=%s command=%s",
        scoped_device_id, normalized_role, "ON" if enabled else "OFF", command,
    )
    return redirect(
        url_for(
            "device_detail_page",
            device_id=scoped_device_id,
            config_message=f"{normalized_role.title()} turbidity simulator {'enable' if enabled else 'disable'} command queued.",
            turbidity_simulator_role=normalized_role,
            turbidity_simulator_state="on" if enabled else "off",
        )
    )


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
    snapshot = fetch_device_snapshot(scoped_device_id, include_transition_counts=False)
    if not snapshot:
        # Newly registered devices can have saved configuration before the first
        # telemetry packet arrives, so return an empty snapshot shell instead of
        # failing the detail page.
        snapshot = build_empty_snapshot_payload(scoped_device_id)
    include_history = request.args.get("history", "1").strip().lower() not in {"0", "false", "no", "off"}
    include_events = request.args.get("events", "1").strip().lower() not in {"0", "false", "no", "off"}
    include_alerts = request.args.get("alerts", "1").strip().lower() not in {"0", "false", "no", "off"}
    include_audit = request.args.get("audit", "1").strip().lower() not in {"0", "false", "no", "off"}
    include_details = request.args.get("details", "1").strip().lower() not in {"0", "false", "no", "off"}
    simulator_enabled = device_simulator_enabled(scoped_device_id, snapshot=snapshot)
    snapshot_payload = strip_ip_address_fields(snapshot, keep_device_local_url=True)
    snapshot_has_live_simulator_status = (
        simulator_payload_status(snapshot_payload) is not None
        and str(snapshot.get("telemetry_status") or "").strip().lower() != "no-data"
    )
    snapshot_payload["simulator_enabled"] = simulator_enabled
    snapshot_payload["simulator_status"] = "ON" if simulator_enabled else "OFF"
    if simulator_enabled and not snapshot_has_live_simulator_status:
        snapshot_payload["simulator"] = "ON"
    # Frequent browser polling needs only the latest telemetry snapshot. Saved
    # configuration is already embedded in the page and is fetched again only
    # for an explicit/full refresh.
    service_config = resolve_device_service_config(scoped_device_id, snapshot=snapshot) if include_details else {}
    current_system_status = build_system_status_payload(
        snapshot,
        device_id=scoped_device_id,
        service_config=service_config,
    )
    log_device_connection_status(
        scoped_device_id,
        current_system_status.get("device") == "online",
        telemetry_status=current_system_status.get("telemetry_status"),
        seconds_since_sync=current_system_status.get("seconds_since_sync"),
    )
    payload = {
        "device_id": scoped_device_id,
        "system_status": current_system_status,
        "monitoring_summary": build_monitoring_summary_payload(snapshot, device_id=scoped_device_id),
        "snapshot": snapshot_payload,
    }
    if include_details:
        payload["service_config"] = service_config
        payload["automation_settings"] = fetch_device_automation_settings(scoped_device_id, snapshot=snapshot)
        payload["current_saved_config"] = build_current_saved_config(scoped_device_id)
    if include_alerts:
        payload["alerts"] = fetch_filtered_alerts(limit=10, device_id=scoped_device_id)
    if include_audit:
        payload["audit"] = fetch_audit_events(limit=10, device_id=scoped_device_id)
    if include_history:
        payload["history"] = fetch_device_history(scoped_device_id, limit=48)
    if include_events:
        event_limit = max(10, min(request.args.get("event_limit", default=300, type=int), 500))
        payload["events"] = build_events(limit=event_limit, device_id=scoped_device_id, sync=False)
    return jsonify(payload)


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
    if CAPACITY_FEATURES.enabled("shared_ingestion_service"):
        ingest_device_sync(
            data,
            authenticated_device_id=auth_payload,
            source_ip=request.remote_addr,
            transport="http",
            defer_postprocess=True,
        )
    else:
        process_telemetry_payload(data, source_ip=request.remote_addr, transport="http", defer_postprocess=True)

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
    summary = load_persisted_dashboard_summary(scoped_device_id) or empty_dashboard_summary(scoped_device_id)
    summary = overlay_capacity_snapshot(summary, scoped_device_id, "dashboard_read_latest_state")
    return jsonify(summary.get("snapshot") or build_empty_snapshot_payload(scoped_device_id))


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
        rows = []
        if scoped_device_id and CAPACITY_FEATURES.enabled("history_read_narrow_table"):
            rows = list(reversed(fetch_narrow_history_rows(
                db.cursor(),
                scoped_device_id,
                get_device_source_mode(),
                800,
                start_at=start_dt.strftime(TIMESTAMP_FORMAT),
                end_at=end_exclusive.strftime(TIMESTAMP_FORMAT),
            )))
        if scoped_device_id and not rows:
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
        if not scoped_device_id:
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
                "ai_usage_rate": row.get("ai_usage_rate"),
                "time": format_timestamp(row.get("created_at") or row.get("recorded_at"))
            }
            for row in rows
        ]
    )


@app.route("/health")
def health():
    mysql_config = mysql_connection_config()
    return {
        "status": "ok",
        "deploy_marker": DEPLOY_MARKER,
        "database": mysql_config.get("database"),
        "database_backend": DB_BACKEND,
        "version": API_VERSION,
        "swt_version": SWT_VERSION,
        "ota_contract_version": OTA_CONTRACT_VERSION,
        "ota_authorization": "artifact_hmac_sha256",
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
    snapshot = load_dashboard_snapshot(
        scoped_device_id,
        prefer_capacity=CAPACITY_FEATURES.enabled("dashboard_read_latest_state"),
    )
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
    summary = load_persisted_dashboard_summary(scoped_device_id) or empty_dashboard_summary(scoped_device_id)
    payload = dict(summary.get("system_status") or {})
    if CAPACITY_FEATURES.enabled("capacity_metrics"):
        payload["capacity"] = {
            "features": CAPACITY_FEATURES.snapshot(),
            "request_metrics": CAPACITY_REQUEST_METRICS.snapshot(),
        }
        if any(
            CAPACITY_FEATURES.enabled(name)
            for name in ("cron_health", "database_size_alerts", "offserver_backup")
        ):
            from flask_app.capacity_operations import fetch_runtime_status

            with get_db() as db:
                payload["capacity"]["operations"] = fetch_runtime_status(db.cursor())
    return jsonify(payload)


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
    limit = max(1, min(request.args.get("limit", default=12, type=int), 500))
    include_local_logs = str(request.args.get("local_logs", "0")).strip().lower() in {"1", "true", "yes", "on"}
    return jsonify(
        build_events(
            limit,
            device_id=current_scope_device_id(request.args.get("device_id", type=str)),
            sync=False,
            include_local_logs=include_local_logs,
        )
    )


@app.route("/dashboard/bootstrap")
@login_required
def dashboard_bootstrap():
    event_limit = max(1, min(request.args.get("event_limit", default=5, type=int), 30))
    audit_limit = max(1, min(request.args.get("audit_limit", default=5, type=int), 30))
    response = customer_cloud_feed_block_response()
    if response:
        return response
    scoped_device_id = current_scope_device_id(request.args.get("device_id", type=str))
    summary = load_persisted_dashboard_summary(scoped_device_id) or empty_dashboard_summary(scoped_device_id)
    summary = overlay_capacity_snapshot(summary, scoped_device_id, "dashboard_read_latest_state")
    payload = dict(summary)
    payload.update({
        "events": list(summary.get("events") or [])[:event_limit],
        "audit": list(summary.get("audit") or [])[:audit_limit],
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
    })

    return jsonify(payload)


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
        logger.debug("Local device sync issue for %s via %s: %s", scoped_device_id, local_base_url, exc)
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
    try:
        payload = build_dashboard_analytics_singleflight(start_dt, end_exclusive, label, device_id=scoped_device_id)
    except Exception as exc:
        logger.exception("Dashboard analytics fallback used for %s: %s", scoped_device_id, exc)
        payload = build_analytics_fallback_payload(
            start_dt,
            end_exclusive,
            label,
            device_id=scoped_device_id,
            reason="AI analysis is using the last successful result while fresh analytics catches up.",
        )
    payload = attach_default_chart_windows(payload, device_id=scoped_device_id)
    return jsonify(payload)


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

    payload = build_dashboard_analytics_singleflight(start_dt, end_exclusive, label, device_id=scoped_device_id)
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


dashboard_summary_reconciler_stop = threading.Event()


def reconcile_dashboard_summaries():
    active_mode = get_device_source_mode()
    stale_before = (now_utc() - timedelta(seconds=DASHBOARD_SUMMARY_RECONCILE_SECONDS)).strftime(TIMESTAMP_FORMAT)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT tank_data.device_id, MAX(dashboard_summaries.updated_at) AS summary_updated_at
            FROM tank_data
            LEFT JOIN dashboard_summaries
              ON dashboard_summaries.device_id = tank_data.device_id
             AND dashboard_summaries.device_source = tank_data.device_source
            WHERE tank_data.device_id IS NOT NULL
              AND tank_data.device_id <> ''
              AND tank_data.device_source = ?
            GROUP BY tank_data.device_id
            HAVING MAX(dashboard_summaries.updated_at) IS NULL
                OR MAX(dashboard_summaries.updated_at) < ?
            ORDER BY summary_updated_at ASC
            LIMIT ?
            """,
            (active_mode, stale_before, DASHBOARD_SUMMARY_RECONCILE_BATCH_SIZE),
        ).fetchall()
    refreshed = 0
    for row in rows:
        if refresh_dashboard_summary(row.get("device_id")) is not None:
            refreshed += 1
        if dashboard_summary_reconciler_stop.wait(0.1):
            break
    logger.info("Dashboard summary reconciliation refreshed %s device(s)", refreshed)
    return refreshed


def dashboard_summary_reconciler_loop():
    # Let normal telemetry seed summaries first. Reconciliation is a bounded
    # stale-summary safety net and must never create a startup/database burst.
    if dashboard_summary_reconciler_stop.wait(DASHBOARD_SUMMARY_RECONCILE_SECONDS):
        return
    while not dashboard_summary_reconciler_stop.is_set():
        try:
            reconcile_dashboard_summaries()
        except Exception as exc:
            logger.warning("Dashboard summary reconciliation failed: %s", exc)
        if dashboard_summary_reconciler_stop.wait(DASHBOARD_SUMMARY_RECONCILE_SECONDS):
            break


def start_dashboard_summary_reconciler():
    if not DASHBOARD_SUMMARY_RECONCILIATION_ENABLED:
        return None
    worker = threading.Thread(
        target=dashboard_summary_reconciler_loop,
        name="dashboard-summary-reconciler",
        daemon=True,
    )
    worker.start()
    return worker


logger.info("Initializing database")
_mysql_config_for_log = mysql_connection_config()
logger.info(
    "Database backend resolved to MySQL: host=%s port=%s database=%s user=%s",
    _mysql_config_for_log.get("host"),
    _mysql_config_for_log.get("port"),
    _mysql_config_for_log.get("database"),
    _mysql_config_for_log.get("user"),
)
logger.info("SaleWell deploy marker: %s", DEPLOY_MARKER)
validate_runtime_db_configuration()
init_db_serialized()
# Runtime boot settings must be applied even when the schema revision is
# already current and init_db_serialized() skips init_db().
maybe_reset_device_source_mode_on_boot()
ensure_homepage_visitor_count_loaded()
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
start_dashboard_summary_reconciler()
atexit.register(stop_mqtt_bridge)
atexit.register(dashboard_summary_reconciler_stop.set)


if __name__ == "__main__":
    logger.info("Starting SaleWell Smart Tank Server")
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, threaded=True)




























