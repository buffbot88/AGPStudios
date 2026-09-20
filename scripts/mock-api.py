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
- Listens on 127.0.0.1:3110 (the vite proxy target); request shapes mirror
  services/python-api/proxy.py, including its quirks (POST /admin/users/ban
  always bans; /admin/users/status takes {user_id, status}; PUT /api/account
  requires username/email/display_name).

Run via `npm run mock-api` (alone) or `npm run dev:mock` (alongside vite) from
apps/ashat-hub-web. See the README's "Frontend with the mock API" section.
"""
import json
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# email verification required for register (toggle via /__config)
CONFIG = {"verification_required": True}

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
]}
NEWS = {"news": [
    {"id": "n1", "title": "A New Chapter Ahead",
     "summary": "We're working on new worlds, new ideas, and more ways to play together.",
     "url": "#news", "published_at": int(time.time())},
]}


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

    def log_message(self, *args):
        pass

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
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
            return self.reply(200, {"user": None, "stats": {"conversation_messages": None, "support_tickets": "not-a-number"}})
        if path == "/api/support":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            mine = [{k: v for k, v in t.items() if k != "replies"} for t in TICKETS if t["user_id"] == user["id"]]
            return self.reply(200, {"tickets": "oops-not-a-list"})
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
        if path == "/api/news":
            return self.reply(200, NEWS)
        if path == "/api/admin/summary":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"users": None, "active_users": "12",
                                    "disabled_users": None, "open_tickets": {"bad": True},
                                    "active_projects": 0, "active_deploys": 0})

        if path == "/api/admin/users":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            listing = [
                dict(public(u),
                     is_active=0 if u.get("status_override") is False else 1,
                     banned_at=u.get("banned_at"),
                     email_verified_at=int(time.time()) if u["verified"] else None)
                for u in USERS.values()
            ]
            return self.reply(200, {"users": [None, {"id": None, "username": 123, "display_name": None, "role": None, "is_active": "yes", "banned_at": 5, "email_verified_at": None}]})

        if path == "/api/admin/settings":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"email_verification_enabled": CONFIG["verification_required"], "secure_cookie": False, "backend": "python"})

        if path == "/api/admin/database/status":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"database": "healthy", "version": "mock-8.0", "paws": {"status": "ok"}})

        if path == "/api/admin/repo-status":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"ashathub": {"live": "mock0001", "repo": "mock0001", "state": "current"}})
        return self.reply(404, {"error": {"code": "not_found", "message": "unknown endpoint"}})

    def do_POST(self):
        path = urlparse(self.path).path
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
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            return self.reply(200, {"ok": True})

        if path == "/api/admin/users/role":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            target = USERS.get(str(form.get("user_id", "")))
            if not target or form.get("role") not in ("Member", "Pro", "Admin"):
                return self.reply(*err("invalid role or user", 400))
            target["role"] = form["role"]
            return self.reply(200, {"ok": True})

        if path == "/api/admin/users/status":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            target = USERS.get(str(form.get("user_id", "")))
            if not target:
                return self.reply(*err("user not found", 404))
            # Real API shape: {user_id, status: 'active'|'disabled'|'banned'}
            status = form.get("status", "active")
            if status == "banned":
                target["status_override"] = False
                target["banned_at"] = int(time.time())
            elif status == "disabled":
                target["status_override"] = False
                target["banned_at"] = None
            else:
                target["status_override"] = True
                target["banned_at"] = None
            return self.reply(200, {"ok": True})

        if path == "/api/admin/users/ban":
            # Real API now accepts {user_id, banned: bool} — default true for
            # backward compatibility with callers that omit the field.
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
            target = USERS.get(str(form.get("user_id", "")))
            if not target:
                return self.reply(*err("user not found", 404))
            if form.get("banned", True):
                target["status_override"] = False
                target["banned_at"] = int(time.time())
            else:
                target["status_override"] = True
                target["banned_at"] = None
            return self.reply(200, {"ok": True})

        if path == "/api/admin/telemetry/refresh" or path == "/api/admin/telemetry/clear":
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
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
            _, user, failure = self.require_auth()
            if failure:
                return self.reply(*failure)
            if user["role"].lower() != "admin":
                return self.reply(*err("admin authentication required", 403))
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
    server = ThreadingHTTPServer(("127.0.0.1", 3110), Handler)
    print(f"[mock] AGP Studios API mock on 127.0.0.1:3110 (verification_required={required_verification})", flush=True)
    server.serve_forever()
