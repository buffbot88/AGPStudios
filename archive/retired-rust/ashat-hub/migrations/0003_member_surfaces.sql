CREATE TABLE IF NOT EXISTS galileo_activity (
    id CHAR(36) NOT NULL PRIMARY KEY,
    user_id CHAR(36) NOT NULL,
    project_id VARCHAR(120) NULL,
    action VARCHAR(64) NOT NULL,
    metadata LONGTEXT NULL,
    request_id VARCHAR(128) NULL,
    created_at BIGINT NOT NULL,
    INDEX galileo_activity_user_time (user_id, created_at),
    INDEX galileo_activity_project_time (project_id, created_at),
    INDEX galileo_activity_action_time (action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS galileo_community_links (
    community_id CHAR(36) NOT NULL PRIMARY KEY,
    user_id CHAR(36) NOT NULL,
    project_id VARCHAR(120) NOT NULL,
    created_at BIGINT NOT NULL,
    UNIQUE KEY galileo_community_project (user_id, project_id),
    INDEX galileo_community_project_lookup (project_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
