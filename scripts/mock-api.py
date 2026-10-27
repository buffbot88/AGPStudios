#!/usr/bin/env python3
"""Contract-faithful mock of the AGP Studios Python API for local dev + E2E testing.

A documented dev utility, NOT a reference implementation. It implements the
endpoints from docs/python-api-contracts.md that the portal consumes:

  auth/session, register (email verification disabled by default), login, logout,
  verify-email (+resend), account summary / PUT / DELETE, support tickets +
  replies, games, news, and the admin surface (summary, users incl. role /
  status / ban / delete, settings, database status, and repo-status.

Behavior notes:
- In-memory state only; everything is lost on restart. Pre-seeded admin:
  username `boss`, password `admin-pass-1`.
- Email verification is off by default; `--verification-required` enables it
  for tests that exercise the verification flow.
- Listens on 127.0.0.1:3110 by default (override with `PORT`); request shapes
  mirror services/python-api/proxy.py, including its quirks (POST
  /admin/users/ban accepts `{user_id, banned}`; /admin/users/status takes
  `{user_id, status}`; PUT /api/account requires username/email/display_name).
- Runtime controls are unauthenticated but loopback-only. `POST /__config`
  adds latency with `{"latency_ms": 250}`, returns an injected error with
  `{"error_status": 503, "error_paths": ["/api/news"]}`, makes it one-shot with
  `{"error_once": true}`, and serves deliberately wrong-shape payloads with
  `{"hostile": ["games"]}` (or start with `--hostile[=keys]`, see HOSTILE_SHAPES)
  so drift-resilience tests need no per-test stubs. `GET /__codes` returns the
  pending verification tokens — the portal has no token-entry screen, so that is
  how a test completes the emailed link. `POST /__reset` restores seed data,
  clears sessions, and returns controls to how the process was started, so
  `--verification-required` and `--hostile` survive it; `{"verification_required":
  false}` may be included in the reset body. `--account-cap=N` relaxes the
  per-IP account cap for harnesses that register many accounts from one
  loopback address (0 disables it); the register.max_accounts_per_ip setting
  (default 2) applies otherwise.

Run via `npm run mock-api` (alone) or `npm run dev:mock` (alongside vite) from
apps/ashat-hub-web. See the README's "Frontend with the mock API" section.
"""
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Dev-only controls. /__config and /__reset intentionally require no auth:
# this server binds to loopback and is never used as a production API.
# Controls the process was started with. `/__reset` restores these rather than
# the bare defaults, so a flag like --verification-required or --hostile survives a
# reset instead of silently switching the mock back to normal behavior.
STARTUP_CONTROLS = {}

DEFAULT_CONTROLS = {
    "verification_required": False,
    "hostile": [],
    "latency_ms": 0,
    "error_status": 0,
    "error_message": "Injected mock API failure",
    "error_paths": [],
    "error_once": False,
}
CONFIG = dict(DEFAULT_CONTROLS)

USERS = {
    # pre-seeded admin (password: admin-pass-1)
    "1": {"id": "1", "username": "boss", "email": "boss@agpstudios.org",
          "display_name": "Boss", "role": "Admin", "password": "admin-pass-1",
          "verified": True, "created_ip": "", "referral_code": secrets.token_hex(6),
          "acoins_balance": 0, "created_at": int(time.time())},
}
SESSIONS = {}          # session token -> user id
CSRF = {}              # session token -> csrf token
VERIFICATIONS = {}     # username -> verification code
ONLINE_VISITORS = {}   # visitor id -> last heartbeat
PAYMENT_SETTINGS = {"paypal_email": "", "paypal_client_id": "", "client_secret_configured": False, "environment": "sandbox", "encryption_key_configured": True}
# Mirrors proxy.py's site_settings table (migration 0006): DB overrides for
# the Website admin tab, with the same code-side defaults.
SITE_STRING_DEFAULTS = {
    "featured_video.embed_url": "https://www.youtube-nocookie.com/embed/ky8rKHSVgNw?rel=0",
    "featured_video.watch_url": "https://www.youtube.com/watch?v=ky8rKHSVgNw",
    "footer.tagline": "A Home For Playable Worlds.",
    "footer.discord_url": "https://discord.gg/nKPtUF4KHt",
    "footer.youtube_url": "https://www.youtube.com/@stressthismess",
}
SITE_INT_DEFAULTS = {
    "register.challenge_difficulty": (4, 1, 8),
    "register.budget": (5, 1, 100),
    "register.window_seconds": (600, 60, 3600),
    "referral.bonus_acoins": (100, 0, 10000),
    "referral.uses_per_day": (10, 1, 1000),
    "register.max_accounts_per_ip": (2, 1, 100),
}
SITE_STRING_LIMITS = {
    "featured_video.embed_url": 500,
    "featured_video.watch_url": 500,
    "footer.tagline": 160,
    "footer.discord_url": 500,
    "footer.youtube_url": 500,
}
SITE_SETTINGS = {}  # key -> override string
STUDIO_NEWS_SEED = [
    {"id": "n1", "title": "Paws & Parcels and Everenfell are live",
     "summary": "Both game servers are online. Sign in to manage your account and jump in.",
     "url": "#news", "published_at": int(time.time()), "created_at": int(time.time())},
]
STUDIO_NEWS = copy.deepcopy(STUDIO_NEWS_SEED)


def site_value(key):
    if key in SITE_SETTINGS:
        return SITE_SETTINGS[key]
    return SITE_STRING_DEFAULTS.get(key, "")


def site_int(key):
    default, lo, hi = SITE_INT_DEFAULTS[key]
    try:
        return max(lo, min(hi, int(str(SITE_SETTINGS.get(key, default)).strip() or default)))
    except (TypeError, ValueError):
        return default


def site_content_payload():
    return {
        "featured_video": {
            "title": "Featured video",
            "embed_url": site_value("featured_video.embed_url"),
            "watch_url": site_value("featured_video.watch_url"),
        },
        "footer": {
            "tagline": site_value("footer.tagline"),
            "discord_url": site_value("footer.discord_url"),
            "youtube_url": site_value("footer.youtube_url"),
        },
    }
TICKETS = []           # list of ticket dicts
GAMES = {"games": [
    {"slug": "paws-and-parcels", "title": "Paws & Parcels",
     "summary": "Deliver small moments. Explore a growing world.",
     "status": "live", "tags": ["Cozy", "Exploration", "Social"],
     "url": "https://pawsandparcels.agpstudios.org",
     "poster": "/paws-parcels-poster.svg", "featured": True},
    {"slug": "everenfell", "title": "Everenfell",
     "summary": "Season 1 is live. Sign in with AGP Studios to enter.",
     "status": "live", "tags": ["Adventure", "Season 1", "Multiplayer"],
     "url": "https://everenfell.agpstudios.org",
     "poster": "/everenfell-poster.svg", "featured": True},
]}
NEWS = {"news": [
    {"id": "n1", "title": "Paws & Parcels and Everenfell are live",
     "summary": "Both game servers are online. Sign in to manage your account and jump in.",
     "url": "#news", "published_at": int(time.time())},
    # Static sample of the GitHub-commit items the real API merges into
    # /api/news (recent commits across buffbot88's public repos). The mock
    # never calls out to api.github.com; this keeps the external shape
    # (github: id, commit URL) covered by contract tests deterministically.
    {"id": "github:mock0001", "title": "Trim homepage: no Play button, no games grid",
     "summary": "AGPStudios · Trim homepage: no Play button, no games grid",
     "url": "https://github.com/buffbot88/AGPStudios/commit/mock0001",
     "published_at": int(time.time()) - 3600},
]}

# Shape-only OIDC signing key for local contract tests. The mock never issues
# real tokens; production loads its RSA key from ASHAT_OIDC_PRIVATE_KEY_FILE.
MOCK_JWKS = {
    "keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": "mock-key-1",
        "n": "sXchc2FtcGxlLWp3a3MtbW9kdWx1cy1mb3ItY29udHJhY3QtdGVzdHM",
        "e": "AQAB",
    }]
}

INITIAL_USERS = copy.deepcopy(USERS)
INITIAL_TICKETS = copy.deepcopy(TICKETS)
MODERATION = []  # user_moderation_history mirror
NOTIFICATION_READS = {}  # user_id -> set of read notification refs
CLIENT_ERRORS = []  # client_error_log mirror
CLIENT_ERROR_TRIAGE = {}  # client_error_log.id -> {status, triaged_at, triaged_by} (0006)
CLIENT_ERROR_MUTES = {}   # message hash -> {message, muted_at, muted_by} (0006)
REFERRAL_USES = []  # referral_uses mirror: {id, code, referrer/referred ids, bonus, created_at}
ACCOUNT_LEDGER = []  # per-user account_transactions rows (referral bonuses)


