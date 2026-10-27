-- 0007: referrals. Every account gets one permanent referral code; redemptions
-- are recorded so each link pays out at most referral.uses_per_day bonuses per
-- rolling 24h, each new account redeems at most one code, and registrations
-- carry their source IP so register.max_accounts_per_ip caps accounts per IP.
-- The account_transactions CHECK gains the referral_bonus kind, which needs a
-- table rebuild (SQLite cannot alter a CHECK in place).
ALTER TABLE users ADD COLUMN created_ip TEXT NOT NULL DEFAULT '';
CREATE INDEX idx_users_created_ip ON users(created_ip);

CREATE TABLE referral_codes (
  user_id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL
);

-- Permanent links for accounts that predate referrals.
INSERT OR IGNORE INTO referral_codes (user_id, code, created_at)
SELECT id, lower(hex(randomblob(6))), CAST(strftime('%s','now') AS INTEGER) FROM users;

CREATE TABLE referral_uses (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL,
  referrer_user_id TEXT NOT NULL,
  referred_user_id TEXT NOT NULL UNIQUE,
  bonus_acoins INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_referral_uses_code_time ON referral_uses(code, created_at);

CREATE TABLE account_transactions_new (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('acoins_purchase','acoins_spend','game_purchase','referral_bonus')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER,
  acoins_delta INTEGER NOT NULL DEFAULT 0,
  game_slug TEXT,
  status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('pending','completed','failed')),
  created_at INTEGER NOT NULL,
  receipt TEXT NOT NULL DEFAULT '{}'
);
INSERT INTO account_transactions_new
  (id, user_id, kind, title, detail, amount_cents, acoins_delta, game_slug, status, created_at, receipt)
SELECT id, user_id, kind, title, detail, amount_cents, acoins_delta, game_slug, status, created_at, receipt
FROM account_transactions;
DROP TABLE account_transactions;
ALTER TABLE account_transactions_new RENAME TO account_transactions;
CREATE INDEX idx_account_transactions_user ON account_transactions(user_id, created_at DESC, id DESC);
