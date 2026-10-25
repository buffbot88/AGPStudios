#!/usr/bin/env python3
"""Python API for the retained Ashat Hub surface."""
from base64 import b64encode, urlsafe_b64encode, urlsafe_b64decode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
import crypt, html, json, os, re, secrets, subprocess, sys, threading, time
from cryptography.fernet import Fernet

# The migration runner lives next to this file; import it by path so the API
# works regardless of the process working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate

from database import db
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import CONFIG, path as config_path

ISSUER = CONFIG["oidc_issuer"]
KEY_FILE = str(config_path(CONFIG["oidc_private_key_file"])) if CONFIG["oidc_private_key_file"] else ""
CODES = {}
OIDC_CODES_LOCK = threading.Lock()
LOGIN_ATTEMPTS = {}
# Idempotency guard for admin user mutations: (path, user_id) -> last submit
# time. Double-clicks and retried POSTs within the window get 409 instead of
# re-applying the mutation and writing a second audit event.
IDEMPOTENCY_WINDOW = 3.0  # seconds
RECENT_MUTATIONS = {}

# Public client-error reporting: a per-IP budget for the endpoint, plus a
# process-wide stamp so retention pruning runs at most once an hour.
CLIENT_ERROR_ATTEMPTS = {}
CLIENT_ERROR_BUDGET = 20           # reports per IP per window
CLIENT_ERROR_WINDOW = 60.0         # seconds
CLIENT_ERROR_MAX_BODY = 8192       # bytes
CLIENT_ERROR_RETENTION = 30 * 86400
CLIENT_ERROR_PRUNED_AT = 0.0
GAME_TICKET_ATTEMPTS = {}
GAME_TICKET_LOCK = threading.Lock()
GAME_TICKET_WINDOW = 300
GAME_TICKET_LIMIT = 10
ONLINE_VISITORS = {}
ONLINE_VISITORS_LOCK = threading.Lock()
ONLINE_VISITOR_TTL = 90
ACOINS_PACKAGES = {1000: (100, 1000), 5000: (500, 5000), 10000: (1000, 10000)}
AUTH_CORS_ORIGINS = {"https://mko.agpstudios.org", "https://everenfell.agpstudios.org"}
AUTH_CORS_PATHS = {"/api/auth/session", "/api/auth/login", "/api/auth/register", "/api/auth/logout"}
AUTH_CORS_METHODS = {"GET", "POST"}
AUTH_CORS_HEADERS = {"content-type", "x-csrf-token"}


def payment_cipher():
    value = CONFIG["payment_encryption_key"]
    if not value: raise RuntimeError('payment encryption key not configured')
    return Fernet(value.encode())


def payment_key_configured():
    try: payment_cipher(); return True
    except Exception: return False


def completed_paypal_capture(order, result):
    if result.get('id') != order['paypal_order_id'] or result.get('status') != 'COMPLETED': return None
    units = result.get('purchase_units')
    if not isinstance(units, list) or len(units) != 1: return None
    if units[0].get('reference_id') != order['id']: return None
    payments = units[0].get('payments', {})
    captures = payments.get('captures', [])
    if len(captures) != 1: return None
    capture = captures[0]
    amount = capture.get('amount', {})
    dollars = f"{order['amount_cents'] // 100}.{order['amount_cents'] % 100:02d}"
    if capture.get('status') != 'COMPLETED' or amount.get('currency_code') != 'USD' or amount.get('value') != dollars: return None
    return capture.get('id') if isinstance(capture.get('id'), str) else None


def paypal_request(settings, method, path, payload=None, request_id=None):
    base = 'https://api-m.sandbox.paypal.com' if settings['environment'] == 'sandbox' else 'https://api-m.paypal.com'
    secret = payment_cipher().decrypt(settings['paypal_client_secret'].encode()).decode()
    basic = b64encode(f"{settings['paypal_client_id']}:{secret}".encode()).decode()
    token_request = Request(base + '/v1/oauth2/token', data=b'grant_type=client_credentials',
                            headers={'Authorization': 'Basic ' + basic, 'Content-Type': 'application/x-www-form-urlencoded'}, method='POST')
    with urlopen(token_request, timeout=15) as response: token = json.loads(response.read())['access_token']
    data = json.dumps(payload or {}).encode() if method != 'GET' else None
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json', 'Content-Type': 'application/json', 'Prefer': 'return=representation'}
    if request_id: headers['PayPal-Request-Id'] = request_id
    request = Request(base + path, data=data, headers=headers, method=method)
    with urlopen(request, timeout=15) as response: return json.loads(response.read())


def record_online_visitor(visitor_id, now=None):
    now=time.time() if now is None else now
    with ONLINE_VISITORS_LOCK:
        for key, seen in list(ONLINE_VISITORS.items()):
            if now - seen >= ONLINE_VISITOR_TTL: ONLINE_VISITORS.pop(key, None)
        ONLINE_VISITORS[visitor_id]=now
        return len(ONLINE_VISITORS)


def allow_game_ticket(user_id, now=None):
    now=time.time() if now is None else now
    with GAME_TICKET_LOCK:
        recent=[stamp for stamp in GAME_TICKET_ATTEMPTS.get(user_id,[]) if now-stamp < GAME_TICKET_WINDOW]
        if len(recent) >= GAME_TICKET_LIMIT:
            GAME_TICKET_ATTEMPTS[user_id]=recent
            return False
        GAME_TICKET_ATTEMPTS[user_id]=recent+[now]
        return True


def b64(value):
    return urlsafe_b64encode(value).rstrip(b"=").decode()


def key():
    with open(KEY_FILE, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def oidc_json(handler, value, status=200):
    body = json.dumps(value, separators=(",", ":"), default=str).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error(handler, message, status=400):
    oidc_json(handler, {"error":{"code":message.replace(' ', '_'),"message":message}}, status)


def oidc_client(client_id, redirect_uri):
    clients = CONFIG.get("oidc_clients", {})
    client = clients.get(client_id) if isinstance(clients, dict) else None
    if not isinstance(client, dict):
        return None
    redirects, origins = client.get("redirect_uris"), client.get("allowed_origins")
    if (not isinstance(redirects, list) or not isinstance(origins, list)
            or redirect_uri not in redirects):
        return None
    parsed = urlparse(redirect_uri)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        return None
    return client if origin in origins else None


def store_oidc_code(entry):
    now = time.time()
    with OIDC_CODES_LOCK:
        for code, stored in list(CODES.items()):
            if stored.get("expires", 0) <= now:
                del CODES[code]
        code = secrets.token_urlsafe(32)
        CODES[code] = entry
        return code


def consume_oidc_code(code):
    with OIDC_CODES_LOCK:
        return CODES.pop(code, None)


def valid_oidc_params(params):
    client_id = params.get("client_id", [""])[0]
    redirect_uri = params.get("redirect_uri", [""])[0]
    state = params.get("state", [""])[0]
    challenge = params.get("code_challenge", [""])[0]
    method = params.get("code_challenge_method", [""])[0]
    nonce = params.get("nonce", [""])[0]
    scopes = params.get("scope", [""])[0].split()
    if not scopes or "openid" not in scopes or set(scopes) - {"openid", "profile"}:
        return None
    if not oidc_client(client_id, redirect_uri):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._~-]{16,256}", state):
        return None
    if method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
        return None
    if nonce and not re.fullmatch(r"[A-Za-z0-9._~-]{16,256}", nonce):
        return None
    if client_id == "everenfell" and not nonce:
        return None
    return client_id, redirect_uri, state, challenge, nonce


