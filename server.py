import sys
import time
import random
from functools import wraps

from flask_app import server as flask_server

HOTFIX_DEPLOY_MARKER = "root-mysql-retry-compat-2026-08-29-v1"


def _database_is_locked_error(exc):
    checker = getattr(flask_server, "database_is_locked_error", None)
    if callable(checker):
        try:
            return bool(checker(exc))
        except Exception:
            pass
    message = str(exc or "").strip().lower()
    return (
        "database is locked" in message
        or "database table is locked" in message
        or "database is busy" in message
        or "lock wait timeout" in message
        or "deadlock found" in message
    )


def _database_connection_recoverable_error(exc):
    checker = getattr(flask_server, "mysql_is_connection_recoverable_error", None)
    if callable(checker):
        try:
            return bool(checker(exc))
        except Exception:
            pass
    message = str(exc or "").strip().lower()
    return (
        "mysql server has gone away" in message
        or "lost connection" in message
        or "connection reset by peer" in message
    )
def _run_with_database_lock_retries(
    operation,
    *,
    operation_name="database operation",
    attempts=6,
    initial_delay_s=0.5,
    retry_connection_errors=False,
):
    last_exc = None
    total_attempts = max(1, int(attempts or 1))
    base_delay = max(0.0, float(initial_delay_s or 0.0))
    for attempt in range(total_attempts):
        try:
            return operation()
        except Exception as exc:
            lock_error = _database_is_locked_error(exc)
            connection_error = bool(retry_connection_errors) and _database_connection_recoverable_error(exc)
            if (not lock_error and not connection_error) or attempt >= total_attempts - 1:
                raise
            last_exc = exc
            # Passenger replaces the backend helper with this compatibility
            # wrapper, so preserve its exponential backoff and jitter here.
            delay_s = base_delay * (2 ** attempt)
            if delay_s > 0:
                delay_s += random.uniform(0.0, min(0.25, delay_s * 0.25))
            flask_server.logger.warning(
                "Root hotfix retrying %s after recoverable database %s (%s/%s): %s",
                operation_name,
                "lock/deadlock" if lock_error else "connection error",
                attempt + 1,
                total_attempts,
                exc,
            )
            if delay_s > 0:
                time.sleep(delay_s)
    if last_exc is not None:
        raise last_exc


def _hotfix_purge_device_app_settings(cursor, device_id):
    normalized_device_id = flask_server.normalize_device_id(device_id)

    def delete_app_settings():
        deleted_rows = 0
        key_identifier = flask_server.quote_mysql_identifier("key")
        for setting_key in flask_server.device_scoped_app_setting_keys(device_id):
            deleted_rows += int(
                cursor.execute(
                    f"DELETE FROM app_settings WHERE {key_identifier} = ?",
                    (setting_key,),
                ).rowcount
                or 0
            )
        if normalized_device_id:
            deleted_rows += int(
                cursor.execute(
                    f"DELETE FROM app_settings WHERE {key_identifier} LIKE ?",
                    (f"{flask_server.ANALYTICS_LAST_VALID_SETTING_PREFIX}{normalized_device_id}:%",),
                ).rowcount
                or 0
            )
        return deleted_rows

    return _run_with_database_lock_retries(
        delete_app_settings,
        operation_name=f"purge app settings for {normalized_device_id or device_id}",
    )


def _hotfix_purge_device_data(device_id, remember_deleted_device=False):
    normalized_device_id = flask_server.normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")

    def execute_purge():
        deleted_counts = {}
        if remember_deleted_device:
            with flask_server.get_db() as db:
                flask_server.add_deleted_device_marker(db.cursor(), normalized_device_id)
        with flask_server.get_db() as db:
            cursor = db.cursor()
            for table_name in flask_server.list_database_table_names(cursor):
                if remember_deleted_device and table_name == "ignored_devices":
                    continue
                deleted_rows = flask_server.purge_device_table_rows(cursor, table_name, normalized_device_id)
                if deleted_rows is not None:
                    deleted_counts[table_name] = deleted_rows

            app_settings_count = flask_server.purge_device_app_settings(cursor, normalized_device_id)
            if app_settings_count or "app_settings" not in deleted_counts:
                deleted_counts["app_settings"] = app_settings_count

        with flask_server.get_db() as db:
            cursor = db.cursor()
            for table_name in flask_server.list_database_table_names(cursor):
                if remember_deleted_device and table_name == "ignored_devices":
                    continue
                deleted_rows = flask_server.purge_device_table_rows(cursor, table_name, normalized_device_id)
                if deleted_rows is not None:
                    deleted_counts[table_name] = int(deleted_counts.get(table_name) or 0) + deleted_rows

            app_settings_count = flask_server.purge_device_app_settings(cursor, normalized_device_id)
            if app_settings_count:
                deleted_counts["app_settings"] = int(deleted_counts.get("app_settings") or 0) + app_settings_count

        if remember_deleted_device:
            with flask_server.get_db() as db:
                marker_count = flask_server.add_deleted_device_marker(db.cursor(), normalized_device_id)
                deleted_counts["ignored_devices"] = int(deleted_counts.get("ignored_devices") or 0) + marker_count

        flask_server.forget_registered_device_touch(normalized_device_id)
        flask_server.clear_runtime_caches(normalized_device_id)
        return deleted_counts

    return _run_with_database_lock_retries(
        execute_purge,
        operation_name=f"root hotfix purge device data for {normalized_device_id}",
    )


def _hotfix_delete_known_device(device_id):
    normalized_device_id = flask_server.normalize_device_id(device_id)
    if not normalized_device_id:
        raise ValueError("device_id is required")
    return flask_server.purge_device_data(normalized_device_id, remember_deleted_device=True)


def _patch_health_route():
    original_health = flask_server.app.view_functions.get("health")
    if not callable(original_health):
        return

    @wraps(original_health)
    def wrapped_health(*args, **kwargs):
        payload = original_health(*args, **kwargs)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("wrapper_hotfix_marker", HOTFIX_DEPLOY_MARKER)
        return payload

    flask_server.app.view_functions["health"] = wrapped_health
    flask_server.health = wrapped_health


flask_server.run_with_database_lock_retries = _run_with_database_lock_retries
flask_server.purge_device_app_settings = _hotfix_purge_device_app_settings
flask_server.purge_device_data = _hotfix_purge_device_data
flask_server.delete_known_device = _hotfix_delete_known_device
_patch_health_route()

flask_server.logger.info("SaleWell root hotfix active: %s", HOTFIX_DEPLOY_MARKER)

app = flask_server.app

# Keep the root compatibility module and the real Flask module in sync.
sys.modules[__name__] = flask_server
