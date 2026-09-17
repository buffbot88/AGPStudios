ALTER TABLE galileo_jobs ADD COLUMN checkpoint_id VARCHAR(40) NULL AFTER project_id;
CREATE INDEX idx_galileo_jobs_checkpoint ON galileo_jobs (checkpoint_id);
