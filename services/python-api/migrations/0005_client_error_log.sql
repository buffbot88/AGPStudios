-- Migration 0005: client error log.
--
-- Uncaught browser failures (window.onerror, unhandledrejection, and the render
-- crashes reported by the error boundary) are POSTed to
-- /api/telemetry/client-error and stored here, so production failures are
-- visible after the fact. Read them through GET /api/admin/client-errors.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS.
--
-- The API enforces retention opportunistically (rows older than 30 days are
-- deleted at most once an hour per process), so no cron job is needed.
--
-- Notes:
-- - `message` and `detail` are truncated to these widths by the API; the table
--   is log data, never something to trust as fact or join on.
-- - `path` is the page path only: the API strips the query string, and no IP
--   address is ever stored.

CREATE TABLE IF NOT EXISTS client_error_log (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  source VARCHAR(40) NOT NULL,             -- window.onerror | unhandledrejection | boundary | shape-drift
  message VARCHAR(400) NOT NULL,
  detail VARCHAR(400) NOT NULL DEFAULT '', -- trace line or component stack origin
  path VARCHAR(200) NOT NULL DEFAULT '',   -- page path, query stripped
  count INT NOT NULL DEFAULT 1,            -- occurrences in the reported burst
  occurrences TEXT NOT NULL,               -- JSON array of the last few detail lines
  user_id VARCHAR(64) NOT NULL DEFAULT '', -- attached server-side when signed in
  user_agent VARCHAR(200) NOT NULL DEFAULT '',
  created_at BIGINT UNSIGNED NOT NULL,
  INDEX idx_client_error_created (created_at),
  INDEX idx_client_error_message (message(80), created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
