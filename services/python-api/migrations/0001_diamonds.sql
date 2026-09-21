-- Migration 0001: add the diamonds balance column to users.
--
-- Idempotent: MySQL has no `ADD COLUMN IF NOT EXISTS`, so the ALTER is built
-- and executed only when information_schema shows the column is missing. The
-- check runs in the `users` table's own database (DATABASE()), so it is correct
-- regardless of the schema name. Re-running is a no-op that executes `SELECT 1`.

SET @add_diamonds := (
  SELECT IF(COUNT(*) = 0,
            'ALTER TABLE users ADD COLUMN diamonds BIGINT UNSIGNED NOT NULL DEFAULT 0',
            'SELECT 1')
  FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'users'
    AND column_name = 'diamonds'
);

PREPARE add_diamonds_stmt FROM @add_diamonds;
EXECUTE add_diamonds_stmt;
DEALLOCATE PREPARE add_diamonds_stmt;
