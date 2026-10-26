-- 0004: per-user read state for notifications. A row marks one notification
-- ref as read; refs are `reply:<reply_id>` (staff replies on the user's own
-- tickets) and `mod:<event_id>` (entries in the user's moderation timeline).
-- Refs are opaque to the client except for marking them read.
CREATE TABLE notification_reads (
  user_id TEXT NOT NULL,
  ref TEXT NOT NULL,
  read_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, ref)
);
CREATE INDEX idx_notification_reads_user ON notification_reads(user_id);
