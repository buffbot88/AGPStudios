-- Email verification is attached to the existing legacy users table.
-- MariaDB's IF NOT EXISTS keeps this migration safe on older Alpha installs.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified_at DATETIME NULL;

CREATE TABLE IF NOT EXISTS password_resets (
    id CHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    used TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_password_resets_user (user_id),
    INDEX idx_password_resets_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
