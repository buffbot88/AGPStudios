-- Google OAuth account linkage table
CREATE TABLE IF NOT EXISTS google_accounts (
    id          VARCHAR(36)   NOT NULL PRIMARY KEY,
    user_id     VARCHAR(36)   NOT NULL,
    google_id   VARCHAR(64)   NOT NULL UNIQUE,
    email       VARCHAR(255)  NOT NULL,
    name        VARCHAR(200)  NOT NULL DEFAULT '',
    avatar_url  VARCHAR(512)  NOT NULL DEFAULT '',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_google_accounts_user_id (user_id),
    INDEX idx_google_accounts_email (email),
    CONSTRAINT fk_google_accounts_user
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
