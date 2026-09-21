-- Migration 0004: user moderation history.
--
-- Dedicated table recording every admin moderation action on a user so the
-- admin CP can render a per-user timeline. Events are appended by the API on
-- role/status/ban changes (see proxy.py admin_post) and read through
-- GET /api/admin/users/:id/moderation.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS. Fresh and existing databases converge.

CREATE TABLE IF NOT EXISTS user_moderation_history (
  id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  user_id VARCHAR(64) NOT NULL,
  action VARCHAR(40) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  actor_name VARCHAR(80) NOT NULL,
  detail VARCHAR(400) NOT NULL DEFAULT '',
  created_at BIGINT UNSIGNED NOT NULL,
  INDEX idx_modhist_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Notes:
-- - `action` mirrors the admin_audit_events vocabulary for these flows:
--   'user.role.updated', 'user.status.updated', 'user.banned', 'user.unbanned'.
-- - `detail` carries a short human-readable summary (e.g. 'role: Pro',
--   'status: disabled', 'banned'). Kept under 400 chars to match
--   admin_audit_events.detail.
-- - No FK on user_id: users are deleted by the existing purge paths, and this
--   table intentionally retains history after account deletion.
-- - created_at is unix seconds (BIGINT UNSIGNED), consistent with
--   games/studio_news/migrations 0001-0003 timestamps.
