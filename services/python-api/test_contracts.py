#!/usr/bin/env python3
"""API contract smoke tests.

Phase 1 (always): read-only checks — discovery, JWKS, session, public
homepage content, auth boundaries, authorize validation. No credentials or
data mutations required; safe against production.

Phase 2 (opt-in via ASHAT_TEST_MUTATIONS=1): asserts the request-body shapes
for admin user mutations (POST /api/admin/users/role|status|ban), the PUT
/api/account requirements, and DELETE /api/account self-deletion. Requires a
seeded admin (ASHAT_TEST_ADMIN / ASHAT_TEST_ADMIN_PASSWORD, default the mock's
boss / admin-pass-1) and creates one throwaway member, then cleans up after
itself. Run against the mock or a credentialed environment — never against a
deployment you don't own.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = os.environ.get("ASHAT_TEST_BASE", "https://www.agpstudios.org")
MUTATIONS = os.environ.get("ASHAT_TEST_MUTATIONS") == "1"
ADMIN_USER = os.environ.get("ASHAT_TEST_ADMIN", "boss")
ADMIN_PASSWORD = os.environ.get("ASHAT_TEST_ADMIN_PASSWORD", "admin-pass-1")

def get(path, expected=200):
    try:
        response = urllib.request.urlopen(BASE + path, timeout=15)
    except urllib.error.HTTPError as exc:
        assert exc.code == expected, (path, exc.code)
        return exc.code, exc.headers, exc.read()
    assert response.status == expected, (path, response.status)
    return response.status, response.headers, response.read()

def main():
    _, headers, body = get("/api/oauth/.well-known/openid-configuration")
    discovery = json.loads(body)
    assert discovery["issuer"] == BASE + "/api/oauth"
    assert discovery["code_challenge_methods_supported"] == ["S256"]
    assert discovery["id_token_signing_alg_values_supported"] == ["RS256"]

    _, _, body = get("/api/oauth/.well-known/jwks.json")
    keys = json.loads(body)["keys"]
    assert len(keys) == 1 and keys[0]["alg"] == "RS256" and keys[0]["kty"] == "RSA"

    _, _, body = get("/api/auth/session")
    assert json.loads(body) == {"authenticated": False}

    _, _, body = get("/api/game-status")
    status = json.loads(body)
    assert status["source"] == "paws-and-parcels"
    assert isinstance(status["online"], bool)
    _, _, body = get("/api/portal-data")
    portal = json.loads(body)
    assert set(("health", "players_online", "server_metrics", "season", "character", "news", "regions")) <= set(portal)

    _, _, body = get("/api/games")
    games = json.loads(body)["games"]
    assert isinstance(games, list)
    for game in games:
        assert set(("slug", "title", "summary", "status", "tags", "url", "poster", "featured")) <= set(game)
        assert game["status"] in ("live", "in-development", "coming-soon")
        assert isinstance(game["tags"], list)

    _, _, body = get("/api/news")
    news = json.loads(body)["news"]
    assert isinstance(news, list)
    for item in news:
        assert set(("id", "title", "summary", "url", "published_at")) <= set(item)

    status, headers, body = get("/api/support", expected=401)
    assert json.loads(body)["error"]["code"] == "authentication_required"
    get("/api/account/summary", expected=401)
    _, _, body = get("/api/admin/users", expected=403)
    assert json.loads(body)["error"]["code"] == "admin_authentication_required"
    request = urllib.request.Request(BASE + "/api/account", data=b'{"confirm":"DELETE"}', headers={"Content-Type":"application/json"}, method="DELETE")
    try: urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as exc: assert exc.code == 401
    get("/api/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "invalid", "client_id": "contract-test",
        "redirect_uri": "https://pawsandparcels.agpstudios.org/callback", "state": "contract-test",
    }), expected=400)
    print("phase 1 contract smoke tests: PASS")

    if MUTATIONS:
        phase2()


class Client:
    """A cookie-carrying client (mirrors the frontend's request() helper: CSRF
    header on mutations)."""

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if method in ("POST", "PUT", "DELETE"):
            headers["X-CSRF-Token"] = self.csrf()
        request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
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


def row_for(users, username):
    return next((u for u in (users or {}).get("users", []) if u.get("username") == username), None)


def phase2():
    import secrets
    print("phase 2: request-shape assertions (mutations enabled)")
    admin = Client()
    code, body = admin.req("/api/auth/login", "POST", {"identifier": ADMIN_USER, "password": ADMIN_PASSWORD})
    assert code == 200, ("admin login", code, body)
    code, body = admin.req("/api/admin/users")
    assert code == 200, ("admin users list", code)

    # -- create a throwaway member via the mock-compatible register shape ----
    member = Client()
    username = f"smoke-shape-{secrets.token_hex(4)}"
    code, body = member.req("/api/auth/register", "POST", {
        "username": username, "email": f"{username}@example.test",
        "password": "smoke-pass-1", "display_name": "Smoke Shape",
    })
    assert code == 201, ("register", code, body)
    code, body = member.req("/api/auth/login", "POST", {"identifier": username, "password": "smoke-pass-1"})
    assert code == 200 and isinstance((body or {}).get("user"), dict), ("member login", code)

    code, body = admin.req("/api/admin/users?q=" + username)
    target = row_for(body, username)
    assert target, "registered member appears in admin user list"
    uid = target["id"]

    # -- POST /api/admin/users/role: {user_id, role} -------------------------
    code, body = admin.req("/api/admin/users/role", "POST", {"user_id": uid, "role": "Pro"})
    assert code == 200 and (body or {}).get("ok") is True, ("role accept", code, body)
    code, body = admin.req("/api/admin/users?q=" + username)
    assert row_for(body, username)["role"] == "Pro", "role change persisted"
    code, body = admin.req("/api/admin/users/role", "POST", {"user_id": uid, "role": "SuperAdmin"})
    assert code == 400, ("invalid role rejected 400", code)

    # -- POST /api/admin/users/status: {user_id, status} ---------------------
    import time
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "disabled"})
    assert code == 200, ("status disable", code, body)
    # idempotency guard: an immediate duplicate must 409 without applying twice
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "disabled"})
    assert code == 409, ("duplicate status rejected 409", code, body)
    time.sleep(3.2)  # let the window expire before the next mutation
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "disabled"})
    assert code == 200, ("status disable after window", code, body)
    code, body = admin.req("/api/admin/users?q=" + username)
    row = row_for(body, username)
    assert row.get("is_active") in (0, False), "status=disabled actually disables (is_active regression)"
    time.sleep(3.2)  # guard window
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "active"})
    assert code == 200, ("status enable", code)
    code, body = admin.req("/api/admin/users?q=" + username)
    row = row_for(body, username)
    assert row.get("is_active") in (1, True), "status=active re-enables"
    # status=banned must also work via /status (docs: sets is_active=0 + banned_at)
    time.sleep(3.2)  # guard window
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "banned"})
    assert code == 200, ("status=banned accepted", code, body)
    code, body = admin.req("/api/admin/users?q=" + username)
    row = row_for(body, username)
    assert row.get("is_active") in (0, False) and row.get("banned_at"), \
        "status=banned sets is_active=0 and banned_at (docs)"
    time.sleep(3.2)  # guard window before the /ban section takes over the user
    code, body = admin.req("/api/admin/users/status", "POST", {"user_id": uid, "status": "active"})
    assert code == 200, ("status restore after banned", code, body)

    # -- POST /api/admin/users/ban: {user_id, banned: bool} -------------------
    # banned defaults to true when omitted (docs: a body of only {user_id} bans)
    code, body = admin.req("/api/admin/users/ban", "POST", {"user_id": uid})
    assert code == 200, ("ban with omitted banned defaults true", code, body)
    code, body = admin.req("/api/admin/users?q=" + username)
    row = row_for(body, username)
    assert row.get("is_active") in (0, False) and row.get("banned_at"), \
        "omitted banned defaults to banning (docs backward-compat)"
    # explicit banned:false is the unban path — the always-bans regression
    import time as _t
    _t.sleep(3.2)  # guard window between ban and unban on the same user
    code, body = admin.req("/api/admin/users/ban", "POST", {"user_id": uid, "banned": False})
    assert code == 200, ("unban", code, body)
    code, body = admin.req("/api/admin/users?q=" + username)
    row = row_for(body, username)
    assert row.get("is_active") in (1, True) and not row.get("banned_at"), "banned:false unbans (always-bans regression)"
    # duplicate submission within the idempotency window must 409 (docs)
    code, body = admin.req("/api/admin/users/ban", "POST", {"user_id": uid, "banned": False})
    assert code == 409, ("duplicate ban rejected 409", code, body)
    _t.sleep(3.2)  # clear the guard before the moderation-timeline checks
    # an idempotency 409 must NOT have recorded a timeline event (docs)
    code, body = admin.req(f"/api/admin/users/{uid}/moderation")
    events = (body or {}).get("events") or []
    unbans = [e for e in events if e.get("action") == "user.unbanned"]
    assert len(unbans) == 1, ("409 recorded no duplicate timeline event", len(unbans))

    # -- GET /api/admin/users/:id/moderation: timeline ------------------------
    code, body = admin.req(f"/api/admin/users/{uid}/moderation")
    assert code == 200, ("moderation timeline", code, body)
    events = (body or {}).get("events")
    assert isinstance(events, list) and len(events) >= 3, ("timeline has events", events)
    actions = [e.get("action") for e in events]
    for expected in ("user.role.updated", "user.status.updated", "user.banned", "user.unbanned"):
        assert expected in actions, ("timeline missing action", expected, actions)
    newest, oldest = events[0], events[-1]
    assert (newest.get("created_at"), newest.get("id")) >= (oldest.get("created_at"), oldest.get("id")), \
        "timeline ordered newest first"
    for event in events:
        assert all(k in event for k in ("id", "user_id", "action", "actor_id", "actor_name", "detail", "created_at")), \
            ("event shape", event)

    # -- Migration panel: status summary + apply (admin CP) -------------------
    code, body = admin.req("/api/admin/database/status")
    assert code == 200, ("database status", code)
    summary = (body or {}).get("migrations")
    assert isinstance(summary, dict), ("database/status carries migrations", repr(body)[:200])
    for key in ("tracked", "tracking_table", "current_revision", "applied", "pending", "drifted", "missing"):
        assert key in summary, ("migrations summary key", key)
    assert isinstance(summary["applied"], list) and isinstance(summary["pending"], list), \
        ("applied/pending are lists", summary)
    for entry in summary["applied"]:
        assert all(k in entry for k in ("filename", "applied_at", "statements")), ("applied entry shape", entry)
    for entry in summary["pending"]:
        assert "filename" in entry, ("pending entry shape", entry)

    # Applying when nothing is pending must be a no-op, not an error
    code, body = admin.req("/api/admin/database/migrations/apply", "POST", {})
    assert code == 200 and (body or {}).get("ok") is True, ("apply migrations", code, body)
    assert isinstance((body or {}).get("applied"), list), ("apply returns an applied list", body)
    applied_now = body["applied"]
    code, body = admin.req("/api/admin/database/migrations/apply", "POST", {})
    assert code == 200 and (body or {}).get("applied") == [], \
        ("second apply is idempotent (nothing pending)", body)
    if applied_now:
        code, body = admin.req("/api/admin/database/status")
        summary_after = (body or {}).get("migrations", {})
        assert not summary_after.get("pending"), ("pending cleared after apply", summary_after.get("pending"))

    # -- GET /api/admin/games + PUT /api/admin/games/:slug (poster swap) ------
    code, body = admin.req("/api/admin/games")
    assert code == 200 and isinstance((body or {}).get("games"), list) and body["games"], \
        ("admin games list", code, body)
    game = body["games"][0]
    assert "poster" in game and "slug" in game, ("admin game row shape", game)
    old_poster = game["poster"]
    new_poster = old_poster if old_poster else "/contract-test-poster.svg"
    code, body = admin.req(f"/api/admin/games/{game['slug']}", "PUT", {"poster": new_poster})
    assert code == 200 and (body or {}).get("ok") is True, ("poster update", code, body)
    code, body = member.req("/api/games")
    pub = next((g for g in (body or {}).get("games", []) if g["slug"] == game["slug"]), None)
    assert pub and pub["poster"] == new_poster, "poster swap reflected on public /api/games (no migration)"
    # restore the original art so the suite is repeatable
    code, body = admin.req(f"/api/admin/games/{game['slug']}", "PUT", {"poster": old_poster})
    assert code == 200, ("poster restore", code, body)
    # error paths
    code, body = admin.req("/api/admin/games/does-not-exist", "PUT", {"poster": "/x.svg"})
    assert code == 404, ("unknown slug 404", code, body)
    code, body = admin.req(f"/api/admin/games/{game['slug']}", "PUT", {})
    assert code == 400, ("missing poster 400", code, body)

    # -- PUT /api/account: {username, email, display_name} required -----------
    code, body = member.req("/api/account", "PUT", {
        "username": username, "email": f"{username}@example.test",
        "display_name": "Smoke Shape Updated",
    })
    assert code == 200 and isinstance((body or {}).get("user"), dict), ("PUT account", code, body)
    assert (body or {}).get("user", {}).get("display_name") == "Smoke Shape Updated", "PUT reflected in response"
    code, body = member.req("/api/account/summary")
    assert (body or {}).get("user", {}).get("display_name") == "Smoke Shape Updated", "PUT reflected in later reads"
    # blank display_name must 400 (absent key falls back to current value)
    code, body = member.req("/api/account", "PUT", {"username": username, "email": f"{username}@example.test", "display_name": ""})
    assert code == 400, ("blank display_name rejected 400", code)

    # -- DELETE /api/account: {confirm: 'DELETE'} -----------------------------
    code, body = member.req("/api/account", "DELETE", {"confirm": "WRONG"})
    assert code == 400, ("bad confirm rejected 400", code)
    code, body = member.req("/api/account", "DELETE", {"confirm": "DELETE"})
    assert code == 200 and (body or {}).get("deleted") is True, ("DELETE account", code, body)
    code, body = member.req("/api/auth/session")
    assert (body or {}) == {"authenticated": False}, "session dead after account deletion"

    # -- client error telemetry: public write, admin read --------------------
    guest = Client()
    report = {"source": "window.onerror", "message": "contract telemetry boom",
              "detail": "Support couldn't load · MemberSurfaces (x.tsx:1:1)", "count": 3,
              "occurrences": ["22:24:04 · a", "22:24:02 · b"], "path": "/?utm=junk#frag"}
    code, body = guest.req("/api/telemetry/client-error", "POST", report)
    assert code == 202 and (body or {}).get("logged") is True, ("telemetry accepted", code, body)

    # Signed-in reports carry the user id, which the client never sends.
    code, body = admin.req("/api/telemetry/client-error", "POST",
                           dict(report, message="contract telemetry signed in"))
    assert code == 202, ("telemetry accepts a session", code, body)

    code, body = guest.req("/api/telemetry/client-error", "POST", {"source": "boundary"})
    assert code == 400, ("telemetry requires a message", code, body)

    code, body = guest.req("/api/telemetry/client-error", "POST", {"message": "x" * 9000})
    assert code == 413, ("telemetry caps body size", code, body)

    code, body = guest.req("/api/telemetry/client-error", "POST", "not-an-object")
    assert code == 400, ("telemetry rejects a non-object body", code, body)

    # Shape drift uses the same endpoint with its own source tag.
    code, body = guest.req("/api/telemetry/client-error", "POST",
                           {"source": "shape-drift", "message": "safeNumber expected finite number, got string",
                            "detail": "account summary · MemberSurfaces.tsx:135", "count": 2, "path": "/#account"})
    assert code == 202, ("telemetry accepts shape-drift reports", code, body)

    code, body = admin.req("/api/admin/client-errors")
    assert code == 200, ("admin client-errors list", code, body)
    rows = (body or {}).get("errors") or []
    assert (body or {}).get("page_size") == 50, ("client-errors pagination", body)
    first = next((row for row in rows if row.get("message") == "contract telemetry boom"), None)
    assert first, ("reported error is readable by an admin", rows[:3])
    assert all(key in first for key in ("id", "source", "message", "detail", "path", "count",
                                        "occurrences", "user_id", "user_agent", "created_at")), \
        ("client-error row shape", first)
    assert first["path"] == "/", ("query string and fragment stripped from path", first["path"])
    assert first["count"] == 3 and first["user_id"] == "", \
        ("count kept, signed-out report has no user", first["count"], first["user_id"])
    assert json.loads(first["occurrences"]) == ["22:24:04 · a", "22:24:02 · b"], \
        ("occurrences round-trip as JSON text", first["occurrences"])
    signed_in = next((row for row in rows if row.get("message") == "contract telemetry signed in"), None)
    assert signed_in and signed_in["user_id"], ("signed-in report carries the user id", signed_in)
    drift = next((row for row in rows if row.get("source") == "shape-drift"), None)
    assert drift and drift["path"] == "/" and drift["count"] == 2, \
        ("shape-drift source round-trips", drift)

    # Budget: the 21st report inside the window is refused (documented as 429).
    codes = [guest.req("/api/telemetry/client-error", "POST", {"message": "budget probe"})[0]
             for _ in range(30)]
    assert 429 in codes, ("telemetry rate limits per IP", codes[:5], codes.count(429))
    assert codes.count(202) <= 25, ("budget is bounded", codes.count(202))

    print("phase 2 contract smoke tests: PASS")

if __name__ == "__main__":
    main()
