-- Manual device purge script for MySQL/MariaDB.
-- Update only @device_id before running in cPanel/phpMyAdmin.

SET @device_id = 'swt-000-000-010-001';

START TRANSACTION;

DELETE FROM relay_queue
WHERE payload LIKE CONCAT('%"device_id":"', @device_id, '"%')
   OR payload LIKE CONCAT('%"device_id": "', @device_id, '"%');

DELETE FROM ops_audit_log
WHERE device_id = @device_id
   OR (target_type = 'device' AND target_id = @device_id);

DELETE FROM device_events
WHERE device_id = @device_id;

DELETE FROM ops_alerts
WHERE device_id = @device_id;

DELETE FROM firmware_artifacts
WHERE target_device = @device_id;

DELETE FROM device_mobile_action_queue
WHERE target_device = @device_id;

DELETE FROM device_command_queue
WHERE target_device = @device_id;

DELETE FROM tank_data
WHERE device_id = @device_id;

DELETE FROM customer_password_reset_tokens
WHERE device_id = @device_id;

DELETE FROM device_service_configs
WHERE device_id = @device_id;

DELETE FROM device_auth_keys
WHERE device_id = @device_id;

DELETE FROM registered_devices
WHERE device_id = @device_id;

DELETE FROM customer_accounts
WHERE device_id = @device_id;

DELETE FROM app_settings
WHERE `key` IN (
    CONCAT('device_simulator_state:', @device_id),
    CONCAT('device_automation_settings:', @device_id),
    CONCAT('device_local_web_password:', @device_id),
    CONCAT('mobile_session_epoch:', @device_id),
    CONCAT('active_session:android:customer:', @device_id, ':', @device_id),
    CONCAT('active_session:dashboard:customer:', @device_id, ':', @device_id)
)
OR `key` LIKE CONCAT('analytics:last-valid:', @device_id, ':%');

INSERT INTO ignored_devices (device_id, note, created_at, updated_at)
VALUES (@device_id, 'manual_purge', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
ON DUPLICATE KEY UPDATE
    note = VALUES(note),
    updated_at = CURRENT_TIMESTAMP;

COMMIT;
