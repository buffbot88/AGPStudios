-- 0005: account transaction ledger. One row per money/ACoins movement the
-- member can see on #account > Purchase History: ACoins purchases (PayPal),
-- ACoins spending, and per-game purchases (some games do not use ACoins).
-- Newest-first reads serve the panel; the receipt JSON carries the
-- provider references shown in the receipt view.
CREATE TABLE account_transactions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('acoins_purchase','acoins_spend','game_purchase')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER,
  acoins_delta INTEGER NOT NULL DEFAULT 0,
  game_slug TEXT,
  status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('pending','completed','failed')),
  created_at INTEGER NOT NULL,
  receipt TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_account_transactions_user ON account_transactions(user_id, created_at DESC, id DESC);

-- Backfill completed ACoins orders so existing purchases already appear.
-- Receipt is built with string concatenation (no JSON1 dependency).
INSERT OR IGNORE INTO account_transactions
  (id, user_id, kind, title, detail, amount_cents, acoins_delta, game_slug, status, created_at, receipt)
SELECT 'txn-' || id, user_id, 'acoins_purchase',
  acoins || ' ACoins purchase',
  'ACoins package via PayPal (' || environment || ')',
  amount_cents, acoins, NULL, 'completed',
  COALESCE(completed_at, created_at),
  '{"paypal_order_id":"' || paypal_order_id || '","paypal_capture_id":"' || COALESCE(paypal_capture_id, '') || '"}'
FROM acoin_orders WHERE status = 'completed';
