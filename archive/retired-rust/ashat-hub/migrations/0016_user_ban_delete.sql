-- Distinguish bans from ordinary disables, and support hard user deletion.
-- banned_at records when the user was banned; NULL means not banned.
ALTER TABLE users ADD COLUMN banned_at BIGINT NULL AFTER is_active;
