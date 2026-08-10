CREATE TABLE IF NOT EXISTS device_events (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    event_key VARCHAR(191) NOT NULL UNIQUE,
    device_id VARCHAR(191),
    event_kind VARCHAR(96) NOT NULL,
    severity VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    details_json JSON,
    source_table VARCHAR(96),
    source_row_id VARCHAR(191),
    started_at DATETIME,
    ended_at DATETIME,
    duration_seconds INTEGER,
    event_at DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_device_events_device_event_at (device_id, event_at DESC, id DESC),
    INDEX idx_device_events_kind_event_at (event_kind, event_at DESC)
);
