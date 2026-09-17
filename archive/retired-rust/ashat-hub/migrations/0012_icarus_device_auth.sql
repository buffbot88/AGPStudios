CREATE TABLE IF NOT EXISTS icarus_devices (
    device_code   VARCHAR(64) PRIMARY KEY,
    user_code     VARCHAR(16) UNIQUE NOT NULL,
    status        VARCHAR(16) NOT NULL DEFAULT 'pending',
    session_token VARCHAR(128),
    csrf_token    VARCHAR(128),
    user_id       VARCHAR(36),
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at    TIMESTAMP NOT NULL
);

CREATE INDEX idx_icarus_devices_user_code ON icarus_devices(user_code);
CREATE INDEX idx_icarus_devices_status ON icarus_devices(status);
