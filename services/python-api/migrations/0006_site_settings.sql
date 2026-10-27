-- 0006: site settings. Key-value overrides for the Website admin tab:
-- homepage content (featured video, footer), registration controls
-- (challenge difficulty, per-IP budget/window), with code-side defaults
-- in proxy.py so an empty table means stock behavior.
CREATE TABLE site_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);
