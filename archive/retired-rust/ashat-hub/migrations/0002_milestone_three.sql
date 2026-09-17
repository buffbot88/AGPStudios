CREATE TABLE IF NOT EXISTS galileo_job_changes (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    job_id VARCHAR(32) NOT NULL,
    user_id VARCHAR(191) NOT NULL,
    project_id VARCHAR(191) NOT NULL,
    path VARCHAR(512) NOT NULL,
    operation VARCHAR(16) NOT NULL,
    before_exists TINYINT(1) NOT NULL DEFAULT 0,
    before_content LONGTEXT NULL,
    after_content LONGTEXT NULL,
    state VARCHAR(16) NOT NULL DEFAULT 'pending',
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
    UNIQUE KEY galileo_job_change_path (job_id, path),
    INDEX galileo_job_changes_user (user_id, project_id, state),
    INDEX galileo_job_changes_job (job_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS galileo_deployments (
    user_id VARCHAR(191) NOT NULL,
    project_id VARCHAR(191) NOT NULL,
    deployment_id VARCHAR(64) NOT NULL,
    url VARCHAR(512) NOT NULL,
    status VARCHAR(16) NOT NULL,
    file_count INT UNSIGNED NOT NULL DEFAULT 0,
    deployed_at BIGINT NOT NULL,
    PRIMARY KEY (user_id, project_id),
    INDEX galileo_deployments_status (status, deployed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
