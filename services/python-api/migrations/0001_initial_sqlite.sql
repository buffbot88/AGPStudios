CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  email TEXT NOT NULL COLLATE NOCASE UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT DEFAULT '',
  tag_name TEXT,
  discord_tag TEXT,
  location TEXT,
  interests TEXT,
  github_id TEXT,
  github_login TEXT,
  github_access_token TEXT,
  github_refresh_token TEXT,
  github_token_expires_at TEXT,
  github_app_installation_id INTEGER,
  role TEXT NOT NULL DEFAULT 'Member',
  is_active INTEGER NOT NULL DEFAULT 1,
  email_verified_at TEXT,
  banned_at INTEGER,
  diamonds INTEGER NOT NULL DEFAULT 0,
  acoins_balance INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_login_at TEXT
);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  ip TEXT,
  user_agent TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

CREATE TABLE rust_sessions (
  session_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  csrf_hash TEXT NOT NULL,
  ip TEXT,
  user_agent TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at TEXT NOT NULL
);
CREATE INDEX idx_rust_sessions_user ON rust_sessions(user_id);
CREATE INDEX idx_rust_sessions_expiry ON rust_sessions(expires_at);

CREATE TABLE email_verifications (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_email_verifications_user ON email_verifications(user_id);

CREATE TABLE support_tickets (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  priority TEXT NOT NULL DEFAULT 'normal',
  category TEXT NOT NULL DEFAULT 'other',
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_support_tickets_user ON support_tickets(user_id, updated_at);

CREATE TABLE support_ticket_replies (
  id TEXT PRIMARY KEY,
  ticket_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  message TEXT NOT NULL,
  is_staff INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_support_replies_ticket ON support_ticket_replies(ticket_id, created_at);
CREATE INDEX idx_support_replies_user ON support_ticket_replies(user_id);

CREATE TABLE admin_audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_id TEXT NOT NULL,
  actor_name TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_admin_audit_created ON admin_audit_events(created_at, id);

CREATE TABLE games (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'in-development',
  tags TEXT NOT NULL DEFAULT '[]',
  url TEXT NOT NULL DEFAULT '',
  poster TEXT NOT NULL DEFAULT '',
  featured INTEGER NOT NULL DEFAULT 0,
  sort_order INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE studio_news (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  url TEXT NOT NULL DEFAULT '',
  published_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE user_moderation_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  action TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  actor_name TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_modhist_user ON user_moderation_history(user_id, created_at);

CREATE TABLE client_error_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  message TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  path TEXT NOT NULL DEFAULT '',
  count INTEGER NOT NULL DEFAULT 1,
  occurrences TEXT NOT NULL DEFAULT '[]',
  user_id TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_client_error_created ON client_error_log(created_at);
CREATE INDEX idx_client_error_message ON client_error_log(message, created_at);
CREATE TABLE client_error_triage (
  error_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'new',
  triaged_at INTEGER NOT NULL,
  triaged_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_client_error_triage_status ON client_error_triage(status, triaged_at);
CREATE TABLE client_error_mutes (
  message_hash TEXT PRIMARY KEY,
  message TEXT NOT NULL DEFAULT '',
  muted_at INTEGER NOT NULL,
  muted_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE world_of_ra_entitlements (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  granted_at INTEGER NOT NULL,
  granted_by TEXT NOT NULL
);

CREATE TABLE acoin_payment_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  paypal_email TEXT NOT NULL DEFAULT '',
  paypal_client_id TEXT NOT NULL DEFAULT '',
  paypal_client_secret TEXT NOT NULL,
  environment TEXT NOT NULL DEFAULT 'sandbox' CHECK (environment IN ('sandbox','live')),
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE acoin_orders (
  id TEXT PRIMARY KEY,
  paypal_order_id TEXT NOT NULL UNIQUE,
  paypal_capture_id TEXT UNIQUE,
  user_id TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('sandbox','live')),
  amount_cents INTEGER NOT NULL,
  acoins INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','completed')),
  created_at INTEGER NOT NULL,
  completed_at INTEGER
);
CREATE INDEX idx_acoin_orders_user ON acoin_orders(user_id, created_at);

INSERT OR IGNORE INTO games (id,slug,title,summary,status,tags,url,poster,featured,sort_order,created_at,updated_at) VALUES
 ('paws01','paws-and-parcels','Paws & Parcels','Deliver small moments. Explore a growing world.','live','["Cozy","Exploration","Social"]','https://pawsandparcels.agpstudios.org','/paws-parcels-poster.svg',1,1,CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER)),
 ('horizon01','world-of-ra-horizon','The World of Ra - Horizon - Season 1','A new world is taking shape.','in-development','["Exploration"]','','/world-of-ra-poster.svg',0,2,CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER)),
 ('more01','more-titles','More in Development','New worlds, new experiences, and more to come.','coming-soon','["Variety"]','#news','/more-in-development-poster.svg',0,3,CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER));

INSERT OR IGNORE INTO studio_news (id,title,summary,url,published_at,created_at)
VALUES ('news01','A New Chapter Ahead','We''re working on new worlds, new ideas, and more ways to play together.','#news',CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER));