def real_ip(handler):
    """Mirror proxy.py: leftmost X-Forwarded-For entry, else the direct peer."""
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    return handler.client_address[0]


def account_cap():
    """Effective per-IP account cap: --account-cap startup flag wins (test
    harnesses relax it), otherwise the register.max_accounts_per_ip setting."""
    flag = CONFIG.get("account_cap")
    if flag is not None:
        return flag
    return site_int("register.max_accounts_per_ip")


def mint_referral_code(user_id):
    for _ in range(5):
        code = secrets.token_hex(6)
        if all(u.get("referral_code") != code for u in USERS.values()):
            USERS[user_id]["referral_code"] = code
            return code
    raise ValueError("could not mint a referral code")


def redeem_referral(code, user_id):
    """Mirror proxy.py: credit both sides of a referral signup, or explain why not."""
    code = str(code or "").strip()
    if not code:
        return {"credited": False}
    owner = next((u for u in USERS.values() if u.get("referral_code") == code), None)
    if owner is None or owner["id"] == user_id:
        return {"credited": False, "reason": "unknown_referral_code"}
    if owner.get("role", "").lower() not in ("member", "admin"):
        return {"credited": False, "reason": "referral_code_inactive"}
    bonus = site_int("referral.bonus_acoins")
    per_day = site_int("referral.uses_per_day")
    if bonus <= 0:
        return {"credited": False, "reason": "referrals_disabled"}
    cutoff = time.time() - 86400
    if sum(1 for use in REFERRAL_USES if use["code"] == code and use["created_at"] > cutoff) >= per_day:
        return {"credited": False, "reason": "referral_limit_reached"}
    owner["acoins_balance"] = owner.get("acoins_balance", 0) + bonus
    USERS[user_id]["acoins_balance"] = USERS[user_id].get("acoins_balance", 0) + bonus
    use_id = secrets.token_hex(8)
    REFERRAL_USES.append({"id": use_id, "code": code, "referrer_user_id": owner["id"],
                           "referred_user_id": user_id, "bonus_acoins": bonus, "created_at": time.time()})
    detail = f"Referral bonus ({code})"
    ACCOUNT_LEDGER.append({"id": f"txn-ref-{use_id}-referrer", "user_id": owner["id"],
                            "kind": "referral_bonus", "title": f"{bonus} ACoins referral bonus",
                            "detail": detail, "amount_cents": None, "acoins_delta": bonus,
                            "game_slug": None, "status": "completed", "created_at": int(time.time()),
                            "receipt": {"referral_code": code}})
    ACCOUNT_LEDGER.append({"id": f"txn-ref-{use_id}-referred", "user_id": user_id,
                            "kind": "referral_bonus", "title": f"{bonus} ACoins referral bonus",
                            "detail": detail, "amount_cents": None, "acoins_delta": bonus,
                            "game_slug": None, "status": "completed", "created_at": int(time.time()),
                            "receipt": {"referral_code": code}})
    return {"credited": True, "bonus_acoins": bonus}


def message_hash(text):
    """Lower-hex SHA-256 of UTF-8 bytes, matching the API's SQLite function."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# Mirrors proxy.py's registration hardening so the 400/409/429 paths are
# testable: strict username/email/password filters, a proof-of-work
# human-verification challenge, and a per-IP registration budget.
REGISTER_BUDGET = 5           # keep in sync with proxy.py
REGISTER_WINDOW = 600.0
REGISTER_CHALLENGES = {}
REGISTER_CHALLENGE_TTL = 600.0
REGISTER_CHALLENGE_DIFFICULTY = 4
REGISTER_ATTEMPTS = {}
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,29}")
RESERVED_USERNAMES = {
    "admin", "administrator", "root", "system", "support", "help", "helpdesk",
    "moderator", "mod", "staff", "owner", "official", "agp", "agpstudios",
    "ashat", "security", "abuse", "webmaster", "postmaster", "hostmaster",
    "info", "mail", "email", "news", "games", "game", "server", "console",
    "api", "bot", "robot", "guest", "anonymous", "null", "undefined",
    "test", "tester", "demo", "member", "user", "account", "billing",
    "payment", "paypal", "discord", "youtube", "twitch", "twitter",
    "steam", "minecraft", "paws", "parcels", "everenfell",
}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamailblock.com",
    "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net",
    "tempmail.com", "temp-mail.org", "tempail.com", "throwawaymail.com",
    "yopmail.com", "yopmail.fr", "yopmail.net", "maildrop.cc",
    "getnada.com", "mohmal.com", "fakemail.net", "emailondeck.com",
    "trashmail.com", "trashmail.net", "spamgourmet.com", "bugmenot.com",
    "mintemail.com", "mytemp.email", "dispostable.com", "harakirimail.com",
    "inboxalias.com", "mailnesia.com", "easytrashmail.com", "dodgit.com",
}
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789",
    "qwerty", "qwerty123", "letmein", "welcome", "welcome1", "admin123",
    "agpstudios", "pawsandparcels", "everenfell", "monkeyking", "ashat",
    "iloveyou", "football", "baseball", "dragon", "sunshine", "master",
    "shadow", "abc12345", "passw0rd",
}


def valid_username(username):
    if not isinstance(username, str) or not username:
        return "a username is required"
    if not USERNAME_RE.fullmatch(username):
        return "usernames must start with a letter and use 3-30 letters, numbers, _ or -"
    if username.lower() in RESERVED_USERNAMES:
        return "that username is reserved"
    return None


def valid_email(email):
    if not isinstance(email, str) or not email:
        return "a valid email address is required"
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return "a valid email address is required"
    local, _, domain = email.rpartition("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return "a valid email address is required"
    labels = domain.split(".")
    if ".." in domain or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        return "a valid email address is required"
    if domain.lower() in DISPOSABLE_EMAIL_DOMAINS:
        return "disposable email addresses are not accepted"
    return None


def valid_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return "passwords must be 8-128 characters"
    if password.lower() in COMMON_PASSWORDS:
        return "that password is too common"
    return None


def register_proof_ok(form, difficulty=None):
    proof = form.get("proof")
    if not isinstance(proof, dict):
        return False
    challenge, nonce = proof.get("challenge"), proof.get("nonce")
    if not isinstance(challenge, str) or not isinstance(nonce, str):
        return False
    if not nonce or len(nonce) > 64:
        return False
    expires = REGISTER_CHALLENGES.pop(challenge, None)
    if expires is None or expires < time.time():
        return False
    if difficulty is None:
        difficulty = site_int("register.challenge_difficulty")
    return hashlib.sha256((challenge + nonce).encode()).hexdigest().startswith("0" * difficulty)


# Mirrors proxy.py's public telemetry limits so the 429/413 paths are testable.
CLIENT_ERROR_BUDGET = 20      # keep in sync with proxy.py
CLIENT_ERROR_WINDOW = 60.0
CLIENT_ERROR_MAX_BODY = 8192
CLIENT_ERROR_ATTEMPTS = {}

# Migration tracking mirror. The real runner (services/python-api/migrate.py)
# records applied files in a `schema_migrations` table; here the same state
# lives in memory so the admin CP's migration panel is exercisable without
# MySQL. Seeded as "everything except the newest file is applied", which is the
# realistic state right after a migration is committed to the repo.
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "services" / "python-api" / "migrations"

# Use the real runner's discovery/splitting instead of re-implementing it: a
# duplicated splitter would drift (a naive split on ';' miscounts statements
# that contain semicolons in string literals or comments).
sys.path.insert(0, str(MIGRATIONS_DIR.parent))
import migrate  # noqa: E402


def migration_files():
    """NNNN_description.sql files in order, discovered by the real runner."""
    return [{"filename": migration.name,
             "checksum": migration.checksum,
             "statements": len(migration.statements)}
            for migration in migrate.discover(MIGRATIONS_DIR)]


INITIAL_MIGRATIONS = {
    row["filename"]: dict(row, applied_at=int(time.time()), applied_by="seed")
    for row in migration_files()[:-1]
}
APPLIED_MIGRATIONS = copy.deepcopy(INITIAL_MIGRATIONS)

# Mirrors proxy.py's admin-mutation idempotency guard: (path, user_id) ->
# last submit time. Repeats inside the window return 409.
IDEMPOTENCY_WINDOW = 3.0  # seconds — keep in sync with proxy.py
RECENT_MUTATIONS = {}


def reset_state():
    """Restore deterministic seed data, sessions, and fault controls."""
    CONFIG.clear()
    CONFIG.update(DEFAULT_CONTROLS)
    CONFIG.update(STARTUP_CONTROLS)
    USERS.clear()
    USERS.update(copy.deepcopy(INITIAL_USERS))
    TICKETS.clear()
    TICKETS.extend(copy.deepcopy(INITIAL_TICKETS))
    RECENT_MUTATIONS.clear()
    MODERATION.clear()
    REFERRAL_USES.clear()
    ACCOUNT_LEDGER.clear()
    NOTIFICATION_READS.clear()
    CLIENT_ERRORS.clear()
    CLIENT_ERROR_TRIAGE.clear()
    CLIENT_ERROR_MUTES.clear()
    CLIENT_ERROR_ATTEMPTS.clear()
    APPLIED_MIGRATIONS.clear()
    APPLIED_MIGRATIONS.update(copy.deepcopy(INITIAL_MIGRATIONS))
    SESSIONS.clear()
    CSRF.clear()
    VERIFICATIONS.clear()
    SITE_SETTINGS.clear()
    STUDIO_NEWS.clear()
    STUDIO_NEWS.extend(copy.deepcopy(STUDIO_NEWS_SEED))
    REGISTER_CHALLENGES.clear()
    REGISTER_ATTEMPTS.clear()
    ONLINE_VISITORS.clear()
    PAYMENT_SETTINGS.update({"paypal_email": "", "paypal_client_id": "", "client_secret_configured": False, "environment": "sandbox", "encryption_key_configured": True})

def path_matches(path, configured):
    """Match an injected-error path exactly or as a path prefix."""
    return not configured or any(path == item or path.startswith(item.rstrip('/') + '/') for item in configured)


def err(message, status):
    return status, {"error": {"code": message.replace(" ", "_"), "message": message}}


def cookies(handler):
    out = {}
    raw = handler.headers.get("Cookie", "")
    for part in raw.split("; "):
        if "=" in part:
            key, value = part.split("=", 1)
            out[key] = value
    return out


def public(user):
    return {**{k: user[k] for k in ("id", "username", "email", "display_name", "role")},
            "acoins_balance": user.get("acoins_balance", 0)}


MOD_SUBJECTS = {
    'user.role.updated': 'Your role was updated',
    'user.status.updated': 'Your account status was updated',
    'user.banned': 'Your account was banned',
    'user.unbanned': 'Your account was unbanned',
}


def notification_items(user_id):
    """Mirror proxy.py: staff replies on the user's tickets plus moderation entries."""
    read = NOTIFICATION_READS.get(str(user_id), set())
    items = []
    for ticket in TICKETS:
        if ticket.get("user_id") != str(user_id):
            continue
        for reply in ticket.get("replies", []):
            if not reply.get("is_staff"):
                continue
            ref = 'reply:' + str(reply["id"])
            items.append({"id": ref, "kind": "ticket_reply",
                          "ticket_id": ticket["id"],
                          "subject": ticket.get("subject", ""),
                          "message": str(reply.get("message", ""))[:200],
                          "created_at": reply.get("created_at", 0),
                          "read": ref in read})
    for index, event in enumerate(MODERATION):
        if str(event.get("user_id")) != str(user_id):
            continue
        ref = 'mod:' + str(index)
        items.append({"id": ref, "kind": "account",
                      "subject": MOD_SUBJECTS.get(event.get("action"), str(event.get("action"))),
                      "message": str(event.get("detail", "") or ""),
                      "created_at": event.get("created_at", 0),
                      "read": ref in read})
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return items[:20]


