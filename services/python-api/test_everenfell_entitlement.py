import hashlib
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate
import proxy
from database import db


def test_entitlement_admin_flow():
    with tempfile.TemporaryDirectory() as raw:
        proxy.CONFIG["database_path"] = str(Path(raw) / "agp.sqlite3")
        with db() as conn:
            with conn.cursor() as cur:
                migrate.ensure_tracking_table(cur)
            for migration in migrate.discover():
                migrate.apply_migration(conn, migration, "test")
            conn.execute(
                "INSERT INTO users(id,username,email,password_hash,role) VALUES(?,?,?,?,?)",
                ("admin-id", "admin", "admin@example.test", "unused", "Admin"),
            )
            conn.execute(
                "INSERT INTO users(id,username,email,password_hash,role) VALUES(?,?,?,?,?)",
                ("member-id", "member", "member@example.test", "unused", "Member"),
            )
            conn.execute(
                "INSERT INTO rust_sessions(session_hash,user_id,csrf_hash,created_at,last_seen,expires_at) "
                "VALUES(?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,datetime('now','+1 hour'))",
                (hashlib.sha256(b"session").hexdigest(), "admin-id", hashlib.sha256(b"csrf").hexdigest()),
            )

        proxy.RECENT_MUTATIONS.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), proxy.Proxy)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"

        def request(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Cookie": "ashat_rust_sid=session"}
            if data is not None:
                headers.update({"Content-Type": "application/json", "X-CSRF-Token": "csrf"})
            req = Request(base + path, data=data, headers=headers, method=method)
            try:
                with urlopen(req, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as exc:
                return exc.code, json.loads(exc.read())

        try:
            status, _ = request("/api/admin/everenfell/s1", "POST", {
                "user_id": "member-id", "enabled": True,
            })
            assert status == 400
            status, body = request("/api/admin/everenfell/s1", "POST", {
                "user_id": "admin-id", "enabled": True,
            })
            assert status == 200 and body["ok"]
            status, body = request("/api/admin/users?q=admin")
            assert status == 200 and body["users"][0]["everenfell_s1"] == 1
            status, body = request("/api/admin/everenfell/s1", "POST", {
                "user_id": "admin-id", "enabled": False,
            })
            assert status == 200 and body["ok"]
            status, body = request("/api/admin/users?q=admin")
            assert status == 200 and body["users"][0]["everenfell_s1"] == 0
            status, _ = request("/api/admin/everenfell/s1", "POST", {
                "user_id": "admin-id", "enabled": "true",
            })
            assert status == 400
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    test_entitlement_admin_flow()
    print("Everenfell entitlement grant/revoke and admin authorization: PASS")
