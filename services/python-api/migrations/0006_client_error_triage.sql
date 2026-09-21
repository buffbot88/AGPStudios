-- Migration 0006: triage state for the client error log.
--
-- The admin CP's Client errors panel used to be write-once: every row stayed
-- "new" forever, so a crash reported last week looked as urgent as one reported
-- a minute ago. These two tables record the operator's judgement, deliberately
-- kept out of client_error_log so the log itself stays exactly what visitors
-- reported (client data, never something to trust).
--
--   client_error_triage  row state, keyed by the reported row (client_error_log.id)
--   client_error_mutes   message-level mutes, keyed by SHA2(message, 256)
--
-- Why mutes are message-level: one row is one reported burst, so muting a single
-- row would do nothing about the next burst of the same crash. A mute is applied
-- when the list is read — the row's effective status becomes 'muted' — so it
-- covers rows that already exist and every report that arrives afterwards, and
-- it survives the log's 30-day retention pass. That is what makes muting a noisy
-- known-benign failure actually stick.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, so applying this twice (the CI job
-- does exactly that) converges rather than failing.
--
-- Notes:
-- - No foreign key to client_error_log: rows are pruned by retention, and the
--   API opportunistically deletes triage rows whose log row is gone (see
--   proxy.py prune_client_errors). Orphans would be harmless either way.
-- - `triaged_by` / `muted_by` hold the acting admin's user id, matching the
--   audit event written alongside each change.
-- - Effective status is computed, not stored: 'muted' when the message is
--   muted, otherwise the row's triage status, otherwise 'new'.

CREATE TABLE IF NOT EXISTS client_error_triage (
  error_id BIGINT UNSIGNED PRIMARY KEY,
  status ENUM('new','resolved') NOT NULL DEFAULT 'new',
  triaged_at BIGINT UNSIGNED NOT NULL,
  triaged_by VARCHAR(64) NOT NULL DEFAULT '',
  INDEX idx_client_error_triage_status (status, triaged_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS client_error_mutes (
  message_hash CHAR(64) PRIMARY KEY,          -- SHA2(message, 256), lower hex
  message VARCHAR(400) NOT NULL DEFAULT '',   -- the message, for the panel
  muted_at BIGINT UNSIGNED NOT NULL,
  muted_by VARCHAR(64) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
