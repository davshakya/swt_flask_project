import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "production_preflight",
    ROOT / "scripts" / "production_preflight.py",
)
production_preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(production_preflight)


def valid_backend_env():
    return {
        "APP_SECRET_KEY": "a" * 40,
        "LOGIN_PASSWORD": "StrongAdminPassword2026!",
        "SESSION_COOKIE_SECURE": "true",
        "TRUST_PROXY_HEADERS": "true",
        "SWT_CORS_ALLOWED_ORIGINS": "https://salewell.co.in",
        "DB_BACKEND": "mysql",
        "DATABASE_URL": "mysql+pymysql://sale_user:strong_password@localhost:3306/sale_db",
        "SWT_DEVICE_ID": "swt-100-000-000-001",
        "SWT_DEVICE_API_KEY": "unique-device-key-with-more-than-32-characters",
        "SWT_DEVICE_SOURCE_MODE": "real",
        "SEED_VIRTUAL_DEVICE_ENVS": "false",
        "PURGE_VIRTUAL_DEVICE_ENVS_ON_BOOT": "true",
        "TELEMETRY_HISTORY_ENABLED": "true",
        "DATA_RETENTION_DAYS": "45",
        "RELAY_VERIFY_TLS": "true",
    }


def test_preflight_rejects_placeholder_database_url_fields():
    env = valid_backend_env()
    env["DATABASE_URL"] = (
        "mysql+pymysql://replace_user:replace_password@replace_host:3306/replace_database"
    )
    errors = []

    production_preflight.collect_backend_checks(env, errors, [])

    assert any("DATABASE_URL contains missing or placeholder fields" in error for error in errors)


def test_cpanel_profile_requires_shared_hosting_database_limits():
    env = valid_backend_env()
    env.update({
        "SWT_HOSTING_PROFILE": "cpanel",
        "MYSQL_AUTO_CREATE_DATABASE": "true",
        "MYSQL_OPTIMIZE_ENABLED": "true",
        "BACKGROUND_DB_MAX_WORKERS": "4",
    })
    errors = []

    production_preflight.collect_backend_checks(env, errors, [])

    assert "cPanel deployments must set MYSQL_AUTO_CREATE_DATABASE=false." in errors
    assert "cPanel deployments must set MYSQL_OPTIMIZE_ENABLED=false." in errors
    assert "cPanel deployments must set BACKGROUND_DB_MAX_WORKERS=1." in errors
