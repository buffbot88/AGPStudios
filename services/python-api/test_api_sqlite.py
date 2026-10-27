#!/usr/bin/env python3
"""Smoke-test auth, account, support, telemetry, and payments settings on SQLite."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
import urllib.error
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG


def main():
    from cryptography.fernet import Fernet
    with tempfile.TemporaryDirectory() as raw:
        CONFIG["database_path"] = str(Path(raw) / "api.sqlite3")
        CONFIG["payment_encryption_key"] = Fernet.generate_key().decode()
        import migrate
        import proxy
        with proxy.db() as conn:
            with conn.cursor() as cur:
                migrate.ensure_tracking_table(cur)
            for migration in migrate.discover():
                migrate.apply_migration(conn, migration, "test")

        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        cookies = ""
        csrf = ""

        def request(path, method="GET", body=None, authenticated=False, origin=None, ip=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
            if ip:
                headers["X-Forwarded-For"] = ip
            if authenticated:
                headers.update({"Cookie": cookies, "X-CSRF-Token": csrf})
            data = json.dumps(body).encode() if body is not None else None
            req = Request(base + path, data=data, headers=headers, method=method)
            with urlopen(req, timeout=5) as response:
                payload = json.loads(response.read()) if response.length else {}
                return response.status, payload, response.headers

        try:
            origin = "https://everenfell.agpstudios.org"
            preflight = Request(base + "/api/auth/login", method="OPTIONS", headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            })
            with urlopen(preflight, timeout=5) as response:
                assert response.status == 204
                assert response.headers["Access-Control-Allow-Origin"] == origin
                assert response.headers["Access-Control-Allow-Credentials"] == "true"
            status, body, headers = request("/api/auth/session", origin=origin)
            assert status == 200 and headers["Access-Control-Allow-Origin"] == origin
            _, _, headers = request("/api/auth/session", origin="https://evil.example")
            assert headers.get("Access-Control-Allow-Origin") is None

            status, challenge, _ = request("/api/auth/register-challenge")
            assert status == 200 and challenge["challenge"] and isinstance(challenge["difficulty"], int)
            prefix = "0" * challenge["difficulty"]
            nonce = next(n for n in range(10_000_000)
                         if proxy.sha256(f"{challenge['challenge']}{n}".encode()).hexdigest().startswith(prefix))
            status, body, _ = request("/api/auth/register", "POST", {
                "username": "sqlitemember", "email": "sqlite@example.test",
                "password": "sqlite-pass-123", "display_name": "SQLite Member",
                "proof": {"challenge": challenge["challenge"], "nonce": str(nonce)}})
            assert status == 201 and body["registered"]
            try:
                status, body, _ = request("/api/auth/register", "POST", {
                    "username": "admin", "email": "reserved@example.test",
                    "password": "sqlite-pass-123", "display_name": "Reserved"})
            except urllib.error.HTTPError as exc:
                status, body = exc.code, json.loads(exc.read())
            assert status == 400 and body["error"]["code"] == "that_username_is_reserved"
            try:
                status, body, _ = request("/api/auth/register", "POST", {
                    "username": "sqlitejunk", "email": "junk@mailinator.com",
                    "password": "sqlite-pass-123", "display_name": "Junk"})
            except urllib.error.HTTPError as exc:
                status, body = exc.code, json.loads(exc.read())
            assert status == 400 and body["error"]["code"] == "disposable_email_addresses_are_not_accepted"
            status, body, headers = request("/api/auth/login", "POST", {
                "identifier": "sqlitemember", "password": "sqlite-pass-123"})
            assert status == 200
            set_cookies = headers.get_all("Set-Cookie")
            cookies = "; ".join(item.split(";", 1)[0] for item in set_cookies)
            csrf = next(item.split("=", 1)[1].split(";", 1)[0] for item in set_cookies if item.startswith("ashat_csrf="))

            status, body, _ = request("/api/account/summary", authenticated=True)
            assert status == 200 and body["user"]["acoins_balance"] == 0
            status, history, _ = request("/api/account/transactions", authenticated=True)
            assert status == 200 and history["transactions"] == []
            status, content, _ = request("/api/site/content")
            assert status == 200 and content["footer"]["tagline"] and content["featured_video"]["embed_url"]
            status, challenge, _ = request("/api/auth/register-challenge")
            assert status == 200 and challenge["difficulty"] == 4
            status, body, _ = request("/api/support", "POST", {
                "subject": "SQLite check", "message": "support row"}, authenticated=True)
            assert status == 201

            with proxy.db() as conn:
                conn.execute("UPDATE users SET role='Admin' WHERE username=?", ("sqlitemember",))
            status, site_settings, _ = request("/api/admin/site-settings", authenticated=True)
            assert status == 200 and site_settings["settings"]["register.budget"] == 5
            assert site_settings["settings"]["referral.bonus_acoins"] == 100
            assert site_settings["settings"]["register.max_accounts_per_ip"] == 2
            # -- referrals: permanent code, credit on signup, cap, daily limit --
            status, referral, _ = request("/api/account/referral", authenticated=True)
            assert status == 200 and referral["code"] and referral["uses_total"] == 0
            assert referral["remaining_24h"] == referral["uses_per_day"] == 10
            referrer_code = referral["code"]
            def register_as(prefix, ip, ref=None):
                status, chal, _ = request("/api/auth/register-challenge")
                assert status == 200
                nonce = next(n for n in range(10_000_000)
                             if proxy.sha256(f"{chal['challenge']}{n}".encode()).hexdigest().startswith("0" * chal["difficulty"]))
                body = {"username": f"{prefix}{ip.replace('.', '')}", "email": f"{prefix}{ip.replace('.', '')}@example.test",
                          "password": "sqlite-pass-123", "display_name": prefix,
                          "proof": {"challenge": chal["challenge"], "nonce": str(nonce)}}
                if ref:
                    body["referral_code"] = ref
                try:
                    status, payload, _ = request("/api/auth/register", "POST", body, ip=ip)
                except urllib.error.HTTPError as exc:
                    status, payload = exc.code, json.loads(exc.read())
                return status, payload
            status, referred = register_as("refd", "10.9.8.7", referrer_code)
            assert status == 201 and referred["referral"]["credited"] is True, (status, referred)
            assert referred["referral"]["bonus_acoins"] == 100 and referred["referral_code"]
            assert referred["referral_code"] != referrer_code
            status, summary, _ = request("/api/account/summary", authenticated=True)
            assert summary["user"]["acoins_balance"] == 100, summary
            status, history, _ = request("/api/account/transactions", authenticated=True)
            assert any(t["kind"] == "referral_bonus" and t["acoins_delta"] == 100 for t in history["transactions"]), history
            # second account from the same IP is allowed; the third trips the cap
            status, _ = register_as("second", "10.9.8.7")
            assert status == 201, status
            status, payload = register_as("third", "10.9.8.7")
            assert status == 409 and payload["error"]["code"] == "too_many_accounts_from_this_address", (status, payload)
            # ...but a different IP is unaffected
            status, _ = register_as("other", "10.9.8.8")
            assert status == 201, status
            # unknown codes still register, they just pay nothing
            status, unknown = register_as("nope", "10.9.8.9", "not-a-real-code")
            assert status == 201 and unknown["referral"] == {"credited": False, "reason": "unknown_referral_code"}, unknown
            # lower the daily limit to prove the 24h window without 11 signups
            status, _, _ = request("/api/admin/site-settings", "PUT", {
                "settings": {"referral.uses_per_day": 1}}, authenticated=True)
            assert status == 200
            status, limited = register_as("late", "10.9.8.10", referrer_code)
            assert status == 201 and limited["referral"] == {"credited": False, "reason": "referral_limit_reached"}, limited
            status, _, _ = request("/api/admin/site-settings", "PUT", {
                "settings": {"referral.uses_per_day": 10}}, authenticated=True)
            assert status == 200
            status, referral, _ = request("/api/account/referral", authenticated=True)
            assert referral["uses_total"] == 1 and referral["rewards_earned_acoins"] == 100, referral
            # -- wall stats: signups, profits, per-game activity --
            status, stats, _ = request("/api/admin/stats", authenticated=True)
            assert status == 200, status
            assert stats["signups"]["total"] >= 6, stats["signups"]
            assert stats["signups"]["today"] >= 6, stats["signups"]
            assert stats["profits_cents"] == {"day": 0, "week": 0, "month": 0, "year": 0}, stats["profits_cents"]
            slugs = {game["slug"] for game in stats["games"]}
            assert {"paws-and-parcels", "everenfell"} <= slugs, slugs
            for game in stats["games"]:
                assert game["acoins_spent"] >= 0 and game["transactions"] >= 0, game
            everenfell = next(game for game in stats["games"] if game["slug"] == "everenfell")
            assert everenfell["entitlements"] == 0, everenfell
            assert stats["online"]["website"] >= 0, stats["online"]
            status, _, _ = request("/api/admin/site-settings", "PUT", {
                "settings": {"footer.tagline": "SQLite Tagline", "register.budget": 7}}, authenticated=True)
            assert status == 200
            status, site_settings, _ = request("/api/admin/site-settings", authenticated=True)
            assert site_settings["settings"]["footer.tagline"] == "SQLite Tagline"
            status, pinned, _ = request("/api/admin/news", "POST", {
                "title": "SQLite pinned", "summary": "Pinned by the SQLite suite."}, authenticated=True)
            assert status == 200 and pinned["id"]
            status, _, _ = request(f"/api/admin/news/{pinned['id']}", "PUT", {
                "title": "SQLite pinned (edited)"}, authenticated=True)
            assert status == 200
            status, curated, _ = request("/api/admin/news", authenticated=True)
            assert any(item["id"] == pinned["id"] and item["title"].endswith("(edited)") for item in curated["news"])
            status, _, _ = request(f"/api/admin/news/{pinned['id']}", "DELETE", authenticated=True)
            assert status == 200
            status, game, _ = request("/api/admin/games/paws-and-parcels", "PUT", {
                "featured": True, "sort_order": 1}, authenticated=True)
            assert status == 200 and game["game"]["slug"] == "paws-and-parcels"
            status, _, _ = request("/api/admin/database/status", authenticated=True)
            assert status == 200
            status, settings, _ = request("/api/admin/payments/settings", authenticated=True)
            assert status == 200 and not settings["client_secret_configured"]
            status, _, _ = request("/api/admin/payments/settings", "POST", {
                "paypal_email": "merchant@example.test", "paypal_client_id": "test-id",
                "paypal_client_secret": "test-secret", "environment": "sandbox"}, authenticated=True)
            assert status == 200
            status, settings, _ = request("/api/admin/payments/settings", authenticated=True)
            assert settings["client_secret_configured"] and "paypal_client_secret" not in settings
            with proxy.db() as conn:
                saved = conn.execute("SELECT paypal_client_secret FROM acoin_payment_settings WHERE id=1").fetchone()["paypal_client_secret"]
                assert proxy.payment_cipher().decrypt(saved.encode()).decode() == "test-secret"

            status, _, _ = request("/api/telemetry/client-error", "POST", {"message": "sqlite failure"})
            assert status == 202
            status, report, _ = request("/api/admin/client-errors?status=new", authenticated=True)
            assert status == 200 and report["errors"][0]["message_hash"]
            report_id = report["errors"][0]["id"]
            status, _, _ = request("/api/admin/client-errors/status", "POST", {
                "id": report_id, "status": "resolved"}, authenticated=True)
            assert status == 200
            status, _, _ = request("/api/admin/client-errors/mute", "POST", {
                "id": report_id, "muted": True}, authenticated=True)
            assert status == 200
            status, muted, _ = request("/api/admin/client-errors?status=muted", authenticated=True)
            assert status == 200 and muted["errors"][0]["triage_status"] == "muted"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("SQLite API auth, account, support, admin, and payment-settings smoke test: PASS")


if __name__ == "__main__":
    main()