# ---------------------------------------------------------------------------
# Hostile mode: deliberately wrong-shape payloads for drift-resilience checks.
#
# Each entry is a 200 response whose shape breaks the documented contract in a
# specific way — an array where an object belongs, a string where a number
# belongs, a required field missing. Both validated endpoints (the frontend
# rejects them at the fetch boundary) and unvalidated ones (which fall through
# to the safe-helper notice) are represented, because the two paths fail
# differently and both need coverage.
#
# Keep these in step with the real contracts: if a field's shape changes in
# docs/python-api-contracts.md, the matching hostile entry should change too,
# or the drift tests will be asserting against a contract nobody has any more.
#
# `session` is deliberately excluded from the default set: breaking it would
# sign every test out before it could reach the surface it is checking.
# ---------------------------------------------------------------------------

HOSTILE_SHAPES = {
    "games": lambda: {"games": "not-an-array"},
    "news": lambda: {"news": [{"id": 7, "title": 42, "summary": None, "url": {},
                              "published_at": "soon"}]},
    "account": lambda: {"stats": {"support_tickets": "many"}},  # user missing, count is text
    "support": lambda: {"tickets": [{"id": [], "subject": 12, "status": None,
                                      "priority": {}, "category": 5, "preview": None,
                                      "created_at": [], "updated_at": {}}]},
    "users": lambda: [dict(public(user), is_active=None, banned_at="maybe",
                           email_verified_at={}) for user in list(USERS.values())[:1]],
    "summary": lambda: {"users": "many", "active_users": None, "disabled_users": [],
                        "open_tickets": "0", "active_projects": {}, "active_deploys": 0},
    "telemetry": lambda: {"servers": "none", "updated_at": "now"},
    "omega": lambda: {"servers": {"not": "a list"}},
    "repo": lambda: {"ashathub": "unknown"},
    "client_errors": lambda: {"errors": {"nope": True}},
    "session": lambda: {"authenticated": "yes"},
}

# Endpoint path -> hostile key. Paths absent from this map are never affected.
HOSTILE_PATHS = {
    "/api/games": "games",
    "/api/news": "news",
    "/api/account/summary": "account",
    "/api/support": "support",
    "/api/admin/users": "users",
    "/api/admin/summary": "summary",
    "/api/admin/telemetry": "telemetry",
    "/api/admin/omega/servers": "omega",
    "/api/admin/repo-status": "repo",
    "/api/admin/client-errors": "client_errors",
    "/api/auth/session": "session",
}

