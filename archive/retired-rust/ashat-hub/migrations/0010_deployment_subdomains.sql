ALTER TABLE galileo_deployments
    ADD COLUMN IF NOT EXISTS subdomain VARCHAR(63) NULL,
    ADD UNIQUE KEY galileo_deployments_subdomain (subdomain);
