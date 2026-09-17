-- Vesper platform: AI/ML models for client-side inference
CREATE TABLE IF NOT EXISTS vesper_models (
    id              VARCHAR(36)   NOT NULL PRIMARY KEY,
    name            VARCHAR(128)  NOT NULL,
    slug            VARCHAR(128)  NOT NULL UNIQUE,
    description     TEXT          NOT NULL,
    model_type      VARCHAR(64)   NOT NULL DEFAULT 'llm' COMMENT 'llm, embedding, vision, tts, stt',
    version         VARCHAR(32)   NOT NULL DEFAULT '1.0.0',
    filename        VARCHAR(255)  NOT NULL,
    signature       VARCHAR(255)  NOT NULL DEFAULT '',
    file_size       BIGINT        NOT NULL DEFAULT 0,
    download_url    VARCHAR(512)  NOT NULL DEFAULT '' COMMENT 'Presigned URL for large files, empty = serve from local',
    platform_rid    VARCHAR(64)   NOT NULL DEFAULT '' COMMENT 'Tauri RID if platform-specific, empty = universal',
    min_ram_mb      INT           NOT NULL DEFAULT 0 COMMENT 'Minimum RAM in MB recommended for this model',
    quantization    VARCHAR(32)   NOT NULL DEFAULT '' COMMENT 'q4_0, q4_k_m, q8_0, f16, etc.',
    is_active       TINYINT(1)    NOT NULL DEFAULT 1,
    created_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_vesper_models_type (model_type),
    INDEX idx_vesper_models_slug (slug),
    INDEX idx_vesper_models_active (is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
