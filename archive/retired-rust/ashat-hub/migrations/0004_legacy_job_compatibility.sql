-- Existing Alpha installations created galileo_jobs before Rust approval
-- payloads were introduced. MariaDB's conditional ALTER keeps this safe for
-- both those installations and clean databases where 0001 already includes
-- the column.
ALTER TABLE galileo_jobs
    ADD COLUMN IF NOT EXISTS approval_payload LONGTEXT NULL;
