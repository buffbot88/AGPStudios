#!/usr/bin/env python3
"""Smoke-test auth, account, support, telemetry, and payments settings on SQLite."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
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

        def request(path, method="GET", body=None, authenticated=False, origin=None):
            headers = {"Content-Type": "application/json"}
            if origin:
                headers["Origin"] = origin
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

            status, body, _ = request("/api/auth/register", "POST", {
                "username": "sqlitemember", "email": "sqlite@example.test",
                "password": "sqlite-pass-123", "display_name": "SQLite Member"})
            assert status == 201 and body["registered"]
            status, body, headers = request("/api/auth/login", "POST", {
                "identifier": "sqlitemember", "password": "sqlite-pass-123"})
            assert status == 200
            set_cookies = headers.get_all("Set-Cookie")
            cookies = "; ".join(item.split(";", 1)[0] for item in set_cookies)
            csrf = next(item.split("=", 1)[1].split(";", 1)[0] for item in set_cookies if item.startswith("ashat_csrf="))

            status, body, _ = request("/api/account/summary", authenticated=True)
            assert status == 200 and body["user"]["acoins_balance"] == 0
            status, body, _ = request("/api/support", "POST", {
                "subject": "SQLite check", "message": "support row"}, authenticated=True)
            assert status == 201

            with proxy.db() as conn:
                conn.execute("UPDATE users SET role='Admin' WHERE username=?", ("sqlitemember",))
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