DEFAULT_HOSTILE = ("games", "news", "account", "support", "users", "summary",
                   "telemetry", "omega", "repo", "client_errors")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- plumbing -------------------------------------------------------
    def reply(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def hostile_reply(self, path):
        """Return a deliberately wrong-shape 200 when this endpoint is in hostile mode."""
        key = HOSTILE_PATHS.get(path)
        if not key or key not in CONFIG.get("hostile", []):
            return None
        shape = HOSTILE_SHAPES.get(key)
        if shape is None:
            return None
        print(f"[mock] hostile {key} -> {path}", flush=True)
        # `users` is a list of rows inside the usual envelope.
        payload = shape()
        if key == "users":
            payload = {"users": payload, "page": 1, "page_size": 50}
        return self.reply(200, payload)

    def maybe_inject_fault(self, path):
        """Apply configured latency/failure; return True when a response was sent."""
        delay = max(0, min(int(CONFIG.get("latency_ms", 0) or 0), 60_000))
        if delay:
            time.sleep(delay / 1000)
        status = int(CONFIG.get("error_status", 0) or 0)
        paths = CONFIG.get("error_paths", [])
        if status and path_matches(path, paths):
            if CONFIG.get("error_once"):
                CONFIG["error_status"] = 0
            self.reply(*err(str(CONFIG.get("error_message") or "Injected mock API failure"), status))
            return True
        return False

    def request_base(self):
        """Return the loopback origin used by this mock instance."""
        host = self.headers.get("Host", "127.0.0.1:3110")
        return "http://" + host

    def current_user(self):
        token = cookies(self).get("ashat_rust_sid")
        if token and token in SESSIONS:
            user = USERS[SESSIONS[token]]
            if user["verified"]:
                return token, user
        return None, None

    def require_auth(self):
        token, user = self.current_user()
        if not user:
            return None, None, err("authentication required", 401)
        if self.command in ("POST", "PUT", "DELETE"):
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or supplied != CSRF.get(token):
                return None, None, err("csrf_failed", 403)
        return token, user, None

    def require_admin(self):
        """Mirror proxy.py: no session yields 403 admin_session_required, non-admin yields admin_role_required (not 401)."""
        token, user = self.current_user()
        if not user:
            return None, err("admin session required", 403)
        if user["role"].lower() != "admin":
            return None, err("admin role required", 403)
        if self.command in ("POST", "PUT", "DELETE"):
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or supplied != CSRF.get(token):
                return None, err("csrf_failed", 403)
        return user, None

    def migrations_summary(self):
        """Mirror the `migrations` object in /api/admin/database/status."""
        files = migration_files()
        applied, pending, drifted = [], [], []
        for row in files:
            recorded = APPLIED_MIGRATIONS.get(row["filename"])
            if not recorded:
                pending.append({"filename": row["filename"], "statements": row["statements"]})
            elif recorded["checksum"] != row["checksum"]:
                drifted.append(row["filename"])
            else:
                applied.append({"filename": row["filename"],
                                "applied_at": recorded.get("applied_at"),
                                "statements": recorded.get("statements"),
                                "applied_by": recorded.get("applied_by", "")})
        known = {row["filename"] for row in files}
        return {"tracked": True, "tracking_table": "schema_migrations",
                "current_revision": applied[-1]["filename"] if applied else None,
                "applied": applied, "pending": pending, "drifted": drifted,
                "missing": sorted(name for name in APPLIED_MIGRATIONS if name not in known)}

    def duplicate_submit(self, path, form, intent=""):
        """True when the same admin mutation for the same target arrived within the window.

        Target is `user_id` for user mutations, `id` for client error triage —
        otherwise every triage call would share one key and a second error
        triaged moments later would be rejected as a duplicate. `intent` tells
        apart different asks of the same target: resolving a report and then
        reopening it is a change of mind, not a double-click.
        """
        key = (path, str(form.get("user_id") or form.get("id") or ""), str(intent))
        now = time.time()
        last = RECENT_MUTATIONS.get(key)
        RECENT_MUTATIONS[key] = now
        return last is not None and now - last < IDEMPOTENCY_WINDOW

    def log_message(self, *args):
        pass

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            hostile = self.hostile_reply(path)
            if hostile is not None:
                return hostile
        if path == "/__config":
            return self.reply(200, {key: value for key, value in CONFIG.items()})
        if path == "/__codes":
            # Read-only test control: the pending verification tokens this mock
            # would have put in an email. The portal has no token-entry screen —
            # a real user clicks the emailed link — so an E2E run needs the value
            # the API would have received from that click. Loopback-only, like
            # /__config and /__reset.
            return self.reply(200, {"codes": {name: code for name, code in VERIFICATIONS.items()}})
        if path == "/__reset":
            reset_state()
            return self.reply(200, {"reset": True, "controls": {key: value for key, value in CONFIG.items()}})
        if path == "/api/oauth/.well-known/openid-configuration":
            issuer = f"{self.request_base()}/api/oauth"
            return self.reply(200, {
                "issuer": issuer,
                "authorization_endpoint": issuer + "/authorize",
                "token_endpoint": issuer + "/token",
                "userinfo_endpoint": issuer + "/userinfo",
                "jwks_uri": issuer + "/.well-known/jwks.json",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": ["S256"],
                "id_token_signing_alg_values_supported": ["RS256"],
            })
        if path == "/api/oauth/.well-known/jwks.json":
            return self.reply(200, MOCK_JWKS)
        if path == "/api/oauth/authorize":
            # Mirror proxy.py's authorize validation: response_type must be
            # "code", and the required params must be present (400 otherwise).
            params = parse_qs(urlparse(self.path).query)
            required = ("response_type", "client_id", "redirect_uri", "state")
            if any(not params.get(k, [""])[0] for k in required) or params["response_type"][0] != "code":
                return self.reply(*err("invalid authorize request", 400))
            return self.reply(200, {"content_type": "text/html"})
        if path == "/api/game-status":
            return self.reply(200, {"online": False, "source": "paws-and-parcels", "health": {"status": "ok"}})
        if path == "/api/portal-data":
            return self.reply(200, {"health": {"status": "ok"}, "players_online": None,
                                    "server_metrics": None, "season": None, "character": None,
                                    "news": [], "regions": []})
        if path.startswith("/api/") and self.maybe_inject_fault(path):
            return
        if path == "/api/auth/verify-email":
            token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            for username, code in list(VERIFICATIONS.items()):
                if code == token:
                    target = next(u for u in USERS.values() if u["username"] == username)
                    target["verified"] = True
                    del VERIFICATIONS[username]
                    return self.reply(200, {"verified": True})
            return self.reply(*err("invalid or expired verification token", 400))
        if path == "/api/auth/register-challenge":
            now = time.time()
            for token, expires in list(REGISTER_CHALLENGES.items()):
                if expires <= now:
                    del REGISTER_CHALLENGES[token]
            challenge = secrets.token_hex(16)
            REGISTER_CHALLENGES[challenge] = now + REGISTER_CHALLENGE_TTL
            return self.reply(200, {"challenge": challenge,
                                    "difficulty": site_int("register.challenge_difficulty"),
                                    "expires_in": int(REGISTER_CHALLENGE_TTL)})
        if path == "/api/site/content":
            return self.reply(200, site_content_payload())
        if path == "/api/auth/session":
            _, user = self.current_user()
            if user:
                return self.reply(200, {"authenticated": True, "user": public(user)})
            return self.reply(200, {"authenticated": False})
        if path == "/api/account/summary":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            return self.reply(200, {"user": public(user), "stats": {"support_tickets": len([t for t in TICKETS if t["user_id"] == user["id"]])}})
        if path == "/api/account/referral":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            code = user.get("referral_code") or mint_referral_code(user["id"])
            cutoff = time.time() - 86400
            uses_24h = sum(1 for use in REFERRAL_USES if use["code"] == code and use["created_at"] > cutoff)
            mine = [use for use in REFERRAL_USES if use["code"] == code]
            bonus = site_int("referral.bonus_acoins")
            per_day = site_int("referral.uses_per_day")
            return self.reply(200, {"code": code, "uses_24h": uses_24h,
                                    "uses_total": len(mine), "remaining_24h": max(0, per_day - uses_24h),
                                    "rewards_earned_acoins": sum(use["bonus_acoins"] for use in mine),
                                    "bonus_acoins": bonus, "uses_per_day": per_day})
        if path == "/api/account/transactions":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            now = int(time.time())
            mine = sorted((entry for entry in ACCOUNT_LEDGER if entry["user_id"] == user["id"]),
                          key=lambda entry: entry["created_at"], reverse=True)
            return self.reply(200, {"transactions": [
                *mine,
                {"id": "txn-demo-3", "kind": "game_purchase", "title": "Everenfell Season 1 Pass",
                 "detail": "Season pass via AGP Studios", "amount_cents": 999, "acoins_delta": 0,
                 "game_slug": "everenfell", "status": "completed", "created_at": now - 86400,
                 "receipt": {"order_id": "DEMO-3"}},
                {"id": "txn-demo-2", "kind": "acoins_spend", "title": "500 ACoins spent",
                 "detail": "In-game purchase", "amount_cents": None, "acoins_delta": -500,
                 "game_slug": "paws-and-parcels", "status": "completed", "created_at": now - 86400 * 3,
                 "receipt": {}},
                {"id": "txn-demo-1", "kind": "acoins_purchase", "title": "1,000 ACoins purchase",
                 "detail": "ACoins package via PayPal (sandbox)", "amount_cents": 100, "acoins_delta": 1000,
                 "game_slug": None, "status": "completed", "created_at": now - 86400 * 9,
                 "receipt": {"paypal_order_id": "DEMO-PAYPAL-1", "paypal_capture_id": "DEMO-CAPTURE-1"}},
            ]})
        if path == "/api/support":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            mine = [{k: v for k, v in t.items() if k != "replies"} for t in TICKETS if t["user_id"] == user["id"]]
            return self.reply(200, {"tickets": mine})
        if path.startswith("/api/support/"):
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            ticket_id = path.removeprefix("/api/support/").strip("/")
            ticket = next((t for t in TICKETS if t["id"] == ticket_id and t["user_id"] == user["id"]), None)
            if not ticket:
                return self.reply(*err("ticket not found", 404))
            # Detail shape carries the full `message` (list view has `preview`),
            # matching the real API's SELECT column list.
            ticket = dict(ticket, message=ticket.get("preview", ""), preview=None)
            del ticket["preview"]
            body = {k: v for k, v in ticket.items() if k != "replies"}
            return self.reply(200, {"ticket": body, "replies": ticket.get("replies", [])})
        if path == "/api/notifications":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            items = notification_items(user["id"])
            return self.reply(200, {"notifications": items,
                                    "unread_count": sum(1 for item in items if not item["read"])})
        if path == "/api/games":
            return self.reply(200, GAMES)
        if path == "/api/admin/games":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            listing = [dict(g) for g in GAMES["games"]]
            return self.reply(200, {"games": listing})
        if path == "/api/news":
            return self.reply(200, NEWS)
        if path.startswith("/api/admin/users/") and path.endswith("/moderation"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            uid = path.removeprefix("/api/admin/users/").removesuffix("/moderation")
            events = [e for e in MODERATION if e["user_id"] == uid]
            events.sort(key=lambda e: (e["created_at"], e.get("id", 0)), reverse=True)
            for i, e in enumerate(events):
                e = dict(e, id=len(MODERATION) - i)
                events[i] = e
            return self.reply(200, {"user_id": uid, "events": events})
        if path == "/api/admin/client-errors":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            status = parse_qs(urlparse(self.path).query).get("status", ["new"])[0]
            if status not in ("new", "resolved", "muted", "all"):
                return self.reply(*err("invalid status filter", 400))
            listed = []
            for entry in sorted(CLIENT_ERRORS, key=lambda item: (item["created_at"], item["id"]), reverse=True):
                digest = message_hash(entry["message"])
                mute = CLIENT_ERROR_MUTES.get(digest)
                triage = CLIENT_ERROR_TRIAGE.get(entry["id"], {})
                # A message-wide mute outranks the row's own state, matching
                # the API's effective-status rule.
                effective = "muted" if mute else triage.get("status", "new")
                if status != "all" and effective != status:
                    continue
                listed.append(dict(entry, message_hash=digest, triage_status=effective,
                                   triaged_at=triage.get("triaged_at", 0),
                                   triaged_by=triage.get("triaged_by", ""),
                                   muted_at=(mute or {}).get("muted_at", 0),
                                   muted_by=(mute or {}).get("muted_by", "")))
            return self.reply(200, {"errors": listed[:50], "page": 1, "page_size": 50, "status": status})
        if path in ("/api/admin/summary", "/api/admin/stats", "/api/admin/users", "/api/admin/settings",
                    "/api/admin/site-settings", "/api/admin/news",
                    "/api/admin/payments/settings", "/api/admin/database/status", "/api/admin/repo-status",
                    "/api/admin/telemetry", "/api/admin/omega/servers"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            if path == "/api/admin/summary":
                return self.reply(200, {"users": len(USERS), "active_users": len(USERS),
                                        "disabled_users": 0, "open_tickets": len(TICKETS),
                                        "active_projects": 0, "active_deploys": 0})
            if path == "/api/admin/stats":
                # Mirror proxy.py: counts from the mock's in-memory tables, so
                # quiet suites honestly read zero profits and null game probes.
                now = int(time.time())
                created = [u.get("created_at", now) for u in USERS.values()]
                day_ago = now - 86400
                stats_games = []
                for game in GAMES["games"]:
                    slug = game["slug"]
                    mine = [e for e in ACCOUNT_LEDGER if e.get("game_slug") == slug]
                    spent = sum(-e["acoins_delta"] for e in mine if e.get("kind") == "acoins_spend" and (e.get("acoins_delta") or 0) < 0)
                    stats_games.append({"slug": slug, "title": game["title"], "status": game["status"],
                                        "online": None, "entitlements": None,
                                        "acoins_spent": spent, "transactions": len(mine)})
                return self.reply(200, {
                    "signups": {
                        "today": sum(1 for ts in created if ts >= day_ago),
                        "week": sum(1 for ts in created if ts >= now - 7 * 86400),
                        "month": sum(1 for ts in created if ts >= now - 30 * 86400),
                        "total": len(USERS),
                    },
                    "profits_cents": {"day": 0, "week": 0, "month": 0, "year": 0},
                    "online": {"website": len(ONLINE_VISITORS), "paws": None},
                    "games": stats_games,
                })
            if path == "/api/admin/users":
                listing = [
                    dict(public(u),
                         is_active=0 if u.get("status_override") is False else 1,
                         banned_at=u.get("banned_at"),
                         email_verified_at=int(time.time()) if u["verified"] else None)
                    for u in USERS.values()
                ]
                return self.reply(200, {"users": listing, "page": 1, "page_size": 50})
            if path == "/api/admin/settings":
                return self.reply(200, {"email_verification_enabled": CONFIG["verification_required"],
                                        "secure_cookie": False, "backend": "python"})
            if path == "/api/admin/payments/settings":
                return self.reply(200, dict(PAYMENT_SETTINGS))
            if path == "/api/admin/database/status":
                return self.reply(200, {"database": "healthy", "version": "mock-8.0",
                                        "paws": {"status": "ok"}, "migrations": self.migrations_summary()})
            if path == "/api/admin/repo-status":
                return self.reply(200, {"ashathub": {"live": "mock0001", "repo": "mock0001", "state": "current"}})
            if path == "/api/admin/site-settings":
                settings = dict(SITE_STRING_DEFAULTS)
                for key in SITE_INT_DEFAULTS:
                    settings[key] = site_int(key)
                for key in SITE_STRING_DEFAULTS:
                    settings[key] = site_value(key)
                return self.reply(200, {"settings": settings})
            if path == "/api/admin/news":
                listing = sorted(STUDIO_NEWS, key=lambda item: (item.get("published_at", 0), item.get("created_at", 0)), reverse=True)
                return self.reply(200, {"news": [dict(item) for item in listing]})
            if path == "/api/admin/telemetry":
                return self.reply(200, {"servers": [], "updated_at": int(time.time())})
            return self.reply(200, {"servers": []})
        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/__reset":
            form = self.body_json()
            reset_state()
            if isinstance(form, dict) and "verification_required" in form:
                CONFIG["verification_required"] = bool(form["verification_required"])
            return self.reply(200, {"reset": True, "controls": {key: value for key, value in CONFIG.items()}})
        if path == "/__config":
            form = self.body_json()
            if not isinstance(form, dict):
                return self.reply(*err("config must be an object", 400))
            for key in ("verification_required", "error_once"):
                if key in form:
                    CONFIG[key] = bool(form[key])
            if "latency_ms" in form:
                CONFIG["latency_ms"] = max(0, int(form["latency_ms"]))
            if "error_status" in form:
                CONFIG["error_status"] = max(0, int(form["error_status"]))
            if "error_message" in form:
                CONFIG["error_message"] = str(form["error_message"])
            if "error_paths" in form:
                if not isinstance(form["error_paths"], list) or not all(isinstance(item, str) for item in form["error_paths"]):
                    return self.reply(*err("error_paths must be an array of strings", 400))
                CONFIG["error_paths"] = form["error_paths"]
            if "hostile" in form:
                selected = form["hostile"]
                if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
                    return self.reply(*err("hostile must be an array of endpoint keys", 400))
                unknown = [item for item in selected if item not in HOSTILE_SHAPES]
                if unknown:
                    return self.reply(*err(f"unknown hostile keys: {', '.join(sorted(unknown))}", 400))
                CONFIG["hostile"] = selected
            return self.reply(200, {key: value for key, value in CONFIG.items()})
        if path.startswith("/api/") and self.maybe_inject_fault(path):
            return
        form = self.body_json()

        if path == "/api/admin/payments/settings":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            if not isinstance(form, dict) or form.get("environment") not in ("sandbox", "live"):
                return self.reply(*err("invalid payment settings", 400))
            PAYMENT_SETTINGS.update({
                "paypal_email": str(form.get("paypal_email", "")),
                "paypal_client_id": str(form.get("paypal_client_id", "")),
                "client_secret_configured": bool(form.get("paypal_client_secret")) or PAYMENT_SETTINGS["client_secret_configured"],
                "environment": form["environment"],
            })
            return self.reply(200, {"ok": True})

        if path == "/api/admin/site-settings":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            incoming = form.get("settings") if isinstance(form, dict) else None
            if not isinstance(incoming, dict):
                return self.reply(*err("settings object required", 400))
            unknown = [key for key in incoming if key not in SITE_STRING_DEFAULTS and key not in SITE_INT_DEFAULTS]
            if unknown:
                return self.reply(*err("unknown setting: " + ", ".join(sorted(unknown)[:5]), 400))
            if len(incoming) > 32:
                return self.reply(*err("too many settings at once", 400))
            saved = {}
            for key, value in incoming.items():
                if key in SITE_INT_DEFAULTS:
                    default, lo, hi = SITE_INT_DEFAULTS[key]
                    try:
                        number = int(str(value).strip())
                    except (TypeError, ValueError):
                        return self.reply(*err(f"{key} must be an integer", 400))
                    if not lo <= number <= hi:
                        return self.reply(*err(f"{key} must be {lo}-{hi}", 400))
                    saved[key] = str(number)
                else:
                    if not isinstance(value, str):
                        return self.reply(*err(f"{key} must be text", 400))
                    text = value.strip()
                    if len(text) > SITE_STRING_LIMITS[key]:
                        return self.reply(*err(f"{key} too long (max {SITE_STRING_LIMITS[key]})", 400))
                    if key == "footer.tagline" and not text:
                        return self.reply(*err("footer tagline is required", 400))
                    if key.endswith("_url") and text and not re.fullmatch(r"https?://[^\s]+", text):
                        return self.reply(*err(f"{key} must be an http(s) URL", 400))
                    saved[key] = text
            SITE_SETTINGS.update(saved)
            return self.reply(200, {"ok": True, "settings": saved})

        if path == "/api/admin/news":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            if not isinstance(form, dict):
                return self.reply(*err("title is required (max 140)", 400))
            title = str(form.get("title", "")).strip()
            summary = str(form.get("summary", "")).strip()
            url = str(form.get("url", "") or "#news").strip() or "#news"
            if not title or len(title) > 140:
                return self.reply(*err("title is required (max 140)", 400))
            if not summary or len(summary) > 500:
                return self.reply(*err("summary is required (max 500)", 400))
            if len(url) > 500:
                return self.reply(*err("url too long (max 500)", 400))
            news_id = secrets.token_hex(8)
            now = int(time.time())
            STUDIO_NEWS.append({"id": news_id, "title": title, "summary": summary,
                                "url": url, "published_at": now, "created_at": now})
            return self.reply(200, {"ok": True, "id": news_id})

        if path in ("/api/acoins/order", "/api/acoins/capture"):
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            return self.reply(*err("PayPal is unavailable in the mock API", 503))

        if path == "/api/online-presence":
            visitor_id = form.get("visitor_id") if isinstance(form, dict) else None
            try:
                visitor_id = str(uuid.UUID(visitor_id))
            except (ValueError, TypeError, AttributeError):
                return self.reply(*err("invalid visitor id", 400))
            now = time.time()
            for key, seen in list(ONLINE_VISITORS.items()):
                if now - seen >= 90:
                    ONLINE_VISITORS.pop(key, None)
            ONLINE_VISITORS[visitor_id] = now
            return self.reply(200, {"online_users": len(ONLINE_VISITORS)})

        if path == "/api/telemetry/client-error":
            if int(self.headers.get("Content-Length", "0") or 0) > CLIENT_ERROR_MAX_BODY:
                return self.reply(*err("error report too large", 413))
            if not isinstance(form, dict):
                return self.reply(*err("error report must be a JSON object", 400))
            address = self.client_address[0]
            now = time.time()
            recent = [stamp for stamp in CLIENT_ERROR_ATTEMPTS.get(address, [])
                      if now - stamp < CLIENT_ERROR_WINDOW]
            if len(recent) >= CLIENT_ERROR_BUDGET:
                CLIENT_ERROR_ATTEMPTS[address] = recent
                return self.reply(*err("too many error reports", 429))
            recent.append(now)
            CLIENT_ERROR_ATTEMPTS[address] = recent
            message = str(form.get("message") or "").strip()
            if not message:
                return self.reply(*err("message required", 400))
            try:
                count = max(1, min(10000, int(form.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1
            listed = form.get("occurrences")
            token, user = self.current_user()
            entry = {
                "id": len(CLIENT_ERRORS) + 1,
                "source": str(form.get("source") or "unknown")[:40],
                "message": message[:400],
                "detail": str(form.get("detail") or "")[:400],
                "path": urlparse(str(form.get("path") or "")).path[:200],
                "count": count,
                "occurrences": json.dumps([str(item)[:200] for item in listed[:5]]
                                          if isinstance(listed, list) else []),
                "user_id": user["id"] if user else "",
                "user_agent": self.headers.get("User-Agent", "")[:200],
                "created_at": int(now),
            }
            CLIENT_ERRORS.append(entry)
            print(f"[client-error] {entry['source']} x{count} {entry['path'] or '/'}: {message[:200]}", flush=True)
            return self.reply(202, {"logged": True})

        if path == "/api/auth/register":
            address = real_ip(self)
            now = time.time()
            budget = site_int("register.budget")
            window = site_int("register.window_seconds")
            REGISTER_ATTEMPTS[address] = [stamp for stamp in REGISTER_ATTEMPTS.get(address, []) if now - stamp < window]
            if len(REGISTER_ATTEMPTS[address]) >= budget:
                return self.reply(*err("too many registration attempts", 429))
            REGISTER_ATTEMPTS[address].append(now)
            username = form.get("username", "").strip()
            email = form.get("email", "").strip()
            password = form.get("password", "")
            display = form.get("display_name", username).strip()
            problem = valid_username(username) or valid_email(email) or valid_password(password)
            if problem:
                return self.reply(*err(problem, 400))
            if not display or len(display) > 60:
                return self.reply(*err("display name is required", 400))
            if not register_proof_ok(form):
                return self.reply(*err("human verification failed", 400))
            if any(u["username"] == username or u["email"].lower() == email.lower() for u in USERS.values()):
                return self.reply(*err("username_or_email_taken", 409))
            cap = account_cap()
            if cap > 0 and sum(1 for u in USERS.values() if u.get("created_ip") == address) >= cap:
                return self.reply(*err("too many accounts from this address", 409))
            user_id = str(len(USERS) + 1)
            code = secrets.token_hex(4)
            VERIFICATIONS[username] = code
            USERS[user_id] = {"id": user_id, "username": username, "email": email,
                              "display_name": display, "role": "Member",
                              "password": password, "verified": not CONFIG["verification_required"],
                              "created_ip": address, "referral_code": "", "acoins_balance": 0,
                              "created_at": int(time.time())}
            own_code = mint_referral_code(user_id)
            referral = redeem_referral(form.get("referral_code"), user_id)
            print(f"[mock] verification code for {username}: {code}", flush=True)
            if CONFIG["verification_required"]:
                return self.reply(201, {"registered": True, "verification_required": True, "email": email})
            return self.reply(201, {"registered": True, "verification_required": False,
                                    "user": public(USERS[user_id]),
                                    "referral_code": own_code, "referral": referral})

        if path == "/api/auth/verify-email/resend":
            username = form.get("identifier", "").strip()
            for user in USERS.values():
                if user["username"] == username or user["email"] == username:
                    code = secrets.token_hex(4)
                    VERIFICATIONS[user["username"]] = code
                    print(f"[mock] verification code for {user['username']}: {code}", flush=True)
            return self.reply(202, {"sent": True})

        # Deliberately no POST /api/auth/verify-email: the contract has the
        # clicked email link hit GET with `?token=` (handled in do_GET), and the
        # real proxy.py implements no POST. A POST here used to look up a
        # username in a dict keyed by user id, raising KeyError and cutting the
        # connection mid-response; it falls through to the 404 below instead.

        if path == "/api/auth/login":
            identifier = form.get("identifier", "").strip()
            password = form.get("password", "")
            for user in USERS.values():
                if identifier in (user["username"], user["email"]):
                    if user["password"] != password:
                        return self.reply(401, {"error": {"code": "invalid_credentials", "message": "Invalid credentials"}})
                    if not user["verified"]:
                        return self.reply(401, {"error": {"code": "email_verification_required", "message": "Verify your email"}})
                    token = secrets.token_urlsafe(32)
                    csrf = secrets.token_urlsafe(32)
                    SESSIONS[token] = user["id"]
                    CSRF[token] = csrf
                    return self.reply(200, {"user": public(user)}, [
                        ("Set-Cookie", f"ashat_rust_sid={token}; Path=/; HttpOnly; SameSite=Lax"),
                        ("Set-Cookie", f"ashat_csrf={csrf}; Path=/; SameSite=Lax"),
                    ])
            return self.reply(401, {"error": {"code": "invalid_credentials", "message": "Invalid credentials"}})

        if path == "/api/auth/logout":
            token = cookies(self).get("ashat_rust_sid")
            if token in SESSIONS:
                del SESSIONS[token]
                CSRF.pop(token, None)
            return self.reply(200, {"ok": True}, [
                ("Set-Cookie", "ashat_rust_sid=; Path=/; Max-Age=0"),
            ])

        if path == "/api/support":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            ticket = {"id": secrets.token_hex(8), "user_id": user["id"],
                      "subject": form.get("subject", ""), "status": "open",
                      "priority": form.get("priority", "normal"),
                      "category": form.get("category", "other"),
                      "preview": form.get("message", "")[:120],
                      "created_at": int(time.time()), "updated_at": int(time.time())}
            TICKETS.append(ticket)
            return self.reply(201, {"id": ticket["id"], "status": "open"})

        if path.startswith("/api/support/") and path.endswith("/reply"):
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            ticket_id = path.removeprefix("/api/support/").removesuffix("/reply")
            ticket = next((t for t in TICKETS if t["id"] == ticket_id and t["user_id"] == user["id"]), None)
            if not ticket:
                return self.reply(*err("ticket not found", 404))
            ticket.setdefault("replies", []).append({
                "id": secrets.token_hex(8), "ticket_id": ticket_id,
                "message": form.get("message", ""), "is_staff": 0,
                "display_name": user["display_name"],
                "created_at": int(time.time()),
            })
            return self.reply(200, {"ok": True, "id": ticket["replies"][-1]["id"]})

        if path == "/api/notifications/read":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if not isinstance(form, dict):
                return self.reply(*err("invalid notification ids", 400))
            if form.get("all"):
                refs = [item["id"] for item in notification_items(user["id"]) if not item["read"]]
            elif isinstance(form.get("ids"), list) and form["ids"] and all(
                    isinstance(ref, str) and ref.startswith(("reply:", "mod:")) for ref in form["ids"]):
                refs = form["ids"]
            else:
                return self.reply(*err("invalid notification ids", 400))
            seen = NOTIFICATION_READS.setdefault(str(user["id"]), set())
            for ref in refs:
                seen.add(ref)
            return self.reply(200, {"ok": True, "read": len(refs)})

        if path.startswith("/api/admin/support/") and path.endswith("/reply"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            ticket_id = path.removeprefix("/api/admin/support/").removesuffix("/reply")
            message = str(form.get("message", "")).strip() if isinstance(form, dict) else ""
            if not ticket_id or not message:
                return self.reply(*err("ticket and message required", 400))
            ticket = next((t for t in TICKETS if t["id"] == ticket_id), None)
            if not ticket:
                return self.reply(*err("ticket not found", 404))
            reply_id = secrets.token_hex(8)
            ticket.setdefault("replies", []).append({
                "id": reply_id, "ticket_id": ticket_id,
                "message": message[:2000], "is_staff": 1,
                "display_name": user["display_name"],
                "created_at": int(time.time()),
            })
            ticket["updated_at"] = int(time.time())
            return self.reply(200, {"ok": True, "id": reply_id})

        if path == "/api/admin/omega/launch":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            return self.reply(200, {"ok": True})

        if path == "/api/admin/client-errors/status":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            if form.get("status") not in ("new", "resolved"):
                return self.reply(*err("invalid status", 400))
            if self.duplicate_submit(path, form, form["status"]):
                return self.reply(*err("duplicate submission; retry shortly", 409))
            # Malformed id is a 400 and an unknown one a 404, matching proxy.py.
            try:
                error_id = int(form.get("id"))
            except (TypeError, ValueError):
                return self.reply(*err("invalid client error id", 400))
            entry = next((item for item in CLIENT_ERRORS if item["id"] == error_id), None)
            if not entry:
                return self.reply(*err("client error not found", 404))
            if form["status"] == "new":
                CLIENT_ERROR_TRIAGE.pop(entry["id"], None)
            else:
                CLIENT_ERROR_TRIAGE[entry["id"]] = {"status": "resolved",
                                                     "triaged_at": int(time.time()),
                                                     "triaged_by": user["id"]}
            return self.reply(200, {"ok": True, "id": entry["id"], "status": form["status"]})

        if path == "/api/admin/client-errors/mute":
            # Message-level mute: {id, muted} identifies which message via a row,
            # mirroring how /api/admin/users/ban takes {user_id, banned}.
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            muted = bool(form.get("muted", True))
            if self.duplicate_submit(path, form, "muted" if muted else "unmuted"):
                return self.reply(*err("duplicate submission; retry shortly", 409))
            try:
                error_id = int(form.get("id"))
            except (TypeError, ValueError):
                return self.reply(*err("invalid client error id", 400))
            entry = next((item for item in CLIENT_ERRORS if item["id"] == error_id), None)
            if not entry:
                return self.reply(*err("client error not found", 404))
            digest = message_hash(entry["message"])
            if muted:
                CLIENT_ERROR_MUTES[digest] = {"message": entry["message"][:400],
                                              "muted_at": int(time.time()),
                                              "muted_by": user["id"]}
            else:
                CLIENT_ERROR_MUTES.pop(digest, None)
            return self.reply(200, {"ok": True, "id": entry["id"], "muted": muted,
                                    "message_hash": digest})

        if path == "/api/admin/users/role":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            target = USERS.get(str(form.get("user_id", "")))
            if not target or form.get("role") not in ("Member", "Pro", "Admin"):
                return self.reply(*err("invalid role or user", 400))
            if self.duplicate_submit(path, form):
                return self.reply(*err("duplicate submission; retry shortly", 409))
            target["role"] = form["role"]
            MODERATION.append({"user_id": str(form["user_id"]), "action": "user.role.updated",
                               "actor_id": user["id"], "actor_name": user["username"],
                               "detail": "role: " + form["role"], "created_at": int(time.time())})
            return self.reply(200, {"ok": True})

        if path == "/api/admin/users/status":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            target = USERS.get(str(form.get("user_id", "")))
            if not target:
                return self.reply(*err("user not found", 404))
            # Real API shape: {user_id, status: 'active'|'disabled'|'banned'}
            status = form.get("status", "active")
            if self.duplicate_submit(path, form):
                return self.reply(*err("duplicate submission; retry shortly", 409))
            if status == "banned":
                target["status_override"] = False
                target["banned_at"] = int(time.time())
            elif status == "disabled":
                target["status_override"] = False
                target["banned_at"] = None
            else:
                target["status_override"] = True
                target["banned_at"] = None
            MODERATION.append({"user_id": str(form.get("user_id", "")), "action": "user.status.updated",
                               "actor_id": user["id"], "actor_name": user["username"],
                               "detail": "status: " + status, "created_at": int(time.time())})
            return self.reply(200, {"ok": True})

        if path == "/api/admin/users/ban":
            # Real API now accepts {user_id, banned: bool} — default true for
            # backward compatibility with callers that omit the field.
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            target = USERS.get(str(form.get("user_id", "")))
            if not target:
                return self.reply(*err("user not found", 404))
            if self.duplicate_submit(path, form):
                return self.reply(*err("duplicate submission; retry shortly", 409))
            if form.get("banned", True):
                target["status_override"] = False
                target["banned_at"] = int(time.time())
            else:
                target["status_override"] = True
                target["banned_at"] = None
            MODERATION.append({"user_id": str(form.get("user_id", "")),
                               "action": "user.banned" if form.get("banned", True) else "user.unbanned",
                               "actor_id": user["id"], "actor_name": user["username"],
                               "detail": "banned" if form.get("banned", True) else "unbanned",
                               "created_at": int(time.time())})
            return self.reply(200, {"ok": True})

        if path == "/api/admin/database/migrations/apply":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            baseline = bool(form.get("baseline", False))
            handled = []
            for row in migration_files():
                recorded = APPLIED_MIGRATIONS.get(row["filename"])
                pending = not recorded
                drifted = bool(recorded) and recorded["checksum"] != row["checksum"]
                if pending or (baseline and drifted):
                    APPLIED_MIGRATIONS[row["filename"]] = dict(
                        row, applied_at=int(time.time()), applied_by=f"admin:{user['username']}")
                    handled.append(row["filename"])
            return self.reply(200, {"ok": True, "baseline": baseline, "applied": handled})

        if path == "/api/admin/telemetry/refresh" or path == "/api/admin/telemetry/clear":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            return self.reply(200, {"ok": True})

        if path == "/api/admin/telemetry":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin role required", 403))
            return self.reply(200, {"servers": [], "updated_at": int(time.time())})

        if path == "/api/admin/omega/servers":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin role required", 403))
            return self.reply(200, {"servers": []})

        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/admin/site-settings":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            form = self.body_json()
            incoming = form.get("settings") if isinstance(form, dict) else None
            if not isinstance(incoming, dict):
                return self.reply(*err("settings object required", 400))
            unknown = [key for key in incoming if key not in SITE_STRING_DEFAULTS and key not in SITE_INT_DEFAULTS]
            if unknown:
                return self.reply(*err("unknown setting: " + ", ".join(sorted(unknown)[:5]), 400))
            if len(incoming) > 32:
                return self.reply(*err("too many settings at once", 400))
            saved = {}
            for key, value in incoming.items():
                if key in SITE_INT_DEFAULTS:
                    default, lo, hi = SITE_INT_DEFAULTS[key]
                    try:
                        number = int(str(value).strip())
                    except (TypeError, ValueError):
                        return self.reply(*err(f"{key} must be an integer", 400))
                    if not lo <= number <= hi:
                        return self.reply(*err(f"{key} must be {lo}-{hi}", 400))
                    saved[key] = str(number)
                else:
                    if not isinstance(value, str):
                        return self.reply(*err(f"{key} must be text", 400))
                    text = value.strip()
                    if len(text) > SITE_STRING_LIMITS[key]:
                        return self.reply(*err(f"{key} too long (max {SITE_STRING_LIMITS[key]})", 400))
                    if key == "footer.tagline" and not text:
                        return self.reply(*err("footer tagline is required", 400))
                    if key.endswith("_url") and text and not re.fullmatch(r"https?://[^\s]+", text):
                        return self.reply(*err(f"{key} must be an http(s) URL", 400))
                    saved[key] = text
            SITE_SETTINGS.update(saved)
            return self.reply(200, {"ok": True, "settings": saved})
        if path.startswith("/api/admin/games/"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            slug = path.removeprefix("/api/admin/games/").strip("/")
            target = next((g for g in GAMES["games"] if g["slug"] == slug), None)
            if not target:
                return self.reply(*err("game not found", 404))
            form = self.body_json()
            if not isinstance(form, dict):
                return self.reply(*err("game fields required", 400))
            allowed = ("poster", "title", "summary", "status", "url", "featured", "sort_order", "tags")
            unknown = [key for key in form if key not in allowed]
            if unknown:
                return self.reply(*err("unknown field: " + ", ".join(sorted(unknown)[:5]), 400))
            updates = {}
            if "poster" in form:
                poster = form["poster"]
                if not isinstance(poster, str):
                    return self.reply(*err("poster url required", 400))
                poster = poster.strip()
                if len(poster) > 400:
                    return self.reply(*err("poster url too long (max 400)", 400))
                updates["poster"] = poster
            if "title" in form:
                title = str(form["title"]).strip()
                if not title or len(title) > 120:
                    return self.reply(*err("title is required (max 120)", 400))
                updates["title"] = title
            if "summary" in form:
                summary = str(form["summary"]).strip()
                if not summary or len(summary) > 500:
                    return self.reply(*err("summary is required (max 500)", 400))
                updates["summary"] = summary
            if "status" in form:
                if form["status"] not in ("live", "in-development", "coming-soon"):
                    return self.reply(*err("status must be live, in-development, or coming-soon", 400))
                updates["status"] = form["status"]
            if "url" in form:
                url = str(form["url"] or "").strip()
                if len(url) > 500:
                    return self.reply(*err("url too long (max 500)", 400))
                updates["url"] = url
            if "featured" in form:
                if type(form["featured"]) is not bool:
                    return self.reply(*err("featured must be true or false", 400))
                updates["featured"] = form["featured"]
            if "sort_order" in form:
                try:
                    order = int(form["sort_order"])
                except (TypeError, ValueError):
                    return self.reply(*err("sort_order must be an integer", 400))
                if not 0 <= order <= 1000:
                    return self.reply(*err("sort_order must be 0-1000", 400))
                updates["sort_order"] = order
            if "tags" in form:
                tags = form["tags"]
                if isinstance(tags, str):
                    tags = [tag.strip() for tag in tags.split(",")]
                if not isinstance(tags, list) or not tags or len(tags) > 10 or any(not isinstance(tag, str) or not tag.strip() or len(tag) > 40 for tag in tags):
                    return self.reply(*err("tags must be 1-10 short labels", 400))
                updates["tags"] = [tag.strip() for tag in tags]
            if not updates:
                return self.reply(*err("nothing to update", 400))
            target.update(updates)
            result = {"ok": True, "slug": slug, "game": dict(target)}
            if "poster" in updates:
                result["poster"] = updates["poster"]
            return self.reply(200, result)
        if path.startswith("/api/admin/news/"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            news_id = path.removeprefix("/api/admin/news/").strip("/")
            if not news_id:
                return self.reply(*err("news id required", 400))
            target = next((item for item in STUDIO_NEWS if item["id"] == news_id), None)
            if not target:
                return self.reply(*err("news not found", 404))
            form = self.body_json()
            if not isinstance(form, dict) or not form:
                return self.reply(*err("nothing to update", 400))
            allowed = ("title", "summary", "url")
            unknown = [key for key in form if key not in allowed]
            if unknown:
                return self.reply(*err("unknown field: " + ", ".join(sorted(unknown)[:5]), 400))
            updates = {}
            if "title" in form:
                title = str(form["title"]).strip()
                if not title or len(title) > 140:
                    return self.reply(*err("title is required (max 140)", 400))
                updates["title"] = title
            if "summary" in form:
                summary = str(form["summary"]).strip()
                if not summary or len(summary) > 500:
                    return self.reply(*err("summary is required (max 500)", 400))
                updates["summary"] = summary
            if "url" in form:
                url = str(form["url"] or "#news").strip() or "#news"
                if len(url) > 500:
                    return self.reply(*err("url too long (max 500)", 400))
                updates["url"] = url
            if not updates:
                return self.reply(*err("nothing to update", 400))
            target.update(updates)
            return self.reply(200, {"ok": True, "id": news_id, "updated": updates})
        if path == "/api/account":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            form = self.body_json()
            username = form.get("username", "").strip()
            email = form.get("email", "").strip()
            display = form.get("display_name", user["display_name"]).strip()
            if not username or not email or not display:
                return self.reply(*err("username, email, and display_name are required", 400))
            problem = valid_username(username) or valid_email(email)
            if problem:
                return self.reply(*err(problem, 400))
            if len(display) > 60:
                return self.reply(*err("display name is required", 400))
            if any(u["id"] != user["id"] and (u["username"] == username or u["email"].lower() == email.lower()) for u in USERS.values()):
                return self.reply(*err("username or email already in use", 409))
            user["username"] = username
            user["email"] = email
            user["display_name"] = display
            return self.reply(200, {"user": public(user)})
        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/admin/news/"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            news_id = path.removeprefix("/api/admin/news/").strip("/").rsplit("/", 1)[-1]
            if not news_id:
                return self.reply(*err("news id required", 400))
            target = next((item for item in STUDIO_NEWS if item["id"] == news_id), None)
            if not target:
                return self.reply(*err("news not found", 404))
            STUDIO_NEWS.remove(target)
            return self.reply(200, {"ok": True, "deleted": news_id})
        if path.startswith("/api/admin/users/"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            target_id = path.rsplit("/", 1)[1]
            if target_id not in USERS or target_id == user["id"]:
                return self.reply(*err("user not found", 404))
            del USERS[target_id]
            return self.reply(200, {"ok": True, "deleted": target_id})
        if path == "/api/account":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            form = self.body_json()
            if form.get("confirm") != "DELETE":
                return self.reply(*err("confirm DELETE to remove the account", 400))
            token = cookies(self).get("ashat_rust_sid")
            del USERS[user["id"]]
            if token in SESSIONS:
                del SESSIONS[token]
                CSRF.pop(token, None)
            return self.reply(200, {"deleted": True}, [
                ("Set-Cookie", "ashat_rust_sid=; Path=/; Max-Age=0"),
                ("Set-Cookie", "ashat_csrf=; Path=/; Max-Age=0"),
            ])
        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})


class DevServer(ThreadingHTTPServer):
    """Threading server whose accept backlog survives parallel test bursts.

    The socketserver default backlog of 5 refuses connections (ECONNREFUSED,
    not a queue) once a suite runs its workers in parallel, which surfaces as
    random failed requests rather than an obvious server error.
    """

    daemon_threads = True
    request_queue_size = 128


def hostile_selection(argv):
    """Hostile endpoint keys from --hostile or --hostile=a,b (empty when absent)."""
    for argument in argv:
        if argument == "--hostile":
            return list(DEFAULT_HOSTILE)
        if argument.startswith("--hostile="):
            return [item.strip() for item in argument.split("=", 1)[1].split(",") if item.strip()]
    return []


def account_cap_flag(argv):
    """--account-cap=N relaxes the per-IP account cap for test harnesses
    (suites register many accounts from one loopback IP); 0 disables it.
    Absent, the register.max_accounts_per_ip setting (default 2) applies."""
    for argument in argv:
        if argument.startswith("--account-cap="):
            try:
                return max(0, int(argument.split("=", 1)[1]))
            except ValueError:
                raise SystemExit("[mock] --account-cap needs an integer (0 disables the cap)")
    return None


if __name__ == "__main__":
    required_verification = "--verification-required" in sys.argv
    hostile = hostile_selection(sys.argv)
    cap_flag = account_cap_flag(sys.argv)
    unknown = [item for item in hostile if item not in HOSTILE_SHAPES]
    if unknown:
        print(f"[mock] unknown --hostile keys: {', '.join(sorted(unknown))}", flush=True)
        print(f"[mock] known keys: {', '.join(sorted(HOSTILE_SHAPES))}", flush=True)
        raise SystemExit(2)
    CONFIG["verification_required"] = required_verification
    CONFIG["hostile"] = hostile
    if cap_flag is not None:
        CONFIG["account_cap"] = cap_flag
    STARTUP_CONTROLS.update({"verification_required": required_verification, "hostile": list(hostile)})
    if cap_flag is not None:
        STARTUP_CONTROLS["account_cap"] = cap_flag
    # PORT mirrors services/python-api/proxy.py so parallel stacks (E2E runs)
    # can bind their own instance without touching the default 3110.
    port = int(os.environ.get("PORT", "3110"))
    server = DevServer(("127.0.0.1", port), Handler)
    print(f"[mock] AGP Studios API mock on 127.0.0.1:{port} (verification_required={required_verification})", flush=True)
    if hostile:
        print(f"[mock] HOSTILE shapes served for: {', '.join(hostile)}", flush=True)
    server.serve_forever()
