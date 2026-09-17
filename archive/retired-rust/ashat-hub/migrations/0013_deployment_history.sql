-- Deployment history timeline. The galileo_deployments table is upserted
-- (one row per project, current state); this table appends a row for every
-- publish / undeploy / rollback so the UI can render a Vercel-style timeline.
CREATE TABLE IF NOT EXISTS galileo_deployment_history (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id VARCHAR(191) NOT NULL,
    project_id VARCHAR(191) NOT NULL,
    deployment_id VARCHAR(64) NOT NULL,
    url VARCHAR(512) NOT NULL DEFAULT '',
    subdomain VARCHAR(63) NULL,
    status VARCHAR(16) NOT NULL,
    file_count INT UNSIGNED NOT NULL DEFAULT 0,
    message VARCHAR(255) NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL,
    PRIMARY KEY (id),
    INDEX galileo_deployment_history_user (user_id, created_at),
    INDEX galileo_deployment_history_project (user_id, project_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
