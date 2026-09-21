#!/usr/bin/env python3
"""Contract-faithful mock of the AGP Studios Python API for local dev + E2E testing.

A documented dev utility, NOT a reference implementation. It implements the
endpoints from docs/python-api-contracts.md that the portal consumes:

  auth/session, register (with optional email verification), login, logout,
  verify-email (+resend), account summary / PUT / DELETE, support tickets +
  replies, games, news, and the admin surface (summary, users incl. role /
  status / ban / delete, settings, database status, repo-status, telemetry,
  omega).

Behavior notes:
- In-memory state only; everything is lost on restart. Pre-seeded admin:
  username `boss`, password `admin-pass-1`.
- Verification codes are printed to stdout so tests can complete the verify
  flow; run with --no-verification to activate accounts immediately.
- Listens on 127.0.0.1:3110 by default (override with `PORT`); request shapes
  mirror services/python-api/proxy.py, including its quirks (POST
  /admin/users/ban accepts `{user_id, banned}`; /admin/users/status takes
  `{user_id, status}`; PUT /api/account requires username/email/display_name).
- Failure injection is controlled through unauthenticated loopback-only
  `/__config` and `/__reset`: add latency with `{"latency_ms": 250}`, return
  an injected error with `{"error_status": 503, "error_paths": ["/api/news"]}`,
  and make it one-shot with `{"error_once": true}`. Reset restores seed data,
  clears sessions, and clears all controls; `{"verification_required": false}`
  may be included in the reset body.

Run via `npm run mock-api` (alone) or `npm run dev:mock` (alongside vite) from
apps/ashat-hub-web. See the README's "Frontend with the mock API" section.
"""
import copy
import json
import os
import secrets
import sys
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# Dev-only controls. /__config and /__reset intentionally require no auth:
# this server binds to loopback and is never used as a production API.
DEFAULT_CONTROLS = {
    "verification_required": True,
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
          "verified": True},
}
SESSIONS = {}          # session token -> user id
CSRF = {}              # session token -> csrf token
VERIFICATIONS = {}     # username -> verification code
TICKETS = []           # list of ticket dicts
GAMES = {"games": [
    {"slug": "paws-and-parcels", "title": "Paws & Parcels",
     "summary": "Deliver small moments. Explore a growing world.",
     "status": "live", "tags": ["Cozy", "Exploration", "Social"],
     "url": "https://pawsandparcels.agpstudios.org",
     "poster": "/paws-parcels-poster.svg", "featured": True},
    {"slug": "world-of-ra-horizon", "title": "The World of Ra - Horizon - Season 1",
     "summary": "A new world is taking shape.",
     "status": "in-development", "tags": ["Exploration"], "url": "",
     "poster": "/world-of-ra-poster.svg", "featured": False},
    {"slug": "more-titles", "title": "More in Development",
     "summary": "New worlds, new experiences, and more to come.",
     "status": "coming-soon", "tags": ["Variety"], "url": "#news",
     "poster": "/more-in-development-poster.svg", "featured": False},
]}
NEWS = {"news": [
    {"id": "n1", "title": "A New Chapter Ahead",
     "summary": "We're working on new worlds, new ideas, and more ways to play together.",
     "url": "#news", "published_at": int(time.time())},
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
MODERATION = []  # user_moderation_history mirror (migration 0004)

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
    """NNNN_description.sql files in application order, per the real runner."""
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
    USERS.clear()
    USERS.update(copy.deepcopy(INITIAL_USERS))
    TICKETS.clear()
    TICKETS.extend(copy.deepcopy(INITIAL_TICKETS))
    RECENT_MUTATIONS.clear()
    MODERATION.clear()
    APPLIED_MIGRATIONS.clear()
    APPLIED_MIGRATIONS.update(copy.deepcopy(INITIAL_MIGRATIONS))
    SESSIONS.clear()
    CSRF.clear()
    VERIFICATIONS.clear()


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
    return {k: user[k] for k in ("id", "username", "email", "display_name", "role")}


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
                return None, None, err("csrf token required", 403)
        return token, user, None

    def require_admin(self):
        """Mirror proxy.py: no session => 403 admin_authentication_required,
        NOT 401. The real API's admin_user() collapses both cases into one 403
        and the smoke suite asserts that code."""
        token, user = self.current_user()
        if not user or user["role"].lower() != "admin":
            return None, err("admin authentication required", 403)
        if self.command in ("POST", "PUT", "DELETE"):
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or supplied != CSRF.get(token):
                return None, err("csrf token required", 403)
        return user, None

    def migrations_summary(self):
        """Mirror proxy.py's `migrations` object inside /api/admin/database/status."""
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

    def duplicate_submit(self, path, form):
        """Mirror proxy.py's idempotency guard: True when the same admin
        mutation for the same user arrived within the window."""
        key = (path, str(form.get("user_id", "")))
        now = time.time()
        last = RECENT_MUTATIONS.get(key)
        RECENT_MUTATIONS[key] = now
        return last is not None and now - last < IDEMPOTENCY_WINDOW

    def log_message(self, *args):
        pass

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/__config":
            return self.reply(200, {key: value for key, value in CONFIG.items()})
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
        if path in ("/api/admin/summary", "/api/admin/users", "/api/admin/settings",
                    "/api/admin/database/status", "/api/admin/repo-status",
                    "/api/admin/telemetry", "/api/admin/omega/servers"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            if path == "/api/admin/summary":
                return self.reply(200, {"users": len(USERS), "active_users": len(USERS),
                                        "disabled_users": 0, "open_tickets": len(TICKETS),
                                        "active_projects": 0, "active_deploys": 0})
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
            if path == "/api/admin/database/status":
                return self.reply(200, {"database": "healthy", "version": "mock-8.0",
                                        "paws": {"status": "ok"}, "migrations": self.migrations_summary()})
            if path == "/api/admin/repo-status":
                return self.reply(200, {"ashathub": {"live": "mock0001", "repo": "mock0001", "state": "current"}})
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
            return self.reply(200, {key: value for key, value in CONFIG.items()})
        if path.startswith("/api/") and self.maybe_inject_fault(path):
            return
        form = self.body_json()

        if path == "/api/auth/register":
            username = form.get("username", "").strip()
            email = form.get("email", "").strip()
            password = form.get("password", "")
            display = form.get("display_name", username).strip()
            if not (3 <= len(username) <= 30 and "@" in email and len(password) >= 8 and display):
                return self.reply(*err("invalid_registration", 400))
            if any(u["username"] == username or u["email"] == email for u in USERS.values()):
                return self.reply(*err("username_or_email_taken", 409))
            user_id = str(len(USERS) + 1)
            code = secrets.token_hex(4)
            VERIFICATIONS[username] = code
            USERS[user_id] = {"id": user_id, "username": username, "email": email,
                              "display_name": display, "role": "Member",
                              "password": password, "verified": not CONFIG["verification_required"]}
            print(f"[mock] verification code for {username}: {code}", flush=True)
            if CONFIG["verification_required"]:
                return self.reply(201, {"registered": True, "verification_required": True, "email": email})
            return self.reply(201, {"registered": True, "verification_required": False,
                                    "user": public(USERS[user_id])})

        if path == "/api/auth/verify-email/resend":
            username = form.get("identifier", "").strip()
            for user in USERS.values():
                if user["username"] == username or user["email"] == username:
                    code = secrets.token_hex(4)
                    VERIFICATIONS[user["username"]] = code
                    print(f"[mock] verification code for {user['username']}: {code}", flush=True)
            return self.reply(202, {"sent": True})

        if path == "/api/auth/verify-email":
            token = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            for username, code in list(VERIFICATIONS.items()):
                if code == token:
                    USERS[[u["username"] for u in USERS.values() if u["username"] == username][0]]["verified"] = True
                    del VERIFICATIONS[username]
                    return self.reply(200, {"verified": True})
            return self.reply(*err("invalid or expired verification token", 400))

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

        if path == "/api/admin/omega/launch":
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            return self.reply(200, {"ok": True})

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
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"servers": [], "updated_at": int(time.time())})

        if path == "/api/admin/omega/servers":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"servers": []})

        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_PUT(self):
        path = urlparse(self.path).path
        if path.startswith("/api/admin/games/"):
            user, failure = self.require_admin()
            if failure:
                return self.reply(*failure)
            slug = path.removeprefix("/api/admin/games/").strip("/")
            target = next((g for g in GAMES["games"] if g["slug"] == slug), None)
            if not target:
                return self.reply(*err("game not found", 404))
            form = self.body_json()
            poster = form.get("poster")
            if not isinstance(poster, str):
                return self.reply(*err("poster url required", 400))
            poster = poster.strip()
            if len(poster) > 400:
                return self.reply(*err("poster url too long (max 400)", 400))
            target["poster"] = poster
            return self.reply(200, {"ok": True, "slug": slug, "poster": poster})
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
            if any(u["id"] != user["id"] and (u["username"] == username or u["email"] == email) for u in USERS.values()):
                return self.reply(*err("username or email already in use", 409))
            user["username"] = username
            user["email"] = email
            user["display_name"] = display
            return self.reply(200, {"user": public(user)})
        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_DELETE(self):
        path = urlparse(self.path).path
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


if __name__ == "__main__":
    required_verification = "--no-verification" not in sys.argv
    CONFIG["verification_required"] = required_verification
    # PORT mirrors services/python-api/proxy.py so parallel stacks (E2E runs)
    # can bind their own instance without touching the default 3110.
    port = int(os.environ.get("PORT", "3110"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"[mock] AGP Studios API mock on 127.0.0.1:{port} (verification_required={required_verification})", flush=True)
    server.serve_forever()
