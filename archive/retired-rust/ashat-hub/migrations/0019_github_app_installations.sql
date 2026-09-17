ALTER TABLE users
    ADD COLUMN github_app_installation_id BIGINT NULL,
    ADD INDEX idx_users_github_app_installation (github_app_installation_id);
