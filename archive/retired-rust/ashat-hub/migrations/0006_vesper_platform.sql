-- Vesper platform: session tokens (Bearer-based, separate from hub cookie sessions)
CREATE TABLE IF NOT EXISTS vesper_sessions (
    id              VARCHAR(36)   NOT NULL PRIMARY KEY,
    user_id         VARCHAR(36)   NOT NULL,
    token_hash      VARCHAR(64)   NOT NULL UNIQUE,
    ip              VARCHAR(64)   NOT NULL DEFAULT '',
    user_agent      VARCHAR(512)  NOT NULL DEFAULT '',
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at      DATETIME      NOT NULL,
    INDEX idx_vesper_sessions_user (user_id),
    INDEX idx_vesper_sessions_token (token_hash),
    INDEX idx_vesper_sessions_expiry (expires_at),
    CONSTRAINT fk_vesper_sessions_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Vesper platform: release versions
CREATE TABLE IF NOT EXISTS vesper_releases (
    id              VARCHAR(36)   NOT NULL PRIMARY KEY,
    version         VARCHAR(32)   NOT NULL,
    platform_rid    VARCHAR(64)   NOT NULL COMMENT 'Tauri RID: windows-x86_64, linux-x86_64, darwin-aarch64',
    pub_date        DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    notes           TEXT          NOT NULL,
    filename        VARCHAR(255)  NOT NULL,
    signature       VARCHAR(255)  NOT NULL DEFAULT '',
    file_size       BIGINT        NOT NULL DEFAULT 0,
    download_url    VARCHAR(512)  NOT NULL DEFAULT '' COMMENT 'Presigned URL for large files, empty = serve from local',
    is_latest       TINYINT(1)    NOT NULL DEFAULT 0,
    INDEX idx_vesper_releases_version (version),
    INDEX idx_vesper_releases_platform (platform_rid),
    INDEX idx_vesper_releases_latest (is_latest),
    UNIQUE KEY uk_vesper_releases_ver_plat (version, platform_rid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
