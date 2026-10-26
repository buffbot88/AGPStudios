#!/usr/bin/env python3
"""Fixed AshatHub admin probe: correct cookies + CSRF + seed verification.

Why the old probe 403'd:
- admin routes need Cookie `ashat_rust_sid=<RAW token>`, not the sha256 hash
  stored in `rust_sessions.session_hash`, and not Everenfell's `evf_session`.
- GET /api/admin/* needs only the session cookie (no CSRF).
  POST /api/admin/* needs BOTH the cookie AND `X-CSRF-Token: <RAW csrf>`
  where sha256(raw_csrf) == rust_sessions.csrf_hash.
- `Secure; Domain=agpstudios.org` cookies never send to http://127.0.0.1,
  so curl without an explicit Cookie header always 403s.

Usage:
  python3 probe-admin.py --base http://127.0.0.1:8001 --login boss --password admin-pass-1
  python3 probe-admin.py --base http://127.0.0.1:8001 --session RAW --csrf RAW
  python3 probe-admin.py --base http://127.0.0.1:8001 --session RAW --csrf RAW --verify-seed --db /path/to/api.db
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from urllib.request import Request, urlopen

TIMEOUT = 10


def parse_set_cookies(headers):
    out = {}
    # get_all for duplicate Set-Cookie headers (py3.11+ has get_all, else fallback)
    try:
        raw = headers.get_all("Set-Cookie") or []
    except AttributeError:
        raw = [v for k, v in headers.items() if k.lower() == "set-cookie"]
    for line in raw:
        pair = line.split(";", 1)[0]
        if "=" in pair:
            k, v = pair.strip().split("=", 1)
            out[k] = v
    return out


def req(method, url, body=None, cookies=None, csrf=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if csrf:
        headers["X-CSRF-Token"] = csrf
    r = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(r, timeout=TIMEOUT) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except Exception as exc:
        # HTTPError carries status + body
        if hasattr(exc, "code") and hasattr(exc, "read"):
            try:
                return exc.code, dict(exc.headers), exc.read()
            except Exception:
                return exc.code, {}, b""
        raise


def verify_seed(db_path, raw_session):
    h = hashlib.sha256(raw_session.encode()).hexdigest()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT s.session_hash, s.csrf_hash, s.expires_at,"
            " s.expires_at > CURRENT_TIMESTAMP AS alive,"
            " u.id, u.username, u.role, u.is_active"
            " FROM rust_sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.session_hash = ?",
            (h,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        print(f"SEED FAIL: no rust_sessions row with session_hash=sha256(raw)={h[:16]}...")
        print("  You probably seeded the hash itself as the cookie value. Send the RAW token.")
        return False
    print(f"seed row: user={row['username']} role={row['role']} "
          f"is_active={row['is_active']} alive={bool(row['alive'])} expires_at={row['expires_at']}")
    ok = True
    if not row["alive"]:
        print("SEED FAIL: expires_at <= CURRENT_TIMESTAMP (expired). Re-seed with "
              "datetime('now','+7200 seconds').")
        ok = False
    if not row["is_active"]:
        print("SEED FAIL: users.is_active=0.")
        ok = False
    if str(row["role"] or "").lower() != "admin":
        print(f"SEED FAIL: role={row['role']!r} is not admin (check is case-insensitive).")
        ok = False
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--login", help="username for POST /api/auth/login")
    ap.add_argument("--password", default="")
    ap.add_argument("--session", help="raw ashat_rust_sid value (seeded or from login)")
    ap.add_argument("--csrf", help="raw ashat_csrf value")
    ap.add_argument("--verify-seed", action="store_true")
    ap.add_argument("--db", help="sqlite path for --verify-seed")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    session, csrf = args.session, args.csrf

    if args.login and not session:
        status, headers, body = req("POST", base + "/api/auth/login",
                                    {"identifier": args.login, "password": args.password})
        print(f"login -> {status} {body[:200]!r}")
        if status != 200:
            print("LOGIN FAILED: check identifier/password, users.is_active=1.")
            return 1
        cookies = parse_set_cookies(headers)
        session = cookies.get("ashat_rust_sid", "")
        csrf = cookies.get("ashat_csrf", "")
        print(f"captured cookies: ashat_rust_sid={'yes' if session else 'MISSING'} "
              f"ashat_csrf={'yes' if csrf else 'MISSING'}")
        if not session:
            print("LOGIN BUG: no ashat_rust_sid Set-Cookie. Are you hitting the mock (port 3110)"
                  " instead of proxy.py?")
            return 1

    if not session:
        print("No session. Pass --login/--password or --session/--csrf.")
        return 2

    if args.verify_seed:
        if not args.db:
            print("--verify-seed needs --db /path/to/api.db")
            return 2
        if not verify_seed(args.db, session):
            return 1

    cookies = {"ashat_rust_sid": session}
    if csrf:
        cookies["ashat_csrf"] = csrf

    # Step 1: session check (no CSRF needed, proves cookie is recognized)
    status, _, body = req("GET", base + "/api/auth/session", cookies=cookies)
    print(f"GET /api/auth/session -> {status} {body[:300]!r}")
    try:
        doc = json.loads(body or b"{}")
    except ValueError:
        doc = {}
    if not doc.get("authenticated"):
        print("SESSION NOT RECOGNIZED: raw token wrong / expired / is_active=0."
              " Do NOT send the sha256 hash; send the raw token."
              " Secure+Domain cookies also never reach http://127.0.0.1 — set the"
              " Cookie header explicitly like this probe does.")
        return 1

    # Step 2: admin GET (session only, no CSRF required by admin_get)
    status, _, body = req("GET", base + "/api/admin/database/status", cookies=cookies)
    print(f"GET /api/admin/database/status -> {status} {body[:300]!r}")
    if status == 403 and (b"admin_authentication_required" in body or b"admin_session_required" in body or b"admin_role_required" in body or b"admin session required" in body or b"admin role required" in body):
        print("ADMIN 403: session valid but role is not admin (admin role required), or no session (admin session required). Promote users.role='admin' or re-login.")
        return 1
    if status != 200:
        print("ADMIN GET FAILED: unexpected status (wanted 200).")
        return 1

    # Step 3: admin POST (needs X-CSRF-Token too)
    if not csrf:
        print("SKIP admin POST: no --csrf given, and POST requires X-CSRF-Token.")
        print("OK (GET path verified).")
        return 0
    status, _, body = req("POST", base + "/api/admin/database/migrations/apply",
                           {"baseline": False}, cookies=cookies, csrf=csrf)
    print(f"POST migrations/apply -> {status} {body[:300]!r}")
    if status == 403 and (b"csrf_failed" in body or b"csrf token required" in body):
        print("CSRF 403: X-CSRF-Token does not match sha256 -> csrf_hash."
              " Send the RAW csrf paired with this session (from the same login).")
        return 1
    if status in (200, 409):
        print("OK: admin session + CSRF both accepted.")
        return 0
    print("POST returned non-200; session path is fine, check payload/permissions.")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
