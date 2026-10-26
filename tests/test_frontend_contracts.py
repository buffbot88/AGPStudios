#!/usr/bin/env python3
"""Frontend contract tests: assert the API response shapes the portal reads.

Every assertion here mirrors a TypeScript type the frontend actually consumes
(the `request<T>` / `fetch` call sites in apps/ashat-hub-web/src). When the API
drifts from those shapes — e.g. the `conversation_messages` account-stats bug
that crashed the whole app — these tests fail in CI instead of in production.

Runs against scripts/mock-api.py by default (contract-mirrored to proxy.py,
so passing here means the real API satisfies the same shapes). To point at a
live deployment instead:

    ASHAT_TEST_BASE=https://www.agpstudios.org python3 tests/test_frontend_contracts.py

Read-only plus self-contained mutations: registers its own throwaway member
(`contract-shape-*`) and admin-session flows via the seeded admin, and cleans
up what it creates. Requires the mock (or a live env) to be running.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("ASHAT_TEST_BASE", "http://127.0.0.1:3110")

failures: list[str] = []
passed = 0


def check(condition, label, detail=""):
    global passed
    if condition:
        passed += 1
    else:
        failures.append(f"{label}{f' — {detail}' if detail else ''}")
        print(f"  FAIL: {label}{f' — {detail}' if detail else ''}")


class Client:
    """A cookie-carrying HTTP client (one per identity)."""

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = dict(headers or {})
        # Mirror the frontend's request() helper: mutations carry the CSRF token.
        if method in ("POST", "PUT", "DELETE"):
            headers.setdefault("X-CSRF-Token", self.csrf())
        request = urllib.request.Request(
            BASE + path, data=data, method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            response = self.opener.open(request, timeout=15)
            raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            return exc.code, (json.loads(raw) if raw else None)
        return response.status, (json.loads(raw) if raw else None)

    def csrf(self):
        for cookie in self.jar:
            if cookie.name == "ashat_csrf":
                return cookie.value
        return ""


def is_str(v): return isinstance(v, str)
def is_num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)
def is_bool(v): return isinstance(v, bool)


# ---- shapes the frontend declares (mirrored from the TS types) --------------

def check_user_shape(user, label):
    check(isinstance(user, dict) and all(is_str(user.get(k)) for k in ("id", "username", "email", "display_name", "role")),
          f"{label}: user has string id/username/email/display_name/role", repr(user)[:120])


def check_ticket_summary(t, label):
    ok = isinstance(t, dict) and all(is_str(t.get(k)) for k in ("id", "subject", "status", "priority", "category", "preview", "created_at", "updated_at")) or \
         isinstance(t, dict) and all(k in t for k in ("id", "subject", "status", "priority", "category", "preview", "created_at", "updated_at"))
    check(ok, f"{label}: ticket summary has id/subject/status/priority/category/preview/created_at/updated_at", repr(t)[:120])


def check_admin_user(u, label):
    ok = isinstance(u, dict) and all(is_str(u.get(k)) for k in ("id", "username", "email", "display_name", "role"))
    check(ok, f"{label}: admin user row has string id/username/email/display_name/role", repr(u)[:120])
    check("is_active" in u and (is_num(u["is_active"]) or is_bool(u["is_active"])),
          f"{label}: admin user row has is_active (the frontend's enable/disable toggles on it)", repr(u)[:120])
    check("banned_at" in u, f"{label}: admin user row has banned_at key", repr(u)[:120])


def run():
    member, admin = Client(), Client()
    username = f"contract-shape-{secrets.token_hex(4)}"

    # -- OIDC issuer (docs/python-api-contracts.md) ----------------------------
    # Keep these assertions against the live discovery document rather than
    # merely checking a hand-written local constant: issuer URLs and supported
    # algorithms are security-sensitive integration points.
    oidc_base = BASE.rstrip("/") + "/api/oauth"
    code, discovery = member.req("/api/oauth/.well-known/openid-configuration")
    check(code == 200 and isinstance(discovery, dict),
          "OIDC discovery returns 200 JSON", repr(discovery)[:160])
    if isinstance(discovery, dict):
        check(discovery.get("issuer") == oidc_base,
              "OIDC discovery issuer matches the configured API origin", repr(discovery.get("issuer")))
        for key, suffix in (("authorization_endpoint", "/authorize"),
                            ("token_endpoint", "/token"),
                            ("userinfo_endpoint", "/userinfo"),
                            ("jwks_uri", "/.well-known/jwks.json")):
            check(discovery.get(key) == oidc_base + suffix,
                  f"OIDC discovery {key} matches issuer", repr(discovery.get(key)))
        check(discovery.get("response_types_supported") == ["code"],
              "OIDC discovery supports code response type", repr(discovery.get("response_types_supported")))
        check(discovery.get("grant_types_supported") == ["authorization_code"],
              "OIDC discovery supports authorization_code grant", repr(discovery.get("grant_types_supported")))
        check(discovery.get("code_challenge_methods_supported") == ["S256"],
              "OIDC discovery requires PKCE S256", repr(discovery.get("code_challenge_methods_supported")))
        check(discovery.get("id_token_signing_alg_values_supported") == ["RS256"],
              "OIDC discovery advertises RS256 ID tokens", repr(discovery.get("id_token_signing_alg_values_supported")))

    code, jwks = member.req("/api/oauth/.well-known/jwks.json")
    keys = (jwks or {}).get("keys") if isinstance(jwks, dict) else None
    check(code == 200 and isinstance(keys, list) and bool(keys),
          "OIDC JWKS returns a non-empty keys array", repr(jwks)[:160])
    for key in keys or []:
        check(isinstance(key, dict) and all(is_str(key.get(field)) for field in ("kty", "use", "alg", "kid", "n", "e")),
              "OIDC JWKS key has RSA public-key fields", repr(key)[:160])
        check(key.get("kty") == "RSA" and key.get("use") == "sig" and key.get("alg") == "RS256",
              "OIDC JWKS key is an RSA signing key", repr(key)[:160])

    # -- public homepage content (content.ts fetchers) ------------------------
    code, body = member.req("/api/games")
    check(code == 200, "/api/games returns 200", str(code))
    games = (body or {}).get("games")
    check(isinstance(games, list) and games, "/api/games: games is a non-empty list")
    for g in games or []:
        ok = all(k in g for k in ("slug", "title", "summary", "status", "tags", "url", "poster", "featured"))
        check(ok, "/api/games: game has slug/title/summary/status/tags/url/poster/featured", repr(g)[:120])
        check(g.get("status") in ("live", "in-development", "coming-soon"), "/api/games: status is a known GameStatus", repr(g.get("status")))
        check(isinstance(g.get("tags"), list), "/api/games: tags is an array", repr(g.get("tags")))

    code, body = member.req("/api/news")
    check(code == 200, "/api/news returns 200", str(code))
    for n in (body or {}).get("news", []):
        check(all(k in n for k in ("id", "title", "summary", "url", "published_at")),
              "/api/news: item has id/title/summary/url/published_at", repr(n)[:120])

    # -- session (session.ts fetch) -------------------------------------------
    code, body = member.req("/api/auth/session")
    check(code == 200 and body == {"authenticated": False},
          "/api/auth/session: signed-out shape is exactly {authenticated:false}", repr(body))

    # -- register + login (AuthPanel submit) ----------------------------------
    code, body = member.req("/api/auth/register", "POST", {
        "username": username, "email": f"{username}@example.test",
        "password": "contract-pass-1", "display_name": "Contract Shape",
    })
    check(code == 201, "register returns 201", f"{code} {repr(body)[:120]}")
    check(body.get("registered") is True, "register: registered=true", repr(body)[:120])
    if body.get("verification_required"):
        check("email" in body, "register: verification path returns email")
        print("  note: verification required; member-scoped shape checks limited")
    else:
        check_user_shape(body.get("user") or {}, "register")
        check(body["user"]["role"] == "Member", "register: new member role is Member")

    code, body = member.req("/api/auth/login", "POST", {"identifier": username, "password": "contract-pass-1"})
    check(code == 200 and isinstance(body.get("user"), dict), "login returns {user}", repr(body)[:120])
    check_user_shape(body.get("user") or {}, "login")

    # wrong-password shape (AuthPanel error handling)
    code, body = Client().req("/api/auth/login", "POST", {"identifier": username, "password": "wrong"})
    check(code == 401 and isinstance(body.get("error"), dict) and "code" in body["error"],
          "login failure: {error:{code,message}} with 401", repr(body)[:120])

    # -- member surfaces (MemberSurfaces request<T> call sites) ---------------
    code, body = member.req("/api/account/summary")
    check(code == 200, "/api/account/summary returns 200", str(code))
    stats = (body or {}).get("stats")
    check(isinstance(stats, dict), "/api/account/summary: stats is an object", repr(stats))
    check(is_num((body or {}).get("user", {}).get("acoins_balance")),
          "/api/account/summary: includes numeric ACoins balance", repr((body or {}).get("user")))
    # THE regression: frontend reads stats.support_tickets; conversation_messages crashed the app
    check(isinstance(stats, dict) and is_num(stats.get("support_tickets")),
          "/api/account/summary: stats.support_tickets is a number (the conversation_messages regression)", repr(stats))
    check_user_shape((body or {}).get("user") or {}, "account summary")

    code, body = member.req("/api/support")
    check(code == 200 and isinstance((body or {}).get("tickets"), list),
          "/api/support: {tickets: [...]}")
    for t in (body or {}).get("tickets", []):
        check_ticket_summary(t, "/api/support")

    code, body = member.req("/api/support", "POST", {
        "subject": "Contract shape ticket", "message": "Checking the ticket shape.",
        "category": "other", "priority": "normal",
    })
    check(code == 201 and is_str((body or {}).get("id")),
          "ticket creation: 201 {id}", f"{code} {repr(body)[:120]}")
    ticket_id = (body or {}).get("id")

    code, body = member.req(f"/api/support/{ticket_id}")
    check(code == 200 and isinstance((body or {}).get("ticket"), dict) and isinstance((body or {}).get("replies"), list),
          "/api/support/:id: {ticket, replies[]}", repr(body)[:160])
    t = (body or {}).get("ticket") or {}
    check(all(k in t for k in ("id", "user_id", "subject", "status", "priority", "category", "message", "created_at", "updated_at")),
          "/api/support/:id: ticket has all detail fields", repr(t)[:120])

    code, body = member.req(f"/api/support/{ticket_id}/reply", "POST", {"message": "shape-check reply"})
    check(code == 200 and is_str((body or {}).get("id")), "reply: 200 {id}", f"{code} {repr(body)[:120]}")

    code, body = member.req("/api/notifications")
    check(code == 200 and isinstance((body or {}).get("notifications"), list) and is_num((body or {}).get("unread_count")),
          "/api/notifications: {notifications[], unread_count}", f"{code} {repr(body)[:120]}")
    for n in (body or {}).get("notifications", []):
        check(all(k in n for k in ("id", "kind", "subject", "message", "created_at", "read")),
              "notification items carry id/kind/subject/message/created_at/read", repr(n)[:120])

    code, body = member.req(f"/api/support/{ticket_id}")
    replies = (body or {}).get("replies") or []
    check(bool(replies), "reply appears in ticket detail")
    r = replies[0] if replies else {}
    check(all(k in r for k in ("id", "ticket_id", "message", "is_staff", "created_at")),
          "ticket replies carry id/ticket_id/message/is_staff/created_at", repr(r)[:120])

    # PUT /api/account — full required body (MemberSurfaces AccountForm)
    code, body = member.req("/api/account", "PUT", {
        "username": username, "email": f"{username}@example.test",
        "display_name": "Contract Shape Updated",
    })
    check(code == 200 and isinstance((body or {}).get("user"), dict),
          "PUT /api/account: 200 {user}", f"{code} {repr(body)[:120]}")
    check_user_shape((body or {}).get("user") or {}, "PUT /api/account")

    # PUT /api/account — a BLANK display_name must be rejected (400). (An absent
    # key falls back to the current display_name and succeeds, per proxy.py.)
    code, body = member.req("/api/account", "PUT", {"username": username, "email": f"{username}@example.test", "display_name": ""})
    check(code == 400, "PUT /api/account: blank display_name returns 400", str(code))

    # account summary after update reflects new display_name
    _, body = member.req("/api/account/summary")
    check((body or {}).get("user", {}).get("display_name") == "Contract Shape Updated",
          "PUT /api/account: update is reflected in subsequent reads")

    # -- admin surfaces (AdminSurface call sites) -----------------------------
    code, body = admin.req("/api/auth/login", "POST", {"identifier": "boss", "password": "admin-pass-1"})
    check(code == 200, "seeded admin login (mock) returns 200", str(code))
    admin_csrf = {"X-CSRF-Token": admin.csrf()}
    check(bool(admin.csrf()), "admin has CSRF cookie after login")

    code, body = admin.req("/api/admin/summary")
    check(code == 200, "/api/admin/summary returns 200", str(code))
    s = body or {}
    check(all(k in s for k in ("users", "active_users", "disabled_users", "open_tickets", "active_deploys", "active_projects")),
          "/api/admin/summary: has all AdminSummary fields", repr(s)[:160])

    code, body = admin.req("/api/admin/users?q=contract-shape")
    check(code == 200 and isinstance((body or {}).get("users"), list),
          "/api/admin/users: {users: [...]}", str(code))
    rows = (body or {}).get("users") or []
    target = next((u for u in rows if u.get("username") == username), None)
    check(target is not None, "/api/admin/users?q= finds the registered member")
    if target:
        check_admin_user(target, "/api/admin/users")

    code, body = admin.req("/api/admin/database/status")
    d = body or {}
    check("database" in d and "version" in d, "/api/admin/database/status: has database+version", repr(d)[:120])

    # Migration panel reads its state from this same response (no extra fetch).
    m = d.get("migrations")
    check(isinstance(m, dict), "/api/admin/database/status: carries migrations summary", repr(m)[:160])
    if isinstance(m, dict):
        check(is_bool(m.get("tracked")), "migrations: tracked is a boolean", repr(m.get("tracked")))
        check(is_str(m.get("tracking_table")), "migrations: tracking_table is a string", repr(m.get("tracking_table")))
        check(m.get("current_revision") is None or is_str(m.get("current_revision")),
              "migrations: current_revision is a string or null", repr(m.get("current_revision")))
        check(isinstance(m.get("applied"), list) and isinstance(m.get("pending"), list),
              "migrations: applied and pending are arrays", repr(m)[:160])
        check(isinstance(m.get("drifted"), list) and isinstance(m.get("missing"), list),
              "migrations: drifted and missing are arrays", repr(m)[:160])
        for entry in m.get("applied") or []:
            check(is_str(entry.get("filename")),
                  "migrations: applied entry has a filename", repr(entry)[:120])
        for entry in m.get("pending") or []:
            check(is_str(entry.get("filename")),
                  "migrations: pending entry has a filename", repr(entry)[:120])

    code, body = admin.req("/api/admin/database/migrations/apply", "POST", {}, admin_csrf)
    check(code == 200 and (body or {}).get("ok") is True and isinstance((body or {}).get("applied"), list),
          "POST /api/admin/database/migrations/apply: {ok, applied[]}", f"{code} {repr(body)[:120]}")
    code, body = admin.req("/api/admin/database/migrations/apply", "POST", {}, admin_csrf)
    check((body or {}).get("applied") == [],
          "apply is idempotent: a second run applies nothing", repr(body)[:120])

    code, body = admin.req("/api/admin/settings")
    check(code == 200 and isinstance(body, dict), "/api/admin/settings: 200 object", str(code))

    code, body = admin.req("/api/admin/payments/settings")
    check(code == 200 and (body or {}).get("client_secret_configured") is False,
          "payment settings never return the client secret", f"{code} {repr(body)[:120]}")
    code, body = admin.req("/api/admin/payments/settings", "POST", {
        "paypal_email": "merchant@example.test", "paypal_client_id": "mock-id",
        "paypal_client_secret": "mock-secret", "environment": "sandbox",
    }, admin_csrf)
    check(code == 200, "admin can save PayPal settings", str(code))
    _, body = admin.req("/api/admin/payments/settings")
    check((body or {}).get("client_secret_configured") is True and "paypal_client_secret" not in (body or {}),
          "saved client secret is redacted from settings reads", repr(body)[:160])

    # role change (AdminSurface updateUser)
    if target:
        uid = target["id"]
        code, body = admin.req("/api/admin/users/role", "POST", {"user_id": uid, "role": "Pro"}, admin_csrf)
        check(code == 200 and (body or {}).get("ok") is True, "role change: {ok:true}", f"{code} {repr(body)[:120]}")
        _, body = admin.req(f"/api/admin/users?q={username}")
        _, body = admin.req(f"/api/admin/users?q={username}")
        row = next((u for u in (body or {}).get("users", []) if u.get("username") == username), {})
        check(row.get("role") == "Pro", "role change persisted", repr(row)[:120])

        # status: disable then enable — THE is_active regression
        code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "disabled"}, admin_csrf)
        check(code == 200, "status disable accepted", str(code))
        _, body = admin.req(f"/api/admin/users?q={username}")
        row = next((u for u in (body or {}).get("users", []) if u.get("username") == username), {})
        check(row.get("is_active") in (0, False),
              "status disable actually disabled (the is_active regression)", repr(row)[:160])

        # the API's idempotency guard 409s same-user mutations within ~3s
        time.sleep(3.2)
        code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "active"}, admin_csrf)
        _, body = admin.req(f"/api/admin/users?q={username}")
        row = next((u for u in (body or {}).get("users", []) if u.get("username") == username), {})
        check(row.get("is_active") in (1, True),
              "status enable actually enabled (the is_active regression)", repr(row)[:160])

        # ban / unban via /ban with banned flag
        time.sleep(3.2)  # idempotency guard window
        code, body = admin.req("/api/admin/users/ban", "POST", {"user_id": uid, "banned": True}, admin_csrf)
        _, body = admin.req(f"/api/admin/users?q={username}")
        row = next((u for u in (body or {}).get("users", []) if u.get("username") == username), {})
        check(row.get("is_active") in (0, False) and row.get("banned_at"),
              "ban sets is_active=0 and banned_at", repr(row)[:160])

        time.sleep(3.2)  # idempotency guard window
        code, body = admin.req("/api/admin/users/ban", "POST", {"user_id": uid, "banned": False}, admin_csrf)
        _, body = admin.req(f"/api/admin/users?q={username}")
        row = next((u for u in (body or {}).get("users", []) if u.get("username") == username), {})
        check(row.get("is_active") in (1, True) and not row.get("banned_at"),
              "unban via banned:false clears is_active and banned_at (the always-bans regression)", repr(row)[:160])

    # -- notifications: staff reply surfaces, mark-read clears it ---------------
    code, body = admin.req(f"/api/admin/support/{ticket_id}/reply", "POST", {"message": "Staff follow-up"}, admin_csrf)
    check(code == 200 and is_str((body or {}).get("id")), "staff reply: 200 {id}", f"{code} {repr(body)[:120]}")

    code, body = member.req("/api/notifications")
    items = (body or {}).get("notifications") or []
    staff = [n for n in items if n.get("kind") == "ticket_reply" and n.get("ticket_id") == ticket_id]
    check(bool(staff) and (body or {}).get("unread_count", 0) >= 1,
          "staff reply surfaces as an unread ticket_reply notification", repr(body)[:160])
    for n in staff:
        check(n.get("id", "").startswith("reply:") and n.get("read") is False,
              "ticket_reply notification has a reply: ref and starts unread", repr(n)[:120])

    code, body = member.req("/api/notifications/read", "POST", {"ids": [staff[0]["id"]]} if staff else {"all": True})
    check(code == 200 and (body or {}).get("ok") is True,
          "POST /api/notifications/read marks ids read", f"{code} {repr(body)[:120]}")
    _, body = member.req("/api/notifications")
    remaining = [n for n in (body or {}).get("notifications", []) if not n.get("read")]
    check(all(n.get("id") != (staff[0]["id"] if staff else None) for n in remaining),
          "marked notification no longer unread")

    code, body = member.req("/api/notifications/read", "POST", {"all": True})
    check(code == 200 and (body or {}).get("ok") is True, "POST /api/notifications/read {all:true} works", str(code))
    _, body = member.req("/api/notifications")
    check((body or {}).get("unread_count") == 0, "mark-all-read clears unread_count", repr(body)[:120])

    code, _ = member.req("/api/notifications/read", "POST", {"ids": ["bogus"]})
    check(code == 400, "POST /api/notifications/read rejects bad ids (400)", str(code))

    # -- non-admin boundary (guards) -------------------------------------------
    code, body = member.req("/api/admin/users")
    check(code == 403, "member blocked from admin endpoints (403)", str(code))

    # -- member self-delete + session cleanup ----------------------------------
    code, body = member.req("/api/account", "DELETE", {"confirm": "DELETE"})
    check(code == 200 and (body or {}).get("deleted") is True,
          "DELETE /api/account: {deleted:true}", f"{code} {repr(body)[:120]}")
    code, body = member.req("/api/auth/session")
    check(body == {"authenticated": False},
          "session dead after account deletion (cookie expiry works)", repr(body))

    print()
    if failures:
        print(f"frontend contract tests: {len(failures)} FAILED, {passed} passed")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"frontend contract tests: PASS ({passed} checks)")


if __name__ == "__main__":
    run()
