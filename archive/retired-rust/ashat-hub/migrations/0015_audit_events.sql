-- Audit trail for administrative mutations. Every role change, status toggle,
-- deployment removal, and rollback should INSERT a row here.
CREATE TABLE IF NOT EXISTS admin_audit_events (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    actor_id VARCHAR(191) NOT NULL,
    actor_name VARCHAR(191) NOT NULL DEFAULT '',
    action VARCHAR(64) NOT NULL,
    target_type VARCHAR(32) NOT NULL DEFAULT '',
    target_id VARCHAR(191) NOT NULL DEFAULT '',
    detail TEXT NULL,
    created_at BIGINT NOT NULL,
    PRIMARY KEY (id),
    INDEX admin_audit_actor (actor_id, created_at),
    INDEX admin_audit_action (action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
