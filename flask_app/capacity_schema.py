from __future__ import annotations


CAPACITY_SCHEMA_VERSION = "capacity-phase-1-v1"
CAPACITY_TABLES = (
    "device_latest_state",
    "tank_telemetry_history",
    "device_metadata",
    "telemetry_sampling_state",
    "alert_occurrences",
    "background_jobs",
    "api_metrics_minute",
    "tank_telemetry_hourly",
    "tank_telemetry_daily",
    "capacity_runtime_status",
)


CAPACITY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_migrations(
        version VARCHAR(128) PRIMARY KEY,
        applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_latest_state(
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        boot_id VARCHAR(64),
        sequence_number BIGINT,
        level REAL,
        lower_tank_level REAL,
        motor VARCHAR(16),
        mode VARCHAR(32),
        sensor VARCHAR(32),
        alert_flags BIGINT NOT NULL DEFAULT 0,
        firmware_version VARCHAR(64),
        device_reported_at DATETIME,
        received_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        state_json LONGTEXT,
        PRIMARY KEY(device_id, device_source),
        KEY idx_latest_state_received(received_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tank_telemetry_history(
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        boot_id VARCHAR(64),
        sequence_number BIGINT,
        level REAL,
        lower_tank_level REAL,
        motor VARCHAR(16),
        mode VARCHAR(32),
        sensor VARCHAR(32),
        alert_flags BIGINT NOT NULL DEFAULT 0,
        sample_reason VARCHAR(64),
        recorded_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_history_sequence(device_id, device_source, boot_id, sequence_number),
        KEY idx_history_device_time(device_id, device_source, recorded_at),
        KEY idx_history_recorded(recorded_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS device_metadata(
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        architecture_id VARCHAR(64),
        node_role VARCHAR(64),
        device_type VARCHAR(64),
        firmware_version VARCHAR(64),
        tank_height_cm REAL,
        tank_capacity_liters REAL,
        lower_tank_height_cm REAL,
        lower_tank_capacity_liters REAL,
        configuration_version BIGINT,
        metadata_json LONGTEXT,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(device_id, device_source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telemetry_sampling_state(
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        last_history_at DATETIME,
        last_level REAL,
        last_lower_tank_level REAL,
        last_motor VARCHAR(16),
        last_mode VARCHAR(32),
        last_sensor VARCHAR(32),
        last_alert_flags BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(device_id, device_source)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alert_occurrences(
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        incident_key VARCHAR(128) NOT NULL,
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        alert_kind VARCHAR(64) NOT NULL,
        severity VARCHAR(32) NOT NULL,
        message TEXT,
        details_json LONGTEXT,
        started_at DATETIME NOT NULL,
        resolved_at DATETIME,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uq_alert_occurrence_key(incident_key),
        KEY idx_alert_occurrence_device_time(device_id, device_source, started_at),
        KEY idx_alert_occurrence_open(resolved_at, alert_kind)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS background_jobs(
        id BIGINT PRIMARY KEY AUTO_INCREMENT,
        job_kind VARCHAR(64) NOT NULL,
        device_id VARCHAR(255),
        payload_json LONGTEXT,
        status VARCHAR(24) NOT NULL DEFAULT 'queued',
        attempt_count INTEGER NOT NULL DEFAULT 0,
        available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        locked_at DATETIME,
        completed_at DATETIME,
        last_error TEXT,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_background_jobs_claim(status, available_at, id),
        KEY idx_background_jobs_device(device_id, created_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS api_metrics_minute(
        minute_at DATETIME NOT NULL,
        route VARCHAR(160) NOT NULL,
        request_count BIGINT NOT NULL DEFAULT 0,
        error_count BIGINT NOT NULL DEFAULT 0,
        total_duration_ms BIGINT NOT NULL DEFAULT 0,
        maximum_duration_ms BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(minute_at, route)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tank_telemetry_hourly(
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        bucket_at DATETIME NOT NULL,
        sample_count INTEGER NOT NULL DEFAULT 0,
        minimum_level REAL,
        maximum_level REAL,
        average_level REAL,
        minimum_lower_level REAL,
        maximum_lower_level REAL,
        average_lower_level REAL,
        pump_runtime_seconds BIGINT NOT NULL DEFAULT 0,
        pump_cycle_count INTEGER NOT NULL DEFAULT 0,
        alert_count INTEGER NOT NULL DEFAULT 0,
        missing_data_seconds BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(device_id, device_source, bucket_at),
        KEY idx_hourly_bucket(bucket_at)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tank_telemetry_daily(
        device_id VARCHAR(255) NOT NULL,
        device_source VARCHAR(32) NOT NULL DEFAULT 'real',
        bucket_date DATE NOT NULL,
        sample_count INTEGER NOT NULL DEFAULT 0,
        minimum_level REAL,
        maximum_level REAL,
        average_level REAL,
        minimum_lower_level REAL,
        maximum_lower_level REAL,
        average_lower_level REAL,
        pump_runtime_seconds BIGINT NOT NULL DEFAULT 0,
        pump_cycle_count INTEGER NOT NULL DEFAULT 0,
        alert_count INTEGER NOT NULL DEFAULT 0,
        missing_data_seconds BIGINT NOT NULL DEFAULT 0,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(device_id, device_source, bucket_date),
        KEY idx_daily_bucket(bucket_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capacity_runtime_status(
        status_key VARCHAR(96) PRIMARY KEY,
        status_value VARCHAR(32) NOT NULL,
        details_json LONGTEXT,
        checked_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        KEY idx_capacity_runtime_checked(checked_at)
    )
    """,
)


def ensure_capacity_schema(cursor):
    for statement in CAPACITY_SCHEMA_STATEMENTS:
        cursor.execute(statement)
    cursor.execute(
        """
        INSERT INTO schema_migrations(version, applied_at)
        VALUES (?, CURRENT_TIMESTAMP)
        ON CONFLICT(version) DO NOTHING
        """,
        (CAPACITY_SCHEMA_VERSION,),
    )