def everenfell_entitled(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM everenfell_s1_entitlements WHERE user_id=?", (str(user_id),))
            return cur.fetchone() is not None


def verify_token(token):
    parts=token.split('.')
    if len(parts) != 3: return None
    try:
        raw=f'{parts[0]}.{parts[1]}'.encode(); signature=urlsafe_b64decode(parts[2] + '=' * (-len(parts[2]) % 4)); key().public_key().verify(signature, raw, padding.PKCS1v15(), hashes.SHA256())
        claims=json.loads(urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
        return claims if int(claims.get('exp', 0)) >= int(time.time()) else None
    except Exception: return None


def issue_token(entry):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": entry["user_id"], "aud": entry["client_id"],
              "exp": now + 3600, "iat": now, "email": entry["email"],
              "username": entry["username"], "role": entry["role"]}
    if entry.get("nonce"):
        claims["nonce"] = entry["nonce"]
    if entry["client_id"] == "everenfell":
        claims["everenfell_s1"] = bool(entry.get("everenfell_s1"))
    private = key()
    numbers = private.private_numbers().public_numbers
    kid = b64(sha256(numbers.n.to_bytes((numbers.n.bit_length()+7)//8, "big")).digest())
    header = b64(json.dumps({"alg":"RS256","typ":"JWT","kid":kid}, separators=(",", ":")).encode())
    payload = b64(json.dumps(claims, separators=(",", ":")).encode())
    raw = f"{header}.{payload}".encode()
    sig = private.sign(raw, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{b64(sig)}"


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        origin = self.headers.get("Origin")
        if origin in AUTH_CORS_ORIGINS and urlparse(self.path).path in AUTH_CORS_PATHS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/oauth/"):
            return self.oidc_get()
        if urlparse(self.path).path in ("/api/auth/session", "/api/auth/me", "/api/me"):
            return self.auth_session()
        if urlparse(self.path).path == "/api/game-status":
            return self.game_status()
        if urlparse(self.path).path == "/api/portal-data":
            return self.portal_data()
        if urlparse(self.path).path == "/api/games":
            return self.games_list()
        if urlparse(self.path).path == "/api/news":
            return self.news_list()
        if urlparse(self.path).path == "/api/auth/verify-email":
            return self.verify_email(parse_qs(urlparse(self.path).query).get('token', [''])[0])
        if urlparse(self.path).path.startswith("/api/admin/"):
            return self.admin_get()
        if urlparse(self.path).path in ("/api/support", "/api/account/summary") or urlparse(self.path).path.startswith("/api/support/"):
            return self.member_get()
        self.forward()

    def do_POST(self):
        if self.path.startswith("/api/oauth/"):
            return self.oidc_post()
        if urlparse(self.path).path == "/api/auth/login":
            return self.auth_login()
        if urlparse(self.path).path == "/api/auth/register":
            return self.auth_register()
        if urlparse(self.path).path == "/api/auth/verify-email/resend":
            return self.resend_verification()
        if urlparse(self.path).path == "/api/auth/logout":
            return self.auth_logout()
        if urlparse(self.path).path == "/api/game/ticket":
            return self.game_ticket()
        if urlparse(self.path).path == "/api/acoins/order":
            return self.acoins_order()
        if urlparse(self.path).path == "/api/acoins/capture":
            return self.acoins_capture()
        if urlparse(self.path).path == "/api/online-presence":
            return self.online_presence()
        if urlparse(self.path).path == "/api/telemetry/client-error":
            return self.client_error_report()
        if urlparse(self.path).path == "/api/admin/audit/prune":
            return self.admin_audit_prune()
        if urlparse(self.path).path == "/api/admin/database/migrations/apply":
            return self.admin_migrations_apply()
        if urlparse(self.path).path.startswith("/api/admin/"):
            return self.admin_post()
        if urlparse(self.path).path == "/api/support" or urlparse(self.path).path.startswith("/api/support/"):
            return self.member_post()
        self.forward()

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/account": return self.account_update()
        if path.startswith("/api/admin/games/"): return self.admin_game_update(path.removeprefix("/api/admin/games/"))
        self.forward()
    def do_PATCH(self): self.forward()
    def do_DELETE(self):
        self._delete_body=self.rfile.read(int(self.headers.get('Content-Length','0')) or 0)
        if urlparse(self.path).path == "/api/account": return self.account_delete()
        if urlparse(self.path).path.startswith("/api/admin/users/"): return self.admin_user_delete(urlparse(self.path).path.rsplit('/',1)[1])
        self.forward()
    def do_OPTIONS(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        method = self.headers.get("Access-Control-Request-Method", "").upper()
        headers = {header.strip().lower() for header in self.headers.get("Access-Control-Request-Headers", "").split(",") if header.strip()}
        if origin not in AUTH_CORS_ORIGINS or path not in AUTH_CORS_PATHS or method not in AUTH_CORS_METHODS or not headers <= AUTH_CORS_HEADERS:
            return self.send_error(403)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def migrations_summary(self, cur):
        """Summarize migration state for the admin CP; an untracked database reports tracked=false."""
        summary = {"tracked": False, "tracking_table": migrate.TRACKING_TABLE,
                   "current_revision": None, "applied": [], "pending": [],
                   "drifted": [], "missing": []}
        try:
            migrations = migrate.discover()
        except Exception as exc:
            summary["error"] = str(exc)
            return summary

        rows = []
        try:
            cur.execute(f"SELECT filename, checksum, statements, applied_at, applied_by FROM {migrate.TRACKING_TABLE}")
            rows = cur.fetchall() or []
            summary["tracked"] = True
        except Exception:
            # Tracking table absent: the database is untracked, not broken.
            rows = []

        recorded = {row["filename"]: row for row in rows}
        result = migrate.plan(migrations, {name: row["checksum"] for name, row in recorded.items()})

        summary["applied"] = [
            {"filename": m.name,
             "applied_at": (recorded[m.name] or {}).get("applied_at"),
             "statements": (recorded[m.name] or {}).get("statements"),
             "applied_by": (recorded[m.name] or {}).get("applied_by") or ""}
            for m in result.applied
        ]
        summary["pending"] = [{"filename": m.name, "statements": len(m.statements)} for m in result.pending]
        summary["drifted"] = [m.name for m in result.drifted]
        summary["missing"] = result.missing
        if result.applied:
            summary["current_revision"] = result.applied[-1].name
        return summary

    def admin_migrations_apply(self):
        """Apply pending migrations, or record them as applied when baselining."""
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
            baseline=bool(form.get('baseline', False))
            actor=f"admin:{user['username']}"
            with db() as conn:
                with conn.cursor() as cur:
                    migrate.ensure_tracking_table(cur)
                    result=migrate.plan(migrate.discover(), migrate.load_applied(cur))
                    todo = (result.pending + result.drifted) if baseline else result.pending
                    handled=[]
                    for migration in todo:
                        if baseline:
                            migrate.record(cur, migration, actor)
                        else:
                            migrate.apply_migration(conn, migration, actor)
                        handled.append(migration.name)
                    action='admin.migrations.baselined' if baseline else 'admin.migrations.applied'
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'migrations',?,?,CAST(strftime('%s','now') AS INTEGER))",
                                (user['id'],user['username'],action,'schema_migrations',json.dumps({"baseline":baseline,"files":handled})))
            return oidc_json(self,{"ok":True,"baseline":baseline,"applied":handled})
        except Exception as exc: return error(self,f"migration unavailable: {exc}",503)

    def admin_game_update(self, slug):
        """PUT /api/admin/games/:slug — swap poster art without a migration.
        Other fields (title, summary, url, status, featured) are accepted for
        future use but currently ignored; only poster is updatable here."""
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        if not slug: return error(self,"game slug required")
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
            poster=form.get("poster")
            if not isinstance(poster,str): return error(self,"poster url required")
            poster=poster.strip()
            if len(poster) > 400: return error(self,"poster url too long (max 400)")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE games SET poster=?,updated_at=CAST(strftime('%s','now') AS INTEGER) WHERE slug=?",(poster,slug))
                    if cur.rowcount == 0: return error(self,"game not found",404)
            return oidc_json(self,{"ok":True,"slug":slug,"poster":poster})
        except Exception as exc: return error(self,f"games unavailable: {exc}",503)

    def admin_user(self):
        user=self.current_user()
        if not user or str(user.get('role','')).lower() != 'admin': return None
        return user

    def duplicate_submit(self, path, form, intent=""):
        """True when the same admin mutation for the same target arrived within
        the idempotency window; records this attempt otherwise.

        The target is read from `user_id` (user mutations) or `id` (client error
        triage) — without the latter, every triage call would share one key and
        a second error triaged seconds later would be rejected as a duplicate.

        `intent` distinguishes *what* was asked of that target, and triage passes
        it: resolving a report and immediately reopening it is a legitimate
        change of mind, not a double-click, so those must not collide. User
        mutations keep the original key (no intent) so their behavior is
        unchanged.
        """
        key = (path, str(form.get("user_id") or form.get("id") or ""), str(intent))
        now = time.time()
        last = RECENT_MUTATIONS.get(key)
        RECENT_MUTATIONS[key] = now
        if last is not None and now - last < IDEMPOTENCY_WINDOW:
            # Prune expired entries occasionally so the map doesn't grow forever.
            if len(RECENT_MUTATIONS) > 1024:
                cutoff = now - IDEMPOTENCY_WINDOW
                for stale in [k for k, t in RECENT_MUTATIONS.items() if t < cutoff]:
                    del RECENT_MUTATIONS[stale]
            return True
        return False

    def admin_get(self):
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        path=urlparse(self.path).path; query=parse_qs(urlparse(self.path).query)
        if path == '/api/admin/repo-status':
            root='/var/oled/data/AshatHub'
            try:
                repo=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True,timeout=5).strip()
                dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=root,text=True,timeout=5).strip())
                state='dirty' if dirty else 'current'
            except Exception:
                repo='unknown'; state='unknown'
            return oidc_json(self,{'ashathub':{'live':repo,'repo':repo,'state':state}})
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    if path.startswith('/api/admin/users/') and path.endswith('/moderation'):
                        uid=path.removeprefix('/api/admin/users/').removesuffix('/moderation')
                        cur.execute("SELECT id,action,actor_id,actor_name,detail,created_at FROM user_moderation_history WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT 100",(uid,))
                        events=cur.fetchall()
                        return oidc_json(self,{"user_id":uid,"events":events})
                    if path in ('/api/admin/summary','/api/admin/overview'):
                        cur.execute("SELECT COUNT(*) AS n FROM users"); total=cur.fetchone()['n']; cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_active=1"); active=cur.fetchone()['n']; cur.execute("SELECT COUNT(*) AS n FROM support_tickets WHERE status IN ('open','in_progress')"); tickets=cur.fetchone()['n']; return oidc_json(self,{"users":total,"active_users":active,"disabled_users":total-active,"open_tickets":tickets,"active_projects":0,"active_deploys":0})
                    if path == '/api/admin/users':
                        q=query.get('q',[''])[0]; page=max(1,min(1000,int(query.get('page',['1'])[0]))); like='%'+q+'%'; cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,u.is_active,u.banned_at,u.email_verified_at,EXISTS(SELECT 1 FROM everenfell_s1_entitlements e WHERE e.user_id=u.id) AS everenfell_s1 FROM users u WHERE u.id LIKE ? OR u.username LIKE ? OR u.email LIKE ? OR u.display_name LIKE ? ORDER BY u.username LIMIT 50 OFFSET ?",(like,like,like,like,(page-1)*50)); return oidc_json(self,{"users":cur.fetchall(),"page":page,"page_size":50})
                    if path.startswith('/api/admin/users/'):
                        uid=path.rsplit('/',1)[1]; cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,u.is_active,u.banned_at,u.email_verified_at,EXISTS(SELECT 1 FROM everenfell_s1_entitlements e WHERE e.user_id=u.id) AS everenfell_s1 FROM users u WHERE u.id=?",(uid,)); row=cur.fetchone(); return oidc_json(self,{"user":row}) if row else error(self,'user not found',404)
                    if path in ('/api/admin/support','/api/admin/tickets'):
                        cur.execute("SELECT id,subject,status,priority,category,created_at,updated_at FROM support_tickets ORDER BY updated_at DESC LIMIT 100"); return oidc_json(self,{"tickets":cur.fetchall()})
                    if path in ('/api/admin/health','/api/admin/database/status'):
                        cur.execute("SELECT sqlite_version() AS version"); return oidc_json(self,{"database":"healthy","version":cur.fetchone()['version'],"paws":self.game_status_value(),"migrations":self.migrations_summary(cur)})
                    if path == '/api/admin/client-errors':
                        # `status` defaults to new so triaged rows stop appearing as new;
                        # `all` and the explicit states are for reviewing what was done.
                        status=query.get('status',['new'])[0]
                        if status not in ('new','resolved','muted','all'): return error(self,'invalid status filter')
                        page=max(1,min(1000,int(query.get('page',['1'])[0])))
                        # Effective status: a message-wide mute wins over the row's own
                        # state, because the message is the thing being muted.
                        where="m.message_hash IS NULL AND COALESCE(t.status,'new')='new'" if status=='new' else ("m.message_hash IS NULL AND t.status='resolved'" if status=='resolved' else ("m.message_hash IS NOT NULL" if status=='muted' else '1=1'))
                        cur.execute("SELECT e.id,e.source,e.message,e.detail,e.path,e.count,e.occurrences,e.user_id,e.user_agent,e.created_at,"
                                    " sha256(e.message) AS message_hash,"
                                    " CASE WHEN m.message_hash IS NOT NULL THEN 'muted' ELSE COALESCE(t.status,'new') END AS triage_status,"
                                    " COALESCE(t.triaged_at,0) AS triaged_at, COALESCE(t.triaged_by,'') AS triaged_by,"
                                    " COALESCE(m.muted_at,0) AS muted_at, COALESCE(m.muted_by,'') AS muted_by"
                                    " FROM client_error_log e"
                                    " LEFT JOIN client_error_triage t ON t.error_id=e.id"
                                    " LEFT JOIN client_error_mutes m ON m.message_hash=sha256(e.message)"
                                    f" WHERE {where} ORDER BY e.created_at DESC,e.id DESC LIMIT 50 OFFSET ?",((page-1)*50,))
                        return oidc_json(self,{"errors":cur.fetchall(),"page":page,"page_size":50,"status":status})
                    if path == '/api/admin/audit':
                        page=max(1,min(1000,int(query.get('page',['1'])[0]))); cur.execute("SELECT id,actor_id,actor_name,action,target_type,target_id,detail,created_at FROM admin_audit_events ORDER BY created_at DESC,id DESC LIMIT 50 OFFSET ?",((page-1)*50,)); return oidc_json(self,{"events":cur.fetchall(),"page":page,"page_size":50})
                    if path == '/api/admin/settings': return oidc_json(self,{"email_verification_enabled":False,"secure_cookie":True,"backend":"python"})
                    if path == '/api/admin/payments/settings':
                        cur.execute("SELECT paypal_email,paypal_client_id,paypal_client_secret,environment FROM acoin_payment_settings WHERE id=1")
                        row=cur.fetchone() or {}
                        return oidc_json(self,{"paypal_email":row.get('paypal_email',''),"paypal_client_id":row.get('paypal_client_id',''),"client_secret_configured":bool(row.get('paypal_client_secret')),"environment":row.get('environment','sandbox'),"encryption_key_configured":payment_key_configured()})
                    if path == '/api/admin/games':
                        cur.execute("SELECT slug,title,summary,status,tags,url,poster,featured,sort_order FROM games ORDER BY sort_order,created_at")
                        rows=cur.fetchall()
                        for row in rows:
                            try: row["tags"]=json.loads(row.get("tags") or "[]")
                            except Exception: row["tags"]=[]
                            row["featured"] = bool(row.get("featured"))
                        return oidc_json(self,{"games":rows})
            return error(self,'admin endpoint not found',404)
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def game_status_value(self):
        try:
            with urlopen(CONFIG["paws_health_url"], timeout=5) as response: return json.loads(response.read())
        except Exception: return {"status":"unavailable"}

    def acoins_order(self):
        user=self.current_user()
        if not user: return error(self,'authentication required',401)
        if not self.csrf_ok(user): return error(self,'csrf token required',403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 <= length <= 1024: return error(self,'invalid ACoins package')
            try: form=json.loads(self.rfile.read(length) or b'{}')
            except ValueError: return error(self,'invalid ACoins package')
            if not isinstance(form,dict): return error(self,'invalid ACoins package')
            package=form.get('acoins')
            if type(package) is not int or package not in ACOINS_PACKAGES: return error(self,'invalid ACoins package')
            amount_cents,coins=ACOINS_PACKAGES[package]
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute('SELECT paypal_email,paypal_client_id,paypal_client_secret,environment FROM acoin_payment_settings WHERE id=1')
                    settings=cur.fetchone()
            if not settings or not settings['paypal_client_id'] or not settings['paypal_client_secret']:
                return error(self,'PayPal is not configured',503)
            local_id=secrets.token_hex(16)
            result=paypal_request(settings,'POST','/v2/checkout/orders',{
                'intent':'CAPTURE',
                'payment_source':{'paypal':{'experience_context':{
                    'brand_name':'AGP Studios','landing_page':'LOGIN','user_action':'PAY_NOW',
                    'shipping_preference':'NO_SHIPPING',
                    'return_url':'https://www.agpstudios.org/?acoins=return',
                    'cancel_url':'https://www.agpstudios.org/?acoins=cancel'}}},
                'purchase_units':[{'reference_id':local_id,'custom_id':local_id,
                    'payee':{'email_address':settings['paypal_email']},
                    'amount':{'currency_code':'USD','value':f'{amount_cents // 100}.{amount_cents % 100:02d}'},
                    'description':f'{coins:,} ACoins'}]},request_id=local_id)
            paypal_id=result.get('id')
            approval=next((item.get('href') for item in result.get('links',[]) if item.get('rel') in ('approve','payer-action')),None)
            if not isinstance(paypal_id,str) or not isinstance(approval,str) or urlparse(approval).scheme != 'https' or urlparse(approval).hostname not in ('www.paypal.com','www.sandbox.paypal.com'):
                return error(self,'PayPal checkout unavailable',503)
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO acoin_orders (id,paypal_order_id,user_id,environment,amount_cents,acoins,status,created_at) VALUES (?,?,?,?,?,?,'pending',CAST(strftime('%s','now') AS INTEGER))",(local_id,paypal_id,str(user['id']),settings['environment'],amount_cents,coins))
            return oidc_json(self,{'approval_url':approval,'order_id':paypal_id,'acoins':coins,'amount_cents':amount_cents},201)
        except Exception:
            return error(self,'PayPal checkout unavailable',503)

    def acoins_capture(self):
        user=self.current_user()
        if not user: return error(self,'authentication required',401)
        if not self.csrf_ok(user): return error(self,'csrf token required',403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0 <= length <= 1024: return error(self,'invalid PayPal order')
            try: form=json.loads(self.rfile.read(length) or b'{}')
            except ValueError: return error(self,'invalid PayPal order')
            paypal_id=form.get('order_id') if isinstance(form,dict) else None
            if not isinstance(paypal_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{5,64}',paypal_id): return error(self,'invalid PayPal order')
            with db() as conn:
                # ponytail: SQLite's single-writer lock spans PayPal capture to prevent double credit; use a short claim/lease if write contention appears.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    with conn.cursor() as cur:
                        cur.execute('SELECT * FROM acoin_orders WHERE paypal_order_id=? AND user_id=?',(paypal_id,str(user['id'])))
                        order=cur.fetchone()
                        if not order:
                            conn.rollback(); return error(self,'purchase not found',404)
                        cur.execute('SELECT acoins_balance FROM users WHERE id=?',(str(user['id']),))
                        balance=cur.fetchone()['acoins_balance']
                        if order['status']=='completed':
                            conn.commit(); return oidc_json(self,{'balance':balance,'credited':order['acoins']})
                        cur.execute('SELECT paypal_email,paypal_client_id,paypal_client_secret,environment FROM acoin_payment_settings WHERE id=1')
                        settings=cur.fetchone()
                        if not settings or settings['environment'] != order['environment']:
                            conn.rollback(); return error(self,'payment settings changed; contact support',503)
                        try:
                            result=paypal_request(settings,'POST',f'/v2/checkout/orders/{paypal_id}/capture',{},request_id=order['id']+'-capture')
                        except Exception:
                            result=paypal_request(settings,'GET',f'/v2/checkout/orders/{paypal_id}')
                        capture_id=completed_paypal_capture(order,result)
                        if not capture_id:
                            conn.rollback(); return error(self,'PayPal payment is not complete',409)
                        cur.execute("UPDATE acoin_orders SET status='completed',paypal_capture_id=?,completed_at=CAST(strftime('%s','now') AS INTEGER) WHERE id=? AND status='pending'",(capture_id,order['id']))
                        cur.execute('UPDATE users SET acoins_balance=acoins_balance+? WHERE id=?',(order['acoins'],str(user['id'])))
                        cur.execute('SELECT acoins_balance FROM users WHERE id=?',(str(user['id']),))
                        balance=cur.fetchone()['acoins_balance']
                    conn.commit()
                    return oidc_json(self,{'balance':balance,'credited':order['acoins']})
                except Exception:
                    conn.rollback()
                    raise
        except Exception:
            return error(self,'Payment confirmation unavailable',503)

    def admin_post(self):
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length","0"))
        if path == '/api/admin/payments/settings' and not 0 <= length <= 4096: return error(self,'invalid payment settings')
        form=json.loads(self.rfile.read(length) or b'{}')
        # Audit/context for the branches below. User mutations write a moderation
        # timeline entry; client error triage has no user to attach one to, so it
        # clears mod_detail and retargets the audit event instead.
        audit_target_type='user'; audit_target_id=str(form.get('user_id',''))
        audit_detail=json.dumps({}) if path == '/api/admin/payments/settings' else json.dumps(form)
        log_moderation=True
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    if path == '/api/admin/payments/settings':
                        if not isinstance(form,dict): return error(self,'invalid payment settings')
                        email=form.get('paypal_email','')
                        client_id=form.get('paypal_client_id','')
                        secret=form.get('paypal_client_secret','')
                        environment=form.get('environment','sandbox')
                        if not isinstance(email,str) or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email.strip()): return error(self,'valid PayPal email required')
                        if not isinstance(client_id,str) or not client_id.strip() or len(client_id)>255: return error(self,'PayPal client ID required')
                        if not isinstance(secret,str) or len(secret)>512: return error(self,'invalid PayPal client secret')
                        if environment not in ('sandbox','live'): return error(self,'invalid PayPal environment')
                        cur.execute("SELECT paypal_client_secret FROM acoin_payment_settings WHERE id=1")
                        saved=cur.fetchone() or {}
                        encrypted=saved.get('paypal_client_secret','')
                        if secret:
                            try: encrypted=payment_cipher().encrypt(secret.encode()).decode()
                            except Exception: return error(self,'payment encryption key unavailable',503)
                        cur.execute("INSERT INTO acoin_payment_settings (id,paypal_email,paypal_client_id,paypal_client_secret,environment,updated_by,updated_at) VALUES (1,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER)) ON CONFLICT(id) DO UPDATE SET paypal_email=excluded.paypal_email,paypal_client_id=excluded.paypal_client_id,paypal_client_secret=excluded.paypal_client_secret,environment=excluded.environment,updated_by=excluded.updated_by,updated_at=excluded.updated_at",(email.strip(),client_id.strip(),encrypted,environment,str(user['id'])))
                        action='acoins.payment_settings.updated'; audit_target_type='payment_settings'; audit_target_id='paypal'; log_moderation=False
                        audit_detail=json.dumps({'paypal_email':email.strip(),'environment':environment})
                        mod_detail=''
                    elif path == '/api/admin/world-of-ra/grant':
                        uid=str(form.get('user_id','')).strip()
                        if not uid: return error(self,'user id required')
                        if self.duplicate_submit(path,form): return error(self,'duplicate submission; retry shortly',409)
                        cur.execute("SELECT id FROM users WHERE id=?",(uid,))
                        if not cur.fetchone(): return error(self,'user not found',404)
                        cur.execute("INSERT INTO world_of_ra_entitlements (user_id,granted_at,granted_by) VALUES (?,CAST(strftime('%s','now') AS INTEGER),?) ON CONFLICT(user_id) DO UPDATE SET granted_at=excluded.granted_at,granted_by=excluded.granted_by",(uid,str(user['id'])))
                        action='world_of_ra.entitlement.granted'; audit_target_type='world_of_ra_entitlement'; audit_target_id=uid; log_moderation=False
                        mod_detail=''
                    elif path == '/api/admin/everenfell/s1':
                        uid=str(form.get('user_id','')).strip()
                        enabled=form.get('enabled')
                        if not uid or type(enabled) is not bool: return error(self,'user_id and boolean enabled required')
                        if self.duplicate_submit(path,form,'grant' if enabled else 'revoke'): return error(self,'duplicate submission; retry shortly',409)
                        cur.execute("SELECT id,role,is_active FROM users WHERE id=?",(uid,))
                        target=cur.fetchone()
                        if not target: return error(self,'user not found',404)
                        if enabled and (str(target['role']).strip().casefold() != 'admin' or not target['is_active']): return error(self,'only active administrators can receive S1 entitlement',400)
                        if enabled:
                            cur.execute("INSERT INTO everenfell_s1_entitlements (user_id,granted_at,granted_by) VALUES (?,CAST(strftime('%s','now') AS INTEGER),?) ON CONFLICT(user_id) DO UPDATE SET granted_at=excluded.granted_at,granted_by=excluded.granted_by",(uid,str(user['id'])))
                            action='everenfell.s1.entitlement.granted'
                        else:
                            cur.execute("DELETE FROM everenfell_s1_entitlements WHERE user_id=?",(uid,))
                            action='everenfell.s1.entitlement.revoked'
                        audit_target_type='everenfell_s1_entitlement'; audit_target_id=uid; log_moderation=False
                        audit_detail=json.dumps({'enabled':enabled}); mod_detail=''
                    elif path == '/api/admin/users/role':
                        if form.get('role') not in ('Member','Pro','Admin'): return error(self,'invalid role')
                        if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                        cur.execute("UPDATE users SET role=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(form['role'],form.get('user_id'))); action='user.role.updated'
                        mod_detail='role: '+str(form['role'])
                    elif path in ('/api/admin/users/status','/api/admin/users/ban'):
                        if path.endswith('/ban'):
                            # POST /api/admin/users/ban accepts {user_id, banned: bool}.
                            # `banned` defaults to true so older callers that omit it
                            # (or sent only user_id) keep the original ban behavior.
                            banned=bool(form.get('banned', True))
                            if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                            if banned: cur.execute("UPDATE users SET is_active=0,banned_at=CAST(strftime('%s','now') AS INTEGER),updated_at=CURRENT_TIMESTAMP WHERE id=?",(form.get('user_id'),))
                            else: cur.execute("UPDATE users SET is_active=1,banned_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(form.get('user_id'),))
                            action='user.banned' if banned else 'user.unbanned'
                            mod_detail='banned' if banned else 'unbanned'
                        else:
                            status=form.get('status','active')
                            if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                            if status == 'banned': cur.execute("UPDATE users SET is_active=0,banned_at=CAST(strftime('%s','now') AS INTEGER),updated_at=CURRENT_TIMESTAMP WHERE id=?",(form.get('user_id'),))
                            else: cur.execute("UPDATE users SET is_active=?,banned_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(0 if status=='disabled' else 1,form.get('user_id')))
                            action='user.status.updated'
                            mod_detail='status: '+str(status)
                    elif path == '/api/admin/client-errors/status':
                        # Row-level triage: 'resolved' keeps the judgement,
                        # 'new' removes it (reopening the report).
                        if form.get('status') not in ('new','resolved'): return error(self,'invalid status')
                        if self.duplicate_submit(path, form, form['status']): return error(self,"duplicate submission; retry shortly",409)
                        try: error_id=int(form.get('id'))
                        except (TypeError,ValueError): return error(self,'invalid client error id')
                        cur.execute("SELECT id FROM client_error_log WHERE id=?",(error_id,))
                        if not cur.fetchone(): return error(self,'client error not found',404)
                        if form['status'] == 'new': cur.execute("DELETE FROM client_error_triage WHERE error_id=?",(error_id,))
                        else: cur.execute("INSERT INTO client_error_triage (error_id,status,triaged_at,triaged_by) VALUES (?,?,CAST(strftime('%s','now') AS INTEGER),?) ON CONFLICT(error_id) DO UPDATE SET status=excluded.status,triaged_at=excluded.triaged_at,triaged_by=excluded.triaged_by",(error_id,form['status'],user['id']))
                        action='client_error.status.updated'
                        audit_target_type='client_error'; audit_target_id=str(error_id); log_moderation=False
                    elif path == '/api/admin/client-errors/mute':
                        # Message-level mute: the row identifies
                        # which message, and the mute then covers every report of
                        # it, past and future. {id, muted} mirrors /users/ban.
                        muted=bool(form.get('muted', True))
                        if self.duplicate_submit(path, form, 'muted' if muted else 'unmuted'): return error(self,"duplicate submission; retry shortly",409)
                        try: error_id=int(form.get('id'))
                        except (TypeError,ValueError): return error(self,'invalid client error id')
                        cur.execute("SELECT message FROM client_error_log WHERE id=?",(error_id,))
                        row=cur.fetchone()
                        if not row: return error(self,'client error not found',404)
                        if muted: cur.execute("INSERT INTO client_error_mutes (message_hash,message,muted_at,muted_by) VALUES (sha256(?),?,CAST(strftime('%s','now') AS INTEGER),?) ON CONFLICT(message_hash) DO UPDATE SET message=excluded.message,muted_at=excluded.muted_at,muted_by=excluded.muted_by",(row['message'],row['message'][:400],user['id']))
                        else: cur.execute("DELETE FROM client_error_mutes WHERE message_hash=sha256(?)",(row['message'],))
                        action='client_error.message.muted' if muted else 'client_error.message.unmuted'
                        audit_target_type='client_error'; audit_target_id=str(error_id); log_moderation=False
                    else: return error(self,'admin endpoint not found',404)
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],action,audit_target_type,audit_target_id,audit_detail))
                    # Moderation timeline: append to the dedicated history table
                    # so the admin CP can show per-user history.
                    if log_moderation: cur.execute("INSERT INTO user_moderation_history (user_id,action,actor_id,actor_name,detail,created_at) VALUES (?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",(str(form.get('user_id','')),action,user['id'],user['username'],mod_detail))
            return oidc_json(self,{"ok":True})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def admin_user_delete(self, uid):
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        if not uid: return error(self,"user id required")
        if uid == str(user.get('id','')): return error(self,"cannot delete your own account",409)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM users WHERE id=?",(uid,)); row=cur.fetchone()
                    if not row: return error(self,"user not found",404)
                    for table in ('support_ticket_replies','support_tickets','rust_sessions','sessions','email_verifications'):
                        cur.execute(f"DELETE FROM {table} WHERE user_id=?",(uid,))
                    cur.execute("DELETE FROM users WHERE id=?",(uid,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'user',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'admin.user.deleted',uid,json.dumps({'deleted_username':row['username']})))
            return oidc_json(self,{"ok":True,"deleted":uid})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def admin_audit_prune(self):
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}'); before=form.get('before_id')
            if not isinstance(before,int) or before < 1: return error(self,"integer before_id required")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM admin_audit_events WHERE id<?",(before,)); count=cur.fetchone()['n']
                    cur.execute("DELETE FROM admin_audit_events WHERE id<?",(before,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'audit',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'admin.audit.pruned',str(before),json.dumps({'deleted':count,'before_id':before})))
            return oidc_json(self,{"ok":True,"deleted":count})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def portal_data(self):
        health=self.game_status_value()
        return oidc_json(self, {"health":health,"players_online":None,"server_metrics":None,"season":None,"character":None,"news":[],"regions":[]})

    def online_presence(self):
        try:
            length=int(self.headers.get("Content-Length","0"))
            if length > 256: return error(self,"invalid visitor id")
            form=json.loads(self.rfile.read(length) or b'{}')
            visitor_id=form.get('visitor_id') if isinstance(form,dict) else None
            if not isinstance(visitor_id,str) or not re.fullmatch(r'[0-9a-fA-F-]{36}',visitor_id):
                return error(self,"invalid visitor id")
            website_count=record_online_visitor(visitor_id)
        except Exception:
            return error(self,"invalid visitor id")
        try:
            url=CONFIG["paws_online_url"]
            with urlopen(url,timeout=2) as response: game_count=max(0,int(json.loads(response.read()).get('online_count',0)))
        except Exception:
            game_count=0
        return oidc_json(self,{"online_users":website_count+game_count})

    def games_list(self):
        """Public read-only list of studio games for the homepage."""
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT slug,title,summary,status,tags,url,poster,featured FROM games ORDER BY sort_order,created_at")
                    rows = cur.fetchall()
            games = []
            for row in rows:
                try:
                    tags = json.loads(row.get("tags") or "[]")
                except Exception:
                    tags = []
                games.append({
                    "slug": row["slug"],
                    "title": row["title"],
                    "summary": row["summary"],
                    "status": row["status"],
                    "tags": tags,
                    "url": row["url"],
                    "poster": row["poster"] or None,
                    "featured": bool(row["featured"]),
                })
            return oidc_json(self, {"games": games})
        except Exception as exc:
            return error(self, f"games unavailable: {exc}", 503)

    def news_list(self):
        """Public read-only list of studio news for the homepage."""
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,title,summary,url,published_at FROM studio_news ORDER BY published_at DESC LIMIT 20")
                    rows = cur.fetchall()
            news = [{
                "id": row["id"],
                "title": row["title"],
                "summary": row["summary"],
                "url": row["url"] or "#news",
                "published_at": row["published_at"],
            } for row in rows]
            return oidc_json(self, {"news": news})
        except Exception as exc:
            return error(self, f"news unavailable: {exc}", 503)

    def game_status(self):
        try:
            with urlopen(CONFIG["paws_health_url"], timeout=5) as response:
                data=json.loads(response.read())
            return oidc_json(self, {"online": data.get("status") == "ok", "source": "paws-and-parcels", "health": data})
        except Exception as exc:
            return oidc_json(self, {"online": False, "source": "paws-and-parcels", "error": str(exc)}, 200)

    def ticket_user(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        with db() as conn:
            with conn.cursor() as cur:
                token=cookies.get('ashat_rust_sid')
                if token:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.is_active,u.banned_at,s.csrf_hash FROM rust_sessions s JOIN users u ON u.id=s.user_id WHERE s.session_hash=? AND s.expires_at>CURRENT_TIMESTAMP LIMIT 1", (sha256(token.encode()).hexdigest(),))
                    user=cur.fetchone()
                    if user: return user
                legacy=cookies.get('ashat_sid')
                if legacy:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.is_active,u.banned_at,'' AS csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires_at>CURRENT_TIMESTAMP LIMIT 1", (legacy,))
                    return cur.fetchone()
        return None

    def game_ticket(self):
        try:
            user=self.ticket_user()
        except Exception:
            return error(self,"ticket_unavailable",503)
        if not user: return error(self,"authentication_required",401)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        if not user.get('is_active') or user.get('banned_at'): return error(self,"suspended",403)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT user_id FROM world_of_ra_entitlements WHERE user_id=?",(user['id'],))
                    if not cur.fetchone(): return error(self,"alpha_not_granted",403)
        except Exception:
            return error(self,"ticket_unavailable",503)

        if not allow_game_ticket(str(user['id'])): return error(self,"ticket_rate_limited",429)

        mint_url=CONFIG["world_of_ra_mint_url"].rstrip('/')
        mint_key=CONFIG["world_of_ra_mint_key"]
        if not mint_url or not mint_key: return error(self,"mint_unavailable",503)
        payload=json.dumps({"user_id":str(user['id']),"username":user['username']}).encode()
        request=Request(mint_url, data=payload, headers={"Authorization":f"Bearer {mint_key}","Content-Type":"application/json"}, method="POST")
        try:
            with urlopen(request, timeout=8) as response:
                minted=json.loads(response.read())
            ticket=minted.get('ticket') if isinstance(minted,dict) else None
            if not isinstance(ticket,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',ticket):
                return error(self,"mint_unavailable",503)
            return oidc_json(self,{"ticket":ticket,"expires_in":300,"user":{"id":str(user['id']),"username":user['username']}})
        except Exception:
            return error(self,"mint_unavailable",503)

    def current_user(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        with db() as conn:
            with conn.cursor() as cur:
                token=cookies.get('ashat_rust_sid')
                if token:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,s.csrf_hash FROM rust_sessions s JOIN users u ON u.id=s.user_id WHERE s.session_hash=? AND s.expires_at>CURRENT_TIMESTAMP AND u.is_active=1 LIMIT 1", (sha256(token.encode()).hexdigest(),))
                    user=cur.fetchone()
                    if user: return user
                legacy=cookies.get('ashat_sid')
                if legacy:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,'' AS csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires_at>CURRENT_TIMESTAMP AND u.is_active=1 LIMIT 1", (legacy,))
                    return cur.fetchone()
        return None

    def csrf_ok(self, user):
        token=self.headers.get('X-CSRF-Token','')
        return bool(token) and sha256(token.encode()).hexdigest() == user.get('csrf_hash')

    def safe_user(self, user):
        return {key:value for key,value in user.items() if key != 'csrf_hash'}

    def member_get(self):
        user=self.current_user()
        if not user: return error(self, "authentication required", 401)
        path=urlparse(self.path).path
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    if path == "/api/account/summary":
                        cur.execute("SELECT COUNT(*) AS n FROM support_tickets WHERE user_id=?", (user["id"],)); tickets=cur.fetchone()["n"]
                        cur.execute("SELECT acoins_balance FROM users WHERE id=?", (user["id"],)); balance=cur.fetchone()["acoins_balance"]
                        return oidc_json(self, {"user":{**self.safe_user(user),"acoins_balance":balance},"stats":{"support_tickets":tickets}})
                    ticket_id=path.removeprefix('/api/support/').strip('/')
                    if ticket_id:
                        cur.execute("SELECT id,user_id,subject,status,priority,category,message,created_at,updated_at FROM support_tickets WHERE id=? AND user_id=?",(ticket_id,user["id"])); ticket=cur.fetchone()
                        if not ticket: return error(self,"ticket not found",404)
                        cur.execute("SELECT id,ticket_id,user_id,message,is_staff,created_at FROM support_ticket_replies WHERE ticket_id=? ORDER BY created_at",(ticket_id,)); return oidc_json(self,{"ticket":ticket,"replies":cur.fetchall()})
                    cur.execute("SELECT id,subject,status,priority,category,SUBSTRING(message,1,120) AS preview,created_at,updated_at FROM support_tickets WHERE user_id=? ORDER BY updated_at DESC LIMIT 100",(user["id"],)); return oidc_json(self,{"tickets":cur.fetchall()})
        except Exception as exc: return error(self, f"member unavailable: {exc}",503)

    def member_post(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length))
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    if path == "/api/support":
                        if not form.get("subject", "").strip() or not form.get("message", "").strip(): return error(self,"subject and message required")
                        ticket_id=secrets.token_hex(16); cur.execute("INSERT INTO support_tickets (id,user_id,subject,status,priority,category,message) VALUES (?,?,?,'open',?,?,?)",(ticket_id,user["id"],form["subject"].strip(),form.get("priority","normal"),form.get("category","other"),form["message"].strip())); return oidc_json(self,{"id":ticket_id,"status":"open"},201)
                    ticket_id=path.removeprefix('/api/support/').removesuffix('/reply')
                    if not form.get("message", "").strip(): return error(self,"message required")
                    cur.execute("SELECT id FROM support_tickets WHERE id=? AND user_id=?",(ticket_id,user["id"]))
                    if not cur.fetchone(): return error(self,"ticket not found",404)
                    reply_id=secrets.token_hex(16); cur.execute("INSERT INTO support_ticket_replies (id,ticket_id,user_id,message,is_staff) VALUES (?,?,?,?,0)",(reply_id,ticket_id,user["id"],form["message"].strip())); cur.execute("UPDATE support_tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(ticket_id,)); return oidc_json(self,{"ok":True,"id":reply_id})
        except Exception as exc: return error(self, f"support unavailable: {exc}",503)

    def account_update(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); username=form.get("username", "").strip(); email=form.get("email", "").strip(); display=form.get("display_name", user["display_name"]).strip()
            if not username or not email or not display: return error(self,"username, email, and display_name are required")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE (username=? OR email=?) AND id<>? LIMIT 1",(username,email,user["id"]))
                    if cur.fetchone(): return error(self,"username or email already in use",409)
                    cur.execute("UPDATE users SET username=?,email=?,display_name=? WHERE id=?",(username,email,display,user["id"]))
            return oidc_json(self,{"user":{**self.safe_user(user),"username":username,"email":email,"display_name":display}})
        except Exception as exc: return error(self,f"account unavailable: {exc}",503)

    def account_delete(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        form=json.loads(getattr(self,'_delete_body',b'{}') or b'{}')
        if form.get('confirm') != 'DELETE': return error(self,"confirm DELETE to remove the account",400)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    for table in ('support_ticket_replies','support_tickets','rust_sessions','sessions','email_verifications'):
                        cur.execute(f"DELETE FROM {table} WHERE user_id=?",(user['id'],))
                    cur.execute("DELETE FROM users WHERE id=?",(user['id'],))
            # Session cleanup: the DB rows are gone, but the browser still holds
            # ashat_rust_sid/ashat_csrf. Expire both cookies so the deleted
            # account's session is fully gone client-side too (and so a stale
            # sid can't be replayed against a future account reusing the row).
            body=json.dumps({"deleted":True}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.send_header("Set-Cookie","ashat_rust_sid=; Domain=agpstudios.org; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure")
            self.send_header("Set-Cookie","ashat_csrf=; Domain=agpstudios.org; Path=/; Max-Age=0; SameSite=Lax; Secure")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc: return error(self,f"account deletion unavailable: {exc}",503)

    def auth_logout(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        token = cookies.get('ashat_rust_sid')
        try:
            if token:
                with db() as conn:
                    with conn.cursor() as cur: cur.execute("DELETE FROM rust_sessions WHERE session_hash=?", (sha256(token.encode()).hexdigest(),))
            self.send_response(200); self.send_header("Set-Cookie", "ashat_rust_sid=; Domain=agpstudios.org; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure"); self.send_header("Content-Length", "0"); self.end_headers()
        except Exception as exc: error(self, f"auth unavailable: {exc}", 503)

    def resend_verification(self):
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); identifier=form.get("identifier", "").strip()
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,email,display_name,email_verified_at FROM users WHERE username=? OR email=? LIMIT 1",(identifier,identifier)); user=cur.fetchone()
                    if user and not user["email_verified_at"]:
                        token=secrets.token_urlsafe(32); cur.execute("UPDATE email_verifications SET used=1 WHERE user_id=? AND used=0",(user["id"],)); cur.execute("INSERT INTO email_verifications (id,user_id,token_hash,expires_at,used) VALUES (?,?,?,datetime('now','+24 hours'),0)",(secrets.token_hex(16),user["id"],sha256(token.encode()).hexdigest()))
                        link='https://www.agpstudios.org/api/auth/verify-email?token='+urlencode({'token':token})[6:]; message=f"From: support@agpstudios.org\nTo: {user['email']}\nSubject: Verify your AGP Studios email\n\nVerify your account: {link}\n"; subprocess.run(['/usr/sbin/sendmail','-f','support@agpstudios.org',user['email']],input=message.encode(),check=True)
            return oidc_json(self,{"sent":True},202)
        except Exception as exc: return error(self,f"verification unavailable: {exc}",503)

    def verify_email(self, token):
        if not token: return error(self, "verification token required")
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT user_id FROM email_verifications WHERE token_hash=? AND used=0 AND expires_at>CURRENT_TIMESTAMP LIMIT 1", (sha256(token.encode()).hexdigest(),)); row=cur.fetchone()
                    if not row: return error(self, "invalid or expired verification token")
                    cur.execute("UPDATE users SET email_verified_at=CURRENT_TIMESTAMP WHERE id=?", (row["user_id"],)); cur.execute("UPDATE email_verifications SET used=1 WHERE token_hash=?", (sha256(token.encode()).hexdigest(),))
            return oidc_json(self, {"verified":True})
        except Exception as exc: return error(self, f"verification unavailable: {exc}",503)

    def auth_register(self):
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); username=form.get("username", "").strip(); email=form.get("email", "").strip(); password=form.get("password", ""); display=form.get("display_name", username).strip()
            if not (3 <= len(username) <= 30 and username.replace('_','').isalnum() and '@' in email and len(password) >= 8 and display): return oidc_json(self, {"error":"invalid_registration"},400)
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM users WHERE username=? OR email=?", (username,email))
                    if cur.fetchone()["n"]: return oidc_json(self, {"error":"username_or_email_taken"},409)
                    user_id=secrets.token_hex(16); password_hash=crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))
                    cur.execute("INSERT INTO users (id,username,email,password_hash,display_name,role,is_active,email_verified_at) VALUES (?,?,?,?,?,'Member',1,CURRENT_TIMESTAMP)",(user_id,username,email,password_hash,display))
            return oidc_json(self, {"registered":True,"verification_required":False,"user":{"id":user_id,"username":username,"email":email,"display_name":display,"role":"Member"}},201)
        except Exception as exc: return error(self, f"registration unavailable: {exc}",503)

    def client_error_report(self):
        """Log an uncaught browser failure. Public and rate limited: the payload is untrusted log data."""
        client = self.client_address[0]
        now = time.time()
        CLIENT_ERROR_ATTEMPTS[client] = [stamp for stamp in CLIENT_ERROR_ATTEMPTS.get(client, []) if now - stamp < CLIENT_ERROR_WINDOW]
        if len(CLIENT_ERROR_ATTEMPTS[client]) >= CLIENT_ERROR_BUDGET:
            return error(self, "too many error reports", 429)
        CLIENT_ERROR_ATTEMPTS[client].append(now)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > CLIENT_ERROR_MAX_BODY:
                return error(self, "error report too large", 413)
            try:
                form = json.loads(self.rfile.read(length) or b'{}')
            except ValueError:
                return error(self, "error report must be a JSON object")
            if not isinstance(form, dict):
                return error(self, "error report must be a JSON object")
            message = str(form.get("message") or "").strip()
            if not message:
                return error(self, "message required")
            source = str(form.get("source") or "unknown")[:40]
            detail = str(form.get("detail") or "")[:400]
            page_path = urlparse(str(form.get("path") or "")).path[:200]
            try:
                count = max(1, min(10000, int(form.get("count") or 1)))
            except (TypeError, ValueError):
                count = 1
            listed = form.get("occurrences")
            occurrences = json.dumps([str(item)[:200] for item in listed[:5]], default=str) if isinstance(listed, list) else "[]"
            user_id = ""
            if 'ashat_rust_sid' in self.headers.get('Cookie', ''):
                try:
                    user_id = str((self.current_user() or {}).get('id') or '')
                except Exception:
                    user_id = ""
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("INSERT INTO client_error_log (source,message,detail,path,count,occurrences,user_id,user_agent,created_at) VALUES (?,?,?,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",
                                (source, message[:400], detail, page_path, count, occurrences[:2000], user_id, self.headers.get("User-Agent", "")[:200]))
                    self.prune_client_errors(cur)
            print(f"[client-error] {source} x{count} {page_path or '/'}: {message[:200]}", flush=True)
            return oidc_json(self, {"logged": True}, 202)
        except Exception as exc:
            return error(self, f"error log unavailable: {exc}", 503)

    def prune_client_errors(self, cur):
        """Delete reported errors past the retention window, at most once an hour per process."""
        global CLIENT_ERROR_PRUNED_AT
        now = time.time()
        if now - CLIENT_ERROR_PRUNED_AT < 3600:
            return
        CLIENT_ERROR_PRUNED_AT = now
        cur.execute("DELETE FROM client_error_log WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - ?", (CLIENT_ERROR_RETENTION,))
        # Triage rows for pruned reports have nothing left to describe. The mutes
        # table is deliberately untouched: a muted message stays muted.
        cur.execute("DELETE FROM client_error_triage WHERE NOT EXISTS (SELECT 1 FROM client_error_log WHERE id=client_error_triage.error_id)")

    def auth_session(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        token = cookies.get('ashat_rust_sid')
        if not token:
            return oidc_json(self, {"authenticated": False})
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds FROM rust_sessions s JOIN users u ON u.id=s.user_id WHERE s.session_hash=? AND s.expires_at>CURRENT_TIMESTAMP AND u.is_active=1 LIMIT 1", (sha256(token.encode()).hexdigest(),))
                    user = cur.fetchone()
            return oidc_json(self, {"authenticated": bool(user), "user": user})
        except Exception as exc:
            return error(self, f"auth unavailable: {exc}", 503)

    def auth_login(self):
        client = self.client_address[0]
        now = time.time()
        LOGIN_ATTEMPTS[client] = [stamp for stamp in LOGIN_ATTEMPTS.get(client, []) if now - stamp < 60]
        if len(LOGIN_ATTEMPTS[client]) >= 10: return error(self, "too many login attempts", 429)
        LOGIN_ATTEMPTS[client].append(now)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length))
            identifier=form.get("identifier", "").strip(); password=form.get("password", "")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,username,email,display_name,role,password_hash,is_active,email_verified_at FROM users WHERE username=? OR email=? LIMIT 1", (identifier, identifier))
                    user=cur.fetchone()
                    if not user or not user["is_active"] or crypt.crypt(password, user["password_hash"].replace("$2y$","$2b$")) != user["password_hash"].replace("$2y$","$2b$"):
                        return oidc_json(self, {"error":{"code":"invalid_credentials","message":"Invalid credentials"}}, 401)
                    LOGIN_ATTEMPTS.pop(client, None)
                    session=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32); h=lambda x:sha256(x.encode()).hexdigest()
                    cur.execute("INSERT INTO rust_sessions (session_hash,user_id,csrf_hash,created_at,last_seen,expires_at) VALUES (?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,datetime('now','+7200 seconds'))", (h(session),user["id"],h(csrf)))
            public={k:user[k] for k in ("id","username","email","display_name","role")}
            body=json.dumps({"user":public}).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Set-Cookie",f"ashat_rust_sid={session}; Domain=agpstudios.org; Path=/; HttpOnly; SameSite=Lax; Secure"); self.send_header("Set-Cookie",f"ashat_csrf={csrf}; Domain=agpstudios.org; Path=/; SameSite=Lax; Secure"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as exc:
            return error(self, f"auth unavailable: {exc}", 503)

    def oidc_get(self):
        parsed = urlparse(self.path)
        if parsed.path.endswith("openid-configuration"):
            return oidc_json(self, {"issuer": ISSUER, "authorization_endpoint": ISSUER+"/authorize",
                "token_endpoint": ISSUER+"/token", "userinfo_endpoint": ISSUER+"/userinfo",
                "jwks_uri": ISSUER+"/.well-known/jwks.json", "response_types_supported":["code"],
                "grant_types_supported":["authorization_code"], "code_challenge_methods_supported":["S256"],
                "id_token_signing_alg_values_supported":["RS256"]})
        if parsed.path.endswith("jwks.json"):
            public = key().public_key().public_numbers()
            n = b64(public.n.to_bytes((public.n.bit_length()+7)//8, "big"))
            e = b64(public.e.to_bytes((public.e.bit_length()+7)//8, "big"))
            kid = b64(sha256(public.n.to_bytes((public.n.bit_length()+7)//8, "big")).digest())
            return oidc_json(self, {"keys":[{"kty":"RSA","use":"sig","alg":"RS256","kid":kid,"n":n,"e":e}]})
        if parsed.path.endswith("authorize"):
            return self.authorize(parse_qs(parsed.query))
        if parsed.path.endswith("userinfo"):
            auth=self.headers.get('Authorization','')
            token=auth[7:] if auth.startswith('Bearer ') else parse_qs(parsed.query).get('access_token',[''])[0]
            claims=verify_token(token)
            return oidc_json(self, claims) if claims else error(self, 'invalid token', 401)
        return error(self, "not found", 404)

    def authorize(self, params):
        if params.get("response_type", [""])[0] != "code":
            return error(self, "invalid authorize request")
        values = valid_oidc_params(params)
        if values is None:
            return error(self, "invalid client, redirect, state, or PKCE parameters")
        client_id, redirect, state, challenge, nonce = values
        user = self.current_user()
        if user:
            try:
                entitled = client_id == "everenfell" and everenfell_entitled(user["id"])
                entry = {"client_id": client_id, "redirect_uri": redirect,
                         "code_challenge": challenge, "nonce": nonce,
                         "user_id": user["id"], "username": user["username"],
                         "email": user["email"], "role": user["role"],
                         "everenfell_s1": entitled, "expires": time.time() + 600}
                code = store_oidc_code(entry)
                target = redirect + ("&" if "?" in redirect else "?") + urlencode({"code": code, "state": state})
                self.send_response(303)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                error(self, "OIDC unavailable", 503)
            return
        values = {"response_type": "code", "client_id": client_id,
                  "redirect_uri": redirect, "state": state,
                  "scope": params.get("scope", [""])[0],
                  "code_challenge": challenge, "code_challenge_method": "S256",
                  "nonce": nonce}
        hidden = "".join(f'<input type="hidden" name="{html.escape(k, quote=True)}" value="{html.escape(v, quote=True)}">' for k, v in values.items())
        page = ('<!doctype html><title>AGP Studios sign in</title>'
                '<form method="post" action="/api/oauth/authorize">' + hidden +
                '<label>Username <input name="username" required></label>'
                '<label>Password <input name="password" type="password" required></label>'
                '<button>Sign in</button></form>')
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def oidc_post(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8192:
            return error(self, "request too large", 413)
        raw = self.rfile.read(length)
        if parsed.path.endswith("authorize"):
            form = parse_qs(raw.decode("utf-8", "replace"))
            values = valid_oidc_params(form)
            if values is None or form.get("response_type", [""])[0] != "code":
                return error(self, "invalid authorize request")
            client_id, redirect, state, challenge, nonce = values
            try:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id,username,email,role,password_hash FROM users WHERE (username=? OR email=?) AND is_active=1 LIMIT 1", (form.get("username", [""])[0], form.get("username", [""])[0]))
                        user = cur.fetchone()
                        if not user or crypt.crypt(form.get("password", [""])[0], user["password_hash"].replace("$2y$", "$2b$")) != user["password_hash"].replace("$2y$", "$2b$"):
                            return error(self, "invalid credentials", 401)
                        session = secrets.token_urlsafe(32)
                        csrf = secrets.token_urlsafe(32)
                        digest = lambda value: sha256(value.encode()).hexdigest()
                        cur.execute("INSERT INTO rust_sessions (session_hash,user_id,csrf_hash,ip,user_agent,created_at,last_seen,expires_at) VALUES (?,?,?,'oidc-authorize','oidc-authorize',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,datetime('now','+7200 seconds'))", (digest(session), user["id"], digest(csrf)))
                entitled = client_id == "everenfell" and everenfell_entitled(user["id"])
                entry = {"client_id": client_id, "redirect_uri": redirect,
                         "code_challenge": challenge, "nonce": nonce,
                         "user_id": user["id"], "username": user["username"],
                         "email": user["email"], "role": user["role"],
                         "everenfell_s1": entitled, "expires": time.time() + 600}
                code = store_oidc_code(entry)
                target = redirect + ("&" if "?" in redirect else "?") + urlencode({"code": code, "state": state})
                self.send_response(303)
                self.send_header("Location", target)
                self.send_header("Set-Cookie", f"ashat_rust_sid={session}; Domain=agpstudios.org; Path=/; HttpOnly; SameSite=Lax; Secure")
                self.send_header("Set-Cookie", f"ashat_csrf={csrf}; Domain=agpstudios.org; Path=/; SameSite=Lax; Secure")
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                error(self, "OIDC unavailable", 503)
            return
        if parsed.path.endswith("token"):
            try:
                form = json.loads(raw)
                code = form.get("code", "") if isinstance(form, dict) else ""
                entry = consume_oidc_code(code)
                valid = (entry is not None and entry["expires"] >= time.time()
                         and form.get("grant_type") == "authorization_code"
                         and form.get("client_id") == entry["client_id"]
                         and form.get("redirect_uri") == entry["redirect_uri"]
                         and oidc_client(entry["client_id"], entry["redirect_uri"]))
                verifier = form.get("code_verifier", "") if isinstance(form, dict) else ""
                valid = valid and isinstance(verifier, str) and re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) is not None
                valid = valid and b64(sha256(verifier.encode()).digest()) == entry["code_challenge"]
                if not valid:
                    return oidc_json(self, {"error": "invalid_grant"}, 400)
                return oidc_json(self, {"id_token": issue_token(entry), "token_type": "Bearer", "expires_in": 3600})
            except Exception:
                return oidc_json(self, {"error": "invalid_grant"}, 400)
        return self.forward()

    def forward(self):
        body=json.dumps({"error":{"code":"not_implemented","message":"This API endpoint has not been migrated to Python yet."}}).encode()
        self.send_response(404); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, format, *args): print(f"{self.address_string()} - {format % args}",flush=True)

if __name__ == "__main__":
    with db() as conn: conn.execute("PRAGMA journal_mode=WAL")
    ThreadingHTTPServer(("127.0.0.1", int(CONFIG["port"])), Proxy).serve_forever()
