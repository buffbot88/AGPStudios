#!/usr/bin/env python3
"""Python API for the retained Ashat Hub surface."""
from base64 import urlsafe_b64encode, urlsafe_b64decode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
import crypt, json, os, secrets, subprocess, sys, time

# The migration runner lives next to this file; import it by path so the API
# works regardless of the process working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import migrate

import pymysql
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ISSUER = os.environ.get("OIDC_ISSUER", "https://www.agpstudios.org/api/oauth")
KEY_FILE = os.environ.get("ASHAT_OIDC_PRIVATE_KEY_FILE", "")
CODES = {}
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


def b64(value):
    return urlsafe_b64encode(value).rstrip(b"=").decode()


def db():
    url = urlparse(os.environ["ASHAT_DATABASE_URL"])
    return pymysql.connect(host=url.hostname, port=url.port or 3306, user=url.username,
                           password=url.password, database=url.path.lstrip("/"),
                           cursorclass=pymysql.cursors.DictCursor, autocommit=True)


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


def redirect_ok(uri):
    parsed = urlparse(uri)
    return len(uri) <= 512 and parsed.scheme in ("https",) or (
        parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))


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
    def do_OPTIONS(self): self.forward()

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
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (%s,%s,%s,'migrations',%s,%s,UNIX_TIMESTAMP())",
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
                    cur.execute("UPDATE games SET poster=%s,updated_at=UNIX_TIMESTAMP() WHERE slug=%s",(poster,slug))
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
        if path == '/api/admin/alpha/status':
            def healthy(url):
                try:
                    with urlopen(url, timeout=3) as response: return response.status == 200
                except Exception: return False
            qwen=healthy('http://127.0.0.1:3004/health')
            alpha=healthy('http://127.0.0.1:3000/health')
            now=int(time.time())
            return oidc_json(self,{'servers':[
                {'id':'qwen','label':'Qwen Coder','online':qwen,'ready':qwen,'workers':1 if qwen else 0,'max_workers':1},
                {'id':'vision','label':'Vision','online':alpha,'ready':alpha,'workers':1 if alpha else 0,'max_workers':1},
            ],'updated_at':now})
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
                        cur.execute("SELECT id,action,actor_id,actor_name,detail,created_at FROM user_moderation_history WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 100",(uid,))
                        events=cur.fetchall()
                        return oidc_json(self,{"user_id":uid,"events":events})
                    if path in ('/api/admin/summary','/api/admin/overview'):
                        cur.execute("SELECT COUNT(*) AS n FROM users"); total=cur.fetchone()['n']; cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_active=1"); active=cur.fetchone()['n']; cur.execute("SELECT COUNT(*) AS n FROM support_tickets WHERE status IN ('open','in_progress')"); tickets=cur.fetchone()['n']; return oidc_json(self,{"users":total,"active_users":active,"disabled_users":total-active,"open_tickets":tickets,"active_projects":0,"active_deploys":0})
                    if path == '/api/admin/users':
                        q=query.get('q',[''])[0]; page=max(1,min(1000,int(query.get('page',['1'])[0]))); like='%'+q+'%'; cur.execute("SELECT id,username,email,display_name,role,diamonds,is_active,banned_at,email_verified_at FROM users WHERE id LIKE %s OR username LIKE %s OR email LIKE %s OR display_name LIKE %s ORDER BY username LIMIT 50 OFFSET %s",(like,like,like,like,(page-1)*50)); return oidc_json(self,{"users":cur.fetchall(),"page":page,"page_size":50})
                    if path.startswith('/api/admin/users/'):
                        uid=path.rsplit('/',1)[1]; cur.execute("SELECT id,username,email,display_name,role,diamonds,is_active,banned_at,email_verified_at FROM users WHERE id=%s",(uid,)); row=cur.fetchone(); return oidc_json(self,{"user":row}) if row else error(self,'user not found',404)
                    if path in ('/api/admin/support','/api/admin/tickets'):
                        cur.execute("SELECT id,subject,status,priority,category,created_at,updated_at FROM support_tickets ORDER BY updated_at DESC LIMIT 100"); return oidc_json(self,{"tickets":cur.fetchall()})
                    if path in ('/api/admin/health','/api/admin/database/status'):
                        cur.execute("SELECT VERSION() AS version"); return oidc_json(self,{"database":"healthy","version":cur.fetchone()['version'],"paws":self.game_status_value(),"migrations":self.migrations_summary(cur)})
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
                                    " SHA2(e.message,256) AS message_hash,"
                                    " CASE WHEN m.message_hash IS NOT NULL THEN 'muted' ELSE COALESCE(t.status,'new') END AS triage_status,"
                                    " COALESCE(t.triaged_at,0) AS triaged_at, COALESCE(t.triaged_by,'') AS triaged_by,"
                                    " COALESCE(m.muted_at,0) AS muted_at, COALESCE(m.muted_by,'') AS muted_by"
                                    " FROM client_error_log e"
                                    " LEFT JOIN client_error_triage t ON t.error_id=e.id"
                                    " LEFT JOIN client_error_mutes m ON m.message_hash=SHA2(e.message,256)"
                                    f" WHERE {where} ORDER BY e.created_at DESC,e.id DESC LIMIT 50 OFFSET %s",((page-1)*50,))
                        return oidc_json(self,{"errors":cur.fetchall(),"page":page,"page_size":50,"status":status})
                    if path == '/api/admin/audit':
                        page=max(1,min(1000,int(query.get('page',['1'])[0]))); cur.execute("SELECT id,actor_id,actor_name,action,target_type,target_id,detail,created_at FROM admin_audit_events ORDER BY created_at DESC,id DESC LIMIT 50 OFFSET %s",((page-1)*50,)); return oidc_json(self,{"events":cur.fetchall(),"page":page,"page_size":50})
                    if path == '/api/admin/settings': return oidc_json(self,{"email_verification_enabled":False,"secure_cookie":True,"backend":"python"})
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
            with urlopen(os.environ.get("PAWS_HEALTH_URL", "http://127.0.0.1:3003/api/health"), timeout=5) as response: return json.loads(response.read())
        except Exception: return {"status":"unavailable"}

    def admin_post(self):
        user=self.admin_user()
        if not user: return error(self,"admin authentication required",403)
        if not self.csrf_ok(user): return error(self,"csrf token required",403)
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
        # Audit/context for the branches below. User mutations write a moderation
        # timeline entry; client error triage has no user to attach one to, so it
        # clears mod_detail and retargets the audit event instead.
        audit_target_type='user'; audit_target_id=str(form.get('user_id',''))
        log_moderation=True
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    if path == '/api/admin/users/role':
                        if form.get('role') not in ('Member','Pro','Admin'): return error(self,'invalid role')
                        if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                        cur.execute("UPDATE users SET role=%s,updated_at=UTC_TIMESTAMP() WHERE id=%s",(form['role'],form.get('user_id'))); action='user.role.updated'
                        mod_detail='role: '+str(form['role'])
                    elif path in ('/api/admin/users/status','/api/admin/users/ban'):
                        if path.endswith('/ban'):
                            # POST /api/admin/users/ban accepts {user_id, banned: bool}.
                            # `banned` defaults to true so older callers that omit it
                            # (or sent only user_id) keep the original ban behavior.
                            banned=bool(form.get('banned', True))
                            if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                            if banned: cur.execute("UPDATE users SET is_active=0,banned_at=UNIX_TIMESTAMP(),updated_at=UTC_TIMESTAMP() WHERE id=%s",(form.get('user_id'),))
                            else: cur.execute("UPDATE users SET is_active=1,banned_at=NULL,updated_at=UTC_TIMESTAMP() WHERE id=%s",(form.get('user_id'),))
                            action='user.banned' if banned else 'user.unbanned'
                            mod_detail='banned' if banned else 'unbanned'
                        else:
                            status=form.get('status','active')
                            if self.duplicate_submit(path, form): return error(self,"duplicate submission; retry shortly",409)
                            if status == 'banned': cur.execute("UPDATE users SET is_active=0,banned_at=UNIX_TIMESTAMP(),updated_at=UTC_TIMESTAMP() WHERE id=%s",(form.get('user_id'),))
                            else: cur.execute("UPDATE users SET is_active=%s,banned_at=NULL,updated_at=UTC_TIMESTAMP() WHERE id=%s",(0 if status=='disabled' else 1,form.get('user_id')))
                            action='user.status.updated'
                            mod_detail='status: '+str(status)
                    elif path == '/api/admin/client-errors/status':
                        # Row-level triage: 'resolved' keeps the judgement,
                        # 'new' removes it (reopening the report).
                        if form.get('status') not in ('new','resolved'): return error(self,'invalid status')
                        if self.duplicate_submit(path, form, form['status']): return error(self,"duplicate submission; retry shortly",409)
                        try: error_id=int(form.get('id'))
                        except (TypeError,ValueError): return error(self,'invalid client error id')
                        cur.execute("SELECT id FROM client_error_log WHERE id=%s",(error_id,))
                        if not cur.fetchone(): return error(self,'client error not found',404)
                        if form['status'] == 'new': cur.execute("DELETE FROM client_error_triage WHERE error_id=%s",(error_id,))
                        else: cur.execute("INSERT INTO client_error_triage (error_id,status,triaged_at,triaged_by) VALUES (%s,%s,UNIX_TIMESTAMP(),%s) ON DUPLICATE KEY UPDATE status=VALUES(status),triaged_at=VALUES(triaged_at),triaged_by=VALUES(triaged_by)",(error_id,form['status'],user['id']))
                        action='client_error.status.updated'
                        audit_target_type='client_error'; audit_target_id=str(error_id); log_moderation=False
                    elif path == '/api/admin/client-errors/mute':
                        # Message-level mute (migration 0006): the row identifies
                        # which message, and the mute then covers every report of
                        # it, past and future. {id, muted} mirrors /users/ban.
                        muted=bool(form.get('muted', True))
                        if self.duplicate_submit(path, form, 'muted' if muted else 'unmuted'): return error(self,"duplicate submission; retry shortly",409)
                        try: error_id=int(form.get('id'))
                        except (TypeError,ValueError): return error(self,'invalid client error id')
                        cur.execute("SELECT message FROM client_error_log WHERE id=%s",(error_id,))
                        row=cur.fetchone()
                        if not row: return error(self,'client error not found',404)
                        if muted: cur.execute("INSERT INTO client_error_mutes (message_hash,message,muted_at,muted_by) VALUES (SHA2(%s,256),%s,UNIX_TIMESTAMP(),%s) ON DUPLICATE KEY UPDATE message=VALUES(message),muted_at=VALUES(muted_at),muted_by=VALUES(muted_by)",(row['message'],row['message'][:400],user['id']))
                        else: cur.execute("DELETE FROM client_error_mutes WHERE message_hash=SHA2(%s,256)",(row['message'],))
                        action='client_error.message.muted' if muted else 'client_error.message.unmuted'
                        audit_target_type='client_error'; audit_target_id=str(error_id); log_moderation=False
                    else: return error(self,'admin endpoint not found',404)
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (%s,%s,%s,%s,%s,%s,UNIX_TIMESTAMP())",(user['id'],user['username'],action,audit_target_type,audit_target_id,json.dumps(form)))
                    # Moderation timeline: append to the dedicated history table
                    # (migration 0004) so the admin CP can show per-user history.
                    if log_moderation: cur.execute("INSERT INTO user_moderation_history (user_id,action,actor_id,actor_name,detail,created_at) VALUES (%s,%s,%s,%s,%s,UNIX_TIMESTAMP())",(str(form.get('user_id','')),action,user['id'],user['username'],mod_detail))
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
                    cur.execute("SELECT username FROM users WHERE id=%s",(uid,)); row=cur.fetchone()
                    if not row: return error(self,"user not found",404)
                    for table in ('support_ticket_replies','support_tickets','rust_sessions','sessions','email_verifications'):
                        cur.execute(f"DELETE FROM {table} WHERE user_id=%s",(uid,))
                    cur.execute("DELETE FROM users WHERE id=%s",(uid,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (%s,%s,%s,'user',%s,%s,UNIX_TIMESTAMP())",(user['id'],user['username'],'admin.user.deleted',uid,json.dumps({'deleted_username':row['username']})))
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
                    cur.execute("SELECT COUNT(*) AS n FROM admin_audit_events WHERE id<%s",(before,)); count=cur.fetchone()['n']
                    cur.execute("DELETE FROM admin_audit_events WHERE id<%s",(before,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (%s,%s,%s,'audit',%s,%s,UNIX_TIMESTAMP())",(user['id'],user['username'],'admin.audit.pruned',str(before),json.dumps({'deleted':count,'before_id':before})))
            return oidc_json(self,{"ok":True,"deleted":count})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def portal_data(self):
        health=self.game_status_value()
        return oidc_json(self, {"health":health,"players_online":None,"server_metrics":None,"season":None,"character":None,"news":[],"regions":[]})

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
            with urlopen(os.environ.get("PAWS_HEALTH_URL", "http://127.0.0.1:3003/api/health"), timeout=5) as response:
                data=json.loads(response.read())
            return oidc_json(self, {"online": data.get("status") == "ok", "source": "paws-and-parcels", "health": data})
        except Exception as exc:
            return oidc_json(self, {"online": False, "source": "paws-and-parcels", "error": str(exc)}, 200)

    def current_user(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        with db() as conn:
            with conn.cursor() as cur:
                token=cookies.get('ashat_rust_sid')
                if token:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,s.csrf_hash FROM rust_sessions s JOIN users u ON u.id=s.user_id WHERE s.session_hash=%s AND s.expires_at>UTC_TIMESTAMP() AND u.is_active=1 LIMIT 1", (sha256(token.encode()).hexdigest(),))
                    user=cur.fetchone()
                    if user: return user
                legacy=cookies.get('ashat_sid')
                if legacy:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds,'' AS csrf_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=%s AND s.expires_at>UTC_TIMESTAMP() AND u.is_active=1 LIMIT 1", (legacy,))
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
                        cur.execute("SELECT COUNT(*) AS n FROM support_tickets WHERE user_id=%s", (user["id"],)); tickets=cur.fetchone()["n"]
                        return oidc_json(self, {"user":self.safe_user(user),"stats":{"support_tickets":tickets}})
                    ticket_id=path.removeprefix('/api/support/').strip('/')
                    if ticket_id:
                        cur.execute("SELECT id,user_id,subject,status,priority,category,message,created_at,updated_at FROM support_tickets WHERE id=%s AND user_id=%s",(ticket_id,user["id"])); ticket=cur.fetchone()
                        if not ticket: return error(self,"ticket not found",404)
                        cur.execute("SELECT id,ticket_id,user_id,message,is_staff,created_at FROM support_ticket_replies WHERE ticket_id=%s ORDER BY created_at",(ticket_id,)); return oidc_json(self,{"ticket":ticket,"replies":cur.fetchall()})
                    cur.execute("SELECT id,subject,status,priority,category,SUBSTRING(message,1,120) AS preview,created_at,updated_at FROM support_tickets WHERE user_id=%s ORDER BY updated_at DESC LIMIT 100",(user["id"],)); return oidc_json(self,{"tickets":cur.fetchall()})
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
                        ticket_id=secrets.token_hex(16); cur.execute("INSERT INTO support_tickets (id,user_id,subject,status,priority,category,message) VALUES (%s,%s,%s,'open',%s,%s,%s)",(ticket_id,user["id"],form["subject"].strip(),form.get("priority","normal"),form.get("category","other"),form["message"].strip())); return oidc_json(self,{"id":ticket_id,"status":"open"},201)
                    ticket_id=path.removeprefix('/api/support/').removesuffix('/reply')
                    if not form.get("message", "").strip(): return error(self,"message required")
                    cur.execute("SELECT id FROM support_tickets WHERE id=%s AND user_id=%s",(ticket_id,user["id"]))
                    if not cur.fetchone(): return error(self,"ticket not found",404)
                    reply_id=secrets.token_hex(16); cur.execute("INSERT INTO support_ticket_replies (id,ticket_id,user_id,message,is_staff) VALUES (%s,%s,%s,%s,0)",(reply_id,ticket_id,user["id"],form["message"].strip())); cur.execute("UPDATE support_tickets SET updated_at=UTC_TIMESTAMP() WHERE id=%s",(ticket_id,)); return oidc_json(self,{"ok":True,"id":reply_id})
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
                    cur.execute("SELECT id FROM users WHERE (username=%s OR email=%s) AND id<>%s LIMIT 1",(username,email,user["id"]))
                    if cur.fetchone(): return error(self,"username or email already in use",409)
                    cur.execute("UPDATE users SET username=%s,email=%s,display_name=%s WHERE id=%s",(username,email,display,user["id"]))
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
                        cur.execute(f"DELETE FROM {table} WHERE user_id=%s",(user['id'],))
                    cur.execute("DELETE FROM users WHERE id=%s",(user['id'],))
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
                    with conn.cursor() as cur: cur.execute("DELETE FROM rust_sessions WHERE session_hash=%s", (sha256(token.encode()).hexdigest(),))
            self.send_response(200); self.send_header("Set-Cookie", "ashat_rust_sid=; Domain=agpstudios.org; Path=/; Max-Age=0; HttpOnly; SameSite=Lax; Secure"); self.send_header("Content-Length", "0"); self.end_headers()
        except Exception as exc: error(self, f"auth unavailable: {exc}", 503)

    def resend_verification(self):
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); identifier=form.get("identifier", "").strip()
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id,email,display_name,email_verified_at FROM users WHERE username=%s OR email=%s LIMIT 1",(identifier,identifier)); user=cur.fetchone()
                    if user and not user["email_verified_at"]:
                        token=secrets.token_urlsafe(32); cur.execute("UPDATE email_verifications SET used=1 WHERE user_id=%s AND used=0",(user["id"],)); cur.execute("INSERT INTO email_verifications (id,user_id,token_hash,expires_at,used) VALUES (%s,%s,%s,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 24 HOUR),0)",(secrets.token_hex(16),user["id"],sha256(token.encode()).hexdigest()))
                        link='https://www.agpstudios.org/api/auth/verify-email?token='+urlencode({'token':token})[6:]; message=f"From: support@agpstudios.org\nTo: {user['email']}\nSubject: Verify your AGP Studios email\n\nVerify your account: {link}\n"; subprocess.run(['/usr/sbin/sendmail','-f','support@agpstudios.org',user['email']],input=message.encode(),check=True)
            return oidc_json(self,{"sent":True},202)
        except Exception as exc: return error(self,f"verification unavailable: {exc}",503)

    def verify_email(self, token):
        if not token: return error(self, "verification token required")
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT user_id FROM email_verifications WHERE token_hash=%s AND used=0 AND expires_at>UTC_TIMESTAMP() LIMIT 1", (sha256(token.encode()).hexdigest(),)); row=cur.fetchone()
                    if not row: return error(self, "invalid or expired verification token")
                    cur.execute("UPDATE users SET email_verified_at=UTC_TIMESTAMP() WHERE id=%s", (row["user_id"],)); cur.execute("UPDATE email_verifications SET used=1 WHERE token_hash=%s", (sha256(token.encode()).hexdigest(),))
            return oidc_json(self, {"verified":True})
        except Exception as exc: return error(self, f"verification unavailable: {exc}",503)

    def auth_register(self):
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); username=form.get("username", "").strip(); email=form.get("email", "").strip(); password=form.get("password", ""); display=form.get("display_name", username).strip()
            if not (3 <= len(username) <= 30 and username.replace('_','').isalnum() and '@' in email and len(password) >= 8 and display): return oidc_json(self, {"error":"invalid_registration"},400)
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM users WHERE username=%s OR email=%s", (username,email))
                    if cur.fetchone()["n"]: return oidc_json(self, {"error":"username_or_email_taken"},409)
                    user_id=secrets.token_hex(16); password_hash=crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))
                    cur.execute("INSERT INTO users (id,username,email,password_hash,display_name,role,is_active,email_verified_at) VALUES (%s,%s,%s,%s,%s,'Member',1,UTC_TIMESTAMP())",(user_id,username,email,password_hash,display))
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
                    cur.execute("INSERT INTO client_error_log (source,message,detail,path,count,occurrences,user_id,user_agent,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UNIX_TIMESTAMP())",
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
        cur.execute("DELETE FROM client_error_log WHERE created_at < UNIX_TIMESTAMP() - %s", (CLIENT_ERROR_RETENTION,))
        # Triage rows for pruned reports have nothing left to describe. The mutes
        # table is deliberately untouched: a muted message stays muted.
        cur.execute("DELETE t FROM client_error_triage t LEFT JOIN client_error_log e ON e.id=t.error_id WHERE e.id IS NULL")

    def auth_session(self):
        cookies = {item.split('=', 1)[0]: item.split('=', 1)[1] for item in self.headers.get('Cookie', '').split('; ') if '=' in item}
        token = cookies.get('ashat_rust_sid')
        if not token:
            return oidc_json(self, {"authenticated": False})
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT u.id,u.username,u.email,u.display_name,u.role,u.diamonds FROM rust_sessions s JOIN users u ON u.id=s.user_id WHERE s.session_hash=%s AND s.expires_at>UTC_TIMESTAMP() AND u.is_active=1 LIMIT 1", (sha256(token.encode()).hexdigest(),))
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
                    cur.execute("SELECT id,username,email,display_name,role,password_hash,is_active,email_verified_at FROM users WHERE username=%s OR email=%s LIMIT 1", (identifier, identifier))
                    user=cur.fetchone()
                    if not user or not user["is_active"] or crypt.crypt(password, user["password_hash"].replace("$2y$","$2b$")) != user["password_hash"].replace("$2y$","$2b$"):
                        return oidc_json(self, {"error":{"code":"invalid_credentials","message":"Invalid credentials"}}, 401)
                    LOGIN_ATTEMPTS.pop(client, None)
                    session=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32); h=lambda x:sha256(x.encode()).hexdigest()
                    cur.execute("INSERT INTO rust_sessions (session_hash,user_id,csrf_hash,created_at,last_seen,expires_at) VALUES (%s,%s,%s,UTC_TIMESTAMP(),UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL 7200 SECOND))", (h(session),user["id"],h(csrf)))
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
        required = ("response_type", "client_id", "redirect_uri", "state")
        if any(not params.get(k, [""])[0] for k in required) or params["response_type"][0] != "code":
            return error(self, "invalid authorize request")
        redirect = params["redirect_uri"][0]
        if not redirect_ok(redirect): return error(self, "redirect_uri not allowed")
        user = self.current_user()
        if user:
            code=secrets.token_urlsafe(32); CODES[code]={"client_id":params["client_id"][0],"redirect_uri":redirect,"code_challenge":params.get("code_challenge",[""])[0],"user_id":user["id"],"username":user["username"],"email":user["email"],"role":user["role"],"expires":time.time()+600}
            target=redirect+('&' if '?' in redirect else '?')+urlencode({"code":code,"state":params["state"][0]})
            self.send_response(303); self.send_header("Location",target); self.send_header("Content-Length","0"); self.end_headers(); return
        html = f'''<!doctype html><title>AGP Studios sign in</title><form method="post" action="/api/oauth/authorize">\
<input type="hidden" name="response_type" value="code"><input type="hidden" name="client_id" value="{params['client_id'][0]}">\
<input type="hidden" name="redirect_uri" value="{redirect}"><input type="hidden" name="state" value="{params['state'][0]}">\
<input type="hidden" name="code_challenge" value="{params.get('code_challenge',[''])[0]}"><input type="hidden" name="code_challenge_method" value="S256">\
<label>Username <input name="username" required></label><label>Password <input name="password" type="password" required></label><button>Sign in</button></form>'''
        body=html.encode(); self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)

    def oidc_post(self):
        parsed = urlparse(self.path)
        length=int(self.headers.get("Content-Length","0")); raw=self.rfile.read(length)
        if parsed.path.endswith("authorize"):
            form={k:v[0] for k,v in parse_qs(raw.decode()).items()}
            try:
                with db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT id,username,email,role,password_hash FROM users WHERE (username=%s OR email=%s) AND is_active=1 LIMIT 1", (form.get("username",""),form.get("username","")))
                        user=cur.fetchone()
                        if not user or not crypt.crypt(form.get("password",""), user["password_hash"].replace("$2y$","$2b$")) == user["password_hash"].replace("$2y$","$2b$"): return error(self,"invalid credentials")
                        session=secrets.token_urlsafe(32); csrf=secrets.token_urlsafe(32); h=lambda x:sha256(x.encode()).hexdigest()
                        cur.execute("INSERT INTO rust_sessions (session_hash,user_id,csrf_hash,ip,user_agent,created_at,last_seen,expires_at) VALUES (%s,%s,%s,'oidc-authorize','oidc-authorize',UTC_TIMESTAMP(),UTC_TIMESTAMP(),DATE_ADD(UTC_TIMESTAMP(),INTERVAL 7200 SECOND))",(h(session),user["id"],h(csrf)))
                entry={"client_id":form["client_id"],"redirect_uri":form["redirect_uri"],"code_challenge":form.get("code_challenge",""),"user_id":user["id"],"username":user["username"],"email":user["email"],"role":user["role"],"expires":time.time()+600}
                code=secrets.token_urlsafe(32); CODES[code]=entry
                target=entry["redirect_uri"]+('&' if '?' in entry["redirect_uri"] else '?')+urlencode({"code":code,"state":form["state"]})
                self.send_response(303); self.send_header("Location",target); self.send_header("Set-Cookie",f"ashat_rust_sid={session}; Domain=agpstudios.org; Path=/; HttpOnly; SameSite=Lax; Secure"); self.send_header("Set-Cookie",f"ashat_csrf={csrf}; Domain=agpstudios.org; Path=/; SameSite=Lax; Secure"); self.send_header("Content-Length","0"); self.end_headers()
            except Exception as exc: error(self, f"OIDC unavailable: {exc}", 503)
            return
        if parsed.path.endswith("token"):
            try:
                form=json.loads(raw); entry=CODES.pop(form.get("code",""),None)
                if not entry or entry["expires"]<time.time() or form.get("redirect_uri") not in (None,entry["redirect_uri"]): return oidc_json(self,{"error":"invalid_grant"},400)
                if entry["code_challenge"] and b64(sha256(form.get("code_verifier","").encode()).digest()) != entry["code_challenge"]: return oidc_json(self,{"error":"invalid_grant"},400)
                return oidc_json(self,{"id_token":issue_token(entry),"token_type":"Bearer","expires_in":3600})
            except Exception: return oidc_json(self,{"error":"invalid_grant"},400)
        return self.forward()

    def forward(self):
        body=json.dumps({"error":{"code":"not_implemented","message":"This API endpoint has not been migrated to Python yet."}}).encode()
        self.send_response(404); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, format, *args): print(f"{self.address_string()} - {format % args}",flush=True)

if __name__ == "__main__": ThreadingHTTPServer(("127.0.0.1",int(os.environ.get("PORT","3110"))),Proxy).serve_forever()
