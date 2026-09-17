-- Vesper model catalog compatibility and local seed registry.
ALTER TABLE vesper_models
    ADD COLUMN checksum VARCHAR(128) NOT NULL DEFAULT '' AFTER filename,
    ADD COLUMN origin_url VARCHAR(512) NOT NULL DEFAULT '' AFTER download_url;

CREATE TABLE IF NOT EXISTS vesper_model_seeds (
    id              VARCHAR(36)   NOT NULL PRIMARY KEY,
    model_id        VARCHAR(36)   NOT NULL,
    user_id         VARCHAR(36)   NOT NULL,
    origin_url      VARCHAR(512)  NOT NULL,
    last_seen       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_vesper_model_seed (model_id, user_id),
    INDEX idx_vesper_model_seeds_model (model_id),
    CONSTRAINT fk_vesper_model_seeds_model
        FOREIGN KEY (model_id) REFERENCES vesper_models(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
