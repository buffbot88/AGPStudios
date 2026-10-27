#!/usr/bin/env python3
"""Python API for the retained Ashat Hub surface."""
from base64 import b64encode, urlsafe_b64encode, urlsafe_b64decode
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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

# Registration hardening: strict username/email/password filters, a
# proof-of-work human-verification challenge (no third-party captcha keys to
# manage), and a per-IP registration budget. Challenges are single-use with
# a 10-minute TTL; the browser solves one with SubtleCrypto in a couple of
# seconds before POSTing, which prices out naive bot registrations.
REGISTER_ATTEMPTS = {}
REGISTER_BUDGET = 5                # registrations per IP per window
REGISTER_WINDOW = 600.0            # seconds
REGISTER_CHALLENGES = {}
REGISTER_CHALLENGE_TTL = 600.0
REGISTER_CHALLENGE_DIFFICULTY = 4  # leading zero hex chars of sha256(challenge + nonce)

USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,29}")
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "system", "support", "help", "helpdesk",
    "moderator", "mod", "staff", "owner", "official", "agp", "agpstudios",
    "ashat", "security", "abuse", "webmaster", "postmaster", "hostmaster",
    "info", "mail", "email", "news", "games", "game", "server", "console",
    "api", "bot", "robot", "guest", "anonymous", "null", "undefined",
    "test", "tester", "demo", "member", "user", "account", "billing",
    "payment", "paypal", "discord", "youtube", "twitch", "twitter",
    "steam", "minecraft", "paws", "parcels", "everenfell",
})
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
DISPOSABLE_EMAIL_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamailblock.com",
    "sharklasers.com", "grr.la", "10minutemail.com", "10minutemail.net",
    "tempmail.com", "temp-mail.org", "tempail.com", "throwawaymail.com",
    "yopmail.com", "yopmail.fr", "yopmail.net", "maildrop.cc",
    "getnada.com", "mohmal.com", "fakemail.net", "emailondeck.com",
    "trashmail.com", "trashmail.net", "spamgourmet.com", "bugmenot.com",
    "mintemail.com", "mytemp.email", "dispostable.com", "harakirimail.com",
    "inboxalias.com", "mailnesia.com", "easytrashmail.com", "dodgit.com",
})
COMMON_PASSWORDS = frozenset({
    "password", "password1", "password123", "12345678", "123456789",
    "qwerty", "qwerty123", "letmein", "welcome", "welcome1", "admin123",
    "agpstudios", "pawsandparcels", "everenfell", "monkeyking", "ashat",
    "iloveyou", "football", "baseball", "dragon", "sunshine", "master",
    "shadow", "abc12345", "passw0rd",
})


def valid_username(username):
    """Return an error message when the username is unacceptable, else None."""
    if not isinstance(username, str) or not username:
        return "a username is required"
    if not USERNAME_RE.fullmatch(username):
        return "usernames must start with a letter and use 3-30 letters, numbers, _ or -"
    if username.lower() in RESERVED_USERNAMES:
        return "that username is reserved"
    return None


def valid_email(email):
    """Return an error message when the email is unacceptable, else None."""
    if not isinstance(email, str) or not email:
        return "a valid email address is required"
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return "a valid email address is required"
    local, _, domain = email.rpartition("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return "a valid email address is required"
    labels = domain.split(".")
    if ".." in domain or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in labels):
        return "a valid email address is required"
    if domain.lower() in DISPOSABLE_EMAIL_DOMAINS:
        return "disposable email addresses are not accepted"
    return None


def valid_password(password):
    """Return an error message when the password is unacceptable, else None."""
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return "passwords must be 8-128 characters"
    if password.lower() in COMMON_PASSWORDS:
        return "that password is too common"
    return None

# Site settings (Website admin tab): key-value overrides with code-side
# defaults, so an empty table means stock behavior. String keys hold homepage
# content; int keys hold registration controls as clamped integers.
SITE_SETTING_DEFAULTS = {
    "featured_video.embed_url": "https://www.youtube-nocookie.com/embed/ky8rKHSVgNw?rel=0",
    "featured_video.watch_url": "https://www.youtube.com/watch?v=ky8rKHSVgNw",
    "footer.tagline": "A Home For Playable Worlds.",
    "footer.discord_url": "https://discord.gg/nKPtUF4KHt",
    "footer.youtube_url": "https://www.youtube.com/@stressthismess",
}
SITE_INT_SETTINGS = {
    # key: (default, min, max)
    "register.challenge_difficulty": (4, 1, 8),
    "register.budget": (5, 1, 100),
    "register.window_seconds": (600, 60, 3600),
    # Referrals: each signup through a link credits both sides this many
    # ACoins; each link pays out at most uses_per_day bonuses per rolling 24h.
    "referral.bonus_acoins": (100, 0, 10000),
    "referral.uses_per_day": (10, 1, 1000),
    # Anti-farming: at most this many accounts may ever register from one IP.
    # 0 is not offered (the range starts at 1); the cap counts successful
    # registrations, not attempts.
    "register.max_accounts_per_ip": (2, 1, 100),
}
SITE_STRING_LIMITS = {
    "featured_video.embed_url": 500,
    "featured_video.watch_url": 500,
    "footer.tagline": 160,
    "footer.discord_url": 500,
    "footer.youtube_url": 500,
}


def site_setting_value(cur, key):
    """Effective string value for a setting key (DB override or default)."""
    default = SITE_SETTING_DEFAULTS.get(key, "")
    try:
        cur.execute("SELECT value FROM site_settings WHERE key=?", (key,))
        row = cur.fetchone()
        if not row:
            return default
        return row["value"]
    except Exception:
        return default


def site_setting_int(cur, key):
    """Effective clamped integer for a registration-control key."""
    default, lo, hi = SITE_INT_SETTINGS[key]
    try:
        value = int(str(site_setting_value(cur, key)).strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, value))


def site_content_payload(cur):
    """Public homepage content for GET /api/site/content."""
    get = lambda key: site_setting_value(cur, key)
    return {
        "featured_video": {
            "title": "Featured video",
            "embed_url": get("featured_video.embed_url"),
            "watch_url": get("featured_video.watch_url"),
        },
        "footer": {
            "tagline": get("footer.tagline"),
            "discord_url": get("footer.discord_url"),
            "youtube_url": get("footer.youtube_url"),
        },
    }


# Studio news from GitHub: recent commits across buffbot88's public repos,
# merged with the curated studio_news rows. The unauthenticated GitHub API
# budget is 60 requests/hour per IP; one refresh costs 1 (repo list) + up to
# GITHUB_NEWS_MAX_REPOS (one commit list each), so a 30-minute cache keeps
# usage around 22/hour. Any GitHub failure degrades to the curated rows
# (or the last good cache) instead of failing the endpoint.
GITHUB_USER = "buffbot88"
GITHUB_NEWS_TTL = 1800
GITHUB_NEWS_MAX_REPOS = 10
GITHUB_NEWS_PER_REPO = 3
GITHUB_NEWS_CACHE = {"items": [], "fetched_at": 0.0}
GITHUB_NEWS_LOCK = threading.Lock()
# Process start for the admin runtime panel (uptime_seconds). Set at import so
# a fresh deploy/restart reads zero, not a guess.
STARTED_AT = time.time()
NEWS_REFRESHER_STARTED = False
NEWS_REFRESHER_LOCK = threading.Lock()

# The tiny summarizer: turn raw commit subjects into news-friendly lines.
# Conventional commits get a human verb ("feat: x" -> "New: x"); merge
# commits and empty subjects are skipped as noise.
CONVENTIONAL_RE = re.compile(r"^(feat|fix|docs|style|refactor|perf|test|chore|build|ci|revert)(\([^\)]*\))?\s*:\s*(.+)$", re.IGNORECASE)
COMMIT_VERBS = {"feat": "New", "fix": "Fixed", "docs": "Docs", "style": "Polish",
                "refactor": "Reworked", "perf": "Faster", "test": "Tests",
                "chore": "Upkeep", "build": "Build", "ci": "CI", "revert": "Reverted"}


def summarize_commit(repo, message):
    """Turn a raw commit message into a news-friendly (title, summary) pair.

    Returns None for noise (merge commits, empty subjects) so those rows
    never surface on the homepage.
    """
    subject = (message or "").strip().split("\n", 1)[0].strip()[:140]
    if not subject or subject.lower().startswith("merge "):
        return None
    match = CONVENTIONAL_RE.match(subject)
    if match:
        verb = COMMIT_VERBS.get(match.group(1).lower(), "Updated")
        rest = (match.group(3) or "").strip()[:140] or subject
        title = rest[:1].upper() + rest[1:] if rest else subject
        return (title, f"{repo} \u00b7 {verb}: {rest}")
    return (subject or repo, f"{repo} \u00b7 {subject}")


def ensure_news_refresher():
    """Keep the commit cache warm: refresh every TTL even when nobody visits.

    The endpoint also refreshes lazily on request, so a dead refresher only
    costs freshness, never availability. Starts once per process.
    """
    global NEWS_REFRESHER_STARTED
    with NEWS_REFRESHER_LOCK:
        if NEWS_REFRESHER_STARTED:
            return
        NEWS_REFRESHER_STARTED = True

    def loop():
        while True:
            time.sleep(GITHUB_NEWS_TTL)
            try:
                github_commit_news()
            except Exception:
                pass

    threading.Thread(target=loop, name="github-news-refresh", daemon=True).start()


def github_get_json(url):
    request = Request(url, headers={
        "User-Agent": "AGPStudios-Hub/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urlopen(request, timeout=8) as response:
        if response.status != 200:
            raise RuntimeError(f"github {response.status}")
        return json.loads(response.read())


def github_repo_commits(repo):
    """Latest commits on one repo as news items; [] when that repo fails."""
    try:
        commits = github_get_json(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo}/commits?per_page={GITHUB_NEWS_PER_REPO}")
        items = []
        for commit in commits if isinstance(commits, list) else []:
            sha = commit.get("sha", "")
            detail = commit.get("commit", {}) or {}
            message = (detail.get("message") or "").strip()
            if not sha or not message:
                continue
            summarized = summarize_commit(repo, message)
            if not summarized:
                continue
            title, summary = summarized
            try:
                published = int(datetime.fromisoformat(
                    ((detail.get("author", {}) or {}).get("date", "")).replace("Z", "+00:00")).timestamp())
            except (ValueError, TypeError, OverflowError, OSError):
                published = int(time.time())
            items.append({
                "id": f"github:{sha}",
                "title": title,
                "summary": summary,
                "url": commit.get("html_url") or f"https://github.com/{GITHUB_USER}/{repo}",
                "published_at": published,
            })
        return items
    except Exception:
        return []


def github_commit_news(force=False):
    """Recent commits across the user's public repos; [] when GitHub is unreachable.

    force=True skips the 30-minute cache (admin refresh button)."""
    now = time.time()
    with GITHUB_NEWS_LOCK:
        if not force and GITHUB_NEWS_CACHE["items"] and now - GITHUB_NEWS_CACHE["fetched_at"] < GITHUB_NEWS_TTL:
            return list(GITHUB_NEWS_CACHE["items"])
    try:
        repos = github_get_json(
            f"https://api.github.com/users/{GITHUB_USER}/repos?per_page=100&type=public")
        public = [r for r in repos if isinstance(r, dict) and r.get("name") and not r.get("private")]
        public.sort(key=lambda r: r.get("pushed_at", ""), reverse=True)
        names = [r["name"] for r in public[:GITHUB_NEWS_MAX_REPOS]]
        with ThreadPoolExecutor(max_workers=5) as pool:
            per_repo = pool.map(github_repo_commits, names)
        items = sorted(
            (item for commits in per_repo for item in commits),
            key=lambda item: item["published_at"], reverse=True)[:20]
    except Exception:
        with GITHUB_NEWS_LOCK:
            return list(GITHUB_NEWS_CACHE["items"])
    with GITHUB_NEWS_LOCK:
        GITHUB_NEWS_CACHE["items"] = items
        GITHUB_NEWS_CACHE["fetched_at"] = now
    return list(items)


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
        if urlparse(self.path).path == "/api/site/content":
            return self.site_content()
        if urlparse(self.path).path == "/api/auth/verify-email":
            return self.verify_email(parse_qs(urlparse(self.path).query).get('token', [''])[0])
        if urlparse(self.path).path == "/api/auth/register-challenge":
            return self.register_challenge()
        if urlparse(self.path).path.startswith("/api/admin/"):
            return self.admin_get()
        if urlparse(self.path).path == "/api/notifications":
            return self.notifications_list()
        if urlparse(self.path).path in ("/api/support", "/api/account/summary", "/api/account/transactions", "/api/account/referral") or urlparse(self.path).path.startswith("/api/support/"):
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
        if urlparse(self.path).path == "/api/notifications/read":
            return self.notifications_read()
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
        if path.startswith("/api/admin/news/"): return self.admin_news_update(path.removeprefix("/api/admin/news/"))
        if path == "/api/admin/site-settings": return self.admin_post()
        self.forward()
    def do_PATCH(self): self.forward()
    def do_DELETE(self):
        self._delete_body=self.rfile.read(int(self.headers.get('Content-Length','0')) or 0)
        if urlparse(self.path).path == "/api/account": return self.account_delete()
        if urlparse(self.path).path.startswith("/api/admin/users/"): return self.admin_user_delete(urlparse(self.path).path.rsplit('/',1)[1])
        if urlparse(self.path).path.startswith("/api/admin/news/"): return self.admin_news_delete(urlparse(self.path).path.rsplit('/',1)[1])
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
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
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
        """PUT /api/admin/games/:slug — edit a game row without a migration.
        Accepts any of poster, title, summary, status, url, featured,
        sort_order, tags; at least one known field is required."""
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        if not slug: return error(self,"game slug required")
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(form, dict): return error(self,"game fields required")
            unknown=[key for key in form if key not in ('poster','title','summary','status','url','featured','sort_order','tags')]
            if unknown: return error(self,'unknown field: ' + ', '.join(sorted(unknown)[:5]))
            updates={}
            if 'poster' in form:
                poster=form['poster']
                if not isinstance(poster,str): return error(self,"poster url required")
                poster=poster.strip()
                if len(poster) > 400: return error(self,"poster url too long (max 400)")
                updates['poster']=poster
            if 'title' in form:
                title=str(form['title']).strip()
                if not title or len(title) > 120: return error(self,"title is required (max 120)")
                updates['title']=title
            if 'summary' in form:
                summary=str(form['summary']).strip()
                if not summary or len(summary) > 500: return error(self,"summary is required (max 500)")
                updates['summary']=summary
            if 'status' in form:
                if form['status'] not in ('live','in-development','coming-soon'): return error(self,"status must be live, in-development, or coming-soon")
                updates['status']=form['status']
            if 'url' in form:
                url=str(form['url'] or '').strip()
                if len(url) > 500: return error(self,"url too long (max 500)")
                updates['url']=url
            if 'featured' in form:
                if type(form['featured']) is not bool: return error(self,"featured must be true or false")
                updates['featured']=1 if form['featured'] else 0
            if 'sort_order' in form:
                try: order=int(form['sort_order'])
                except (TypeError,ValueError): return error(self,"sort_order must be an integer")
                if not 0 <= order <= 1000: return error(self,"sort_order must be 0-1000")
                updates['sort_order']=order
            if 'tags' in form:
                tags=form['tags']
                if isinstance(tags,str): tags=[tag.strip() for tag in tags.split(',')]
                if not isinstance(tags,list) or not tags or len(tags) > 10 or any(not isinstance(tag,str) or not tag.strip() or len(tag) > 40 for tag in tags):
                    return error(self,"tags must be 1-10 short labels")
                updates['tags']=json.dumps([tag.strip() for tag in tags])
            if not updates: return error(self,"nothing to update")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT slug FROM games WHERE slug=?",(slug,))
                    if not cur.fetchone(): return error(self,"game not found",404)
                    for column, value in updates.items():
                        cur.execute(f"UPDATE games SET {column}=?,updated_at=CAST(strftime('%s','now') AS INTEGER) WHERE slug=?",(value,slug))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'game',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'site.game.updated',slug,json.dumps({'fields':sorted(updates)})))
                    cur.execute("SELECT slug,title,summary,status,tags,url,poster,featured,sort_order FROM games WHERE slug=?",(slug,))
                    row=cur.fetchone()
                    try: row["tags"]=json.loads(row.get("tags") or "[]")
                    except Exception: row["tags"]=[]
                    row["featured"]=bool(row.get("featured"))
            result={"ok":True,"slug":slug,"game":row}
            if 'poster' in updates: result["poster"]=updates["poster"]
            return oidc_json(self,result)
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
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
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
                    if path == '/api/admin/stats':
                        return oidc_json(self, self.wall_stats(cur))
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
                    if path == '/api/admin/runtime':
                        with GITHUB_NEWS_LOCK:
                            cached = list(GITHUB_NEWS_CACHE["items"])
                            fetched_at = GITHUB_NEWS_CACHE["fetched_at"]
                        return oidc_json(self,{"port": int(CONFIG.get("port", 3110)),
                            "database_path": str(CONFIG.get("database_path", "")),
                            "oidc_issuer": str(CONFIG.get("oidc_issuer", "")),
                            "probes": {"paws_health_url": str(CONFIG.get("paws_health_url", "") or ""),
                                       "paws_online_url": str(CONFIG.get("paws_online_url", "") or ""),
                                       "everenfell_online_url": str(CONFIG.get("everenfell_online_url", "") or "")},
                            "news": {"commits": len(cached),
                                     "age_seconds": int(time.time() - fetched_at) if fetched_at else None,
                                     "ttl_seconds": GITHUB_NEWS_TTL},
                            "uptime_seconds": int(time.time() - STARTED_AT)})
                    if path == '/api/admin/site-settings':
                        settings = dict(SITE_SETTING_DEFAULTS)
                        for key in SITE_INT_SETTINGS:
                            settings[key] = site_setting_int(cur, key)
                        for key in SITE_SETTING_DEFAULTS:
                            settings[key] = site_setting_value(cur, key)
                        return oidc_json(self,{"settings":settings})
                    if path == '/api/admin/news':
                        cur.execute("SELECT id,title,summary,url,published_at,created_at FROM studio_news ORDER BY published_at DESC,created_at DESC LIMIT 50")
                        return oidc_json(self,{"news":cur.fetchall()})
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

    def probe_online_count(self, key):
        """Best-effort game-server presence probe. Returns a count, or None
        when no URL is configured or the server doesn't answer."""
        url = CONFIG.get(key, "")
        if not url:
            return None
        try:
            with urlopen(url, timeout=2) as response:
                return max(0, int(json.loads(response.read()).get('online_count', 0)))
        except Exception:
            return None

    def wall_stats(self, cur):
        """Live wall stats for the admin dashboard: member signups, PayPal
        profits, and per-platform presence. Every number derives from local
        tables (users, completed acoin_orders) or a best-effort probe —
        nothing is invented, so quiet days honestly read zero and silent game
        servers read null. ACoins are studio-wide, not per-game, so there is
        deliberately no per-game spend here."""
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE date(created_at) = date('now')")
        today = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE date(created_at) >= date('now','-6 days')")
        week = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE strftime('%Y-%m',created_at) = strftime('%Y-%m','now')")
        month = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM users")
        total = cur.fetchone()['n']
        now = int(time.time())
        bounds = {
            'day': now - 86400,
            'week': now - 7 * 86400,
            'month': now - 30 * 86400,
            'year': now - 365 * 86400,
        }
        profits = {}
        for key, cutoff in bounds.items():
            cur.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM acoin_orders WHERE status='completed' AND COALESCE(completed_at,created_at) >= ?", (cutoff,))
            profits[key] = cur.fetchone()['s']
        with ONLINE_VISITORS_LOCK:
            for key, seen in list(ONLINE_VISITORS.items()):
                if now - seen >= ONLINE_VISITOR_TTL:
                    ONLINE_VISITORS.pop(key, None)
            website_online = len(ONLINE_VISITORS)
        return {"signups": {"today": today, "week": week, "month": month, "total": total},
                "profits_cents": profits,
                "online": {"website": website_online,
                           "paws": self.probe_online_count("paws_online_url"),
                           "everenfell": self.probe_online_count("everenfell_online_url")}}

    def acoins_order(self):
        user=self.current_user()
        if not user: return error(self,'authentication required',401)
        if not self.csrf_ok(user): return error(self,'csrf_failed',403)
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
        if not self.csrf_ok(user): return error(self,'csrf_failed',403)
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
                        cur.execute("INSERT OR IGNORE INTO account_transactions (id,user_id,kind,title,detail,amount_cents,acoins_delta,game_slug,status,created_at,receipt) VALUES (?,?,?,?,?,?,?,NULL,'completed',CAST(strftime('%s','now') AS INTEGER),?)",('txn-'+order['id'],str(user['id']),'acoins_purchase',str(order['acoins'])+' ACoins purchase','ACoins package via PayPal ('+order['environment']+')',order['amount_cents'],order['acoins'],'{"paypal_order_id":"'+order['paypal_order_id']+'","paypal_capture_id":"'+(capture_id or '')+'"}'))
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
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        path=urlparse(self.path).path; length=int(self.headers.get("Content-Length","0"))
        if path == '/api/admin/payments/settings' and not 0 <= length <= 4096: return error(self,'invalid payment settings')
        form=json.loads(self.rfile.read(length) or b'{}')
        # Audit/context for the branches below. User mutations write a moderation
        # timeline entry; client error triage has no user to attach one to, so it
        # clears mod_detail and retargets the audit event instead.
        audit_target_type='user'; audit_target_id=str(form.get('user_id',''))
        audit_detail=json.dumps({}) if path == '/api/admin/payments/settings' else json.dumps(form)
        log_moderation=True
        result_extra={}
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
                    elif path.startswith('/api/admin/support/') and path.endswith('/reply'):
                        # Staff reply on a member's ticket. Creates the is_staff=1
                        # reply that member notifications are built from.
                        ticket_id=path.removeprefix('/api/admin/support/').removesuffix('/reply').strip('/')
                        message=form.get('message','').strip() if isinstance(form,dict) else ''
                        if not ticket_id or not message:
                            return error(self,'ticket and message required')
                        cur.execute("SELECT user_id,subject FROM support_tickets WHERE id=?",(ticket_id,))
                        ticket=cur.fetchone()
                        if not ticket: return error(self,'ticket not found',404)
                        reply_id=secrets.token_hex(16)
                        cur.execute("INSERT INTO support_ticket_replies (id,ticket_id,user_id,message,is_staff)"
                                    " VALUES (?,?,?,?,1)",(reply_id,ticket_id,user['id'],message[:2000]))
                        cur.execute("UPDATE support_tickets SET updated_at=CURRENT_TIMESTAMP WHERE id=?",(ticket_id,))
                        action='support.reply.staff'
                        audit_target_type='ticket'; audit_target_id=ticket_id; log_moderation=False
                    elif path == '/api/admin/site-settings':
                        if not isinstance(form, dict) or not isinstance(form.get('settings'), dict):
                            return error(self,'settings object required')
                        incoming = form['settings']
                        unknown = [key for key in incoming if key not in SITE_SETTING_DEFAULTS and key not in SITE_INT_SETTINGS]
                        if unknown: return error(self,'unknown setting: ' + ', '.join(sorted(unknown)[:5]))
                        if len(incoming) > 32: return error(self,'too many settings at once')
                        saved = {}
                        for key, value in incoming.items():
                            if key in SITE_INT_SETTINGS:
                                default, lo, hi = SITE_INT_SETTINGS[key]
                                try: number = int(str(value).strip())
                                except (TypeError, ValueError): return error(self,f'{key} must be an integer')
                                if not lo <= number <= hi: return error(self,f'{key} must be {lo}-{hi}')
                                saved[key] = str(number)
                            else:
                                if not isinstance(value, str): return error(self,f'{key} must be text')
                                text = value.strip()
                                if len(text) > SITE_STRING_LIMITS[key]: return error(self,f'{key} too long (max {SITE_STRING_LIMITS[key]})')
                                if key in ('footer.tagline',) and not text: return error(self,'footer tagline is required')
                                if key.endswith('_url') and text and not re.fullmatch(r'https?://[^\s]+', text): return error(self,f'{key} must be an http(s) URL')
                                saved[key] = text
                        for key, value in saved.items():
                            cur.execute("INSERT INTO site_settings (key,value,updated_by,updated_at) VALUES (?,?,?,CAST(strftime('%s','now') AS INTEGER)) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=excluded.updated_at",(key,value,str(user['id'])))
                        action='site.settings.updated'
                        audit_target_type='site_settings'; audit_target_id='website'; log_moderation=False
                        audit_detail=json.dumps({'keys':sorted(saved)})
                        result_extra={'settings':saved}
                    elif path == '/api/admin/games':
                        slug=str(form.get('slug','')).strip().lower()
                        title=str(form.get('title','')).strip()
                        summary=str(form.get('summary','')).strip()
                        status=form.get('status','in-development')
                        url=str(form.get('url') or '').strip()
                        poster=str(form.get('poster') or '').strip()
                        featured=form.get('featured',False)
                        sort_order=form.get('sort_order',0)
                        tags=form.get('tags',[])
                        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',slug) or len(slug) > 60: return error(self,'slug must be lowercase letters, numbers, and hyphens (max 60)')
                        if not title or len(title) > 120: return error(self,'title is required (max 120)')
                        if not summary or len(summary) > 500: return error(self,'summary is required (max 500)')
                        if status not in ('live','in-development','coming-soon'): return error(self,'status must be live, in-development, or coming-soon')
                        if len(url) > 500: return error(self,'url too long (max 500)')
                        if len(poster) > 400: return error(self,'poster url too long (max 400)')
                        if type(featured) is not bool: return error(self,'featured must be true or false')
                        try: sort_order=int(sort_order)
                        except (TypeError,ValueError): return error(self,'sort_order must be an integer')
                        if not 0 <= sort_order <= 1000: return error(self,'sort_order must be 0-1000')
                        if isinstance(tags,str): tags=[tag.strip() for tag in tags.split(',')]
                        if not isinstance(tags,list) or not tags or len(tags) > 10 or any(not isinstance(tag,str) or not tag.strip() or len(tag) > 40 for tag in tags):
                            return error(self,'tags must be 1-10 short labels')
                        tags=[tag.strip() for tag in tags]
                        if self.duplicate_submit(path,form,slug): return error(self,'duplicate submission; retry shortly',409)
                        cur.execute("SELECT slug FROM games WHERE slug=?",(slug,))
                        if cur.fetchone(): return error(self,'a game with that slug already exists',409)
                        game_id=secrets.token_hex(8)
                        cur.execute("INSERT INTO games (id,slug,title,summary,status,tags,url,poster,featured,sort_order,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER))",(game_id,slug,title,summary,status,json.dumps(tags),url,poster,1 if featured else 0,sort_order))
                        action='site.game.created'
                        audit_target_type='game'; audit_target_id=slug; log_moderation=False
                        audit_detail=json.dumps({'slug':slug,'title':title})
                        result_extra={'slug':slug}
                    elif path == '/api/admin/news/refresh':
                        if self.duplicate_submit(path,form): return error(self,'duplicate submission; retry shortly',409)
                        try: items=github_commit_news(force=True)
                        except Exception as exc: return error(self,f'news refresh failed: {exc}',503)
                        action='site.news.refreshed'
                        audit_target_type='studio_news'; audit_target_id='github'; log_moderation=False
                        audit_detail=json.dumps({'commits':len(items)})
                        result_extra={'commits':len(items)}
                    elif path == '/api/admin/news':
                        title=str(form.get('title','')).strip()
                        summary=str(form.get('summary','')).strip()
                        url=str(form.get('url','') or '#news').strip() or '#news'
                        if not title or len(title) > 140: return error(self,'title is required (max 140)')
                        if not summary or len(summary) > 500: return error(self,'summary is required (max 500)')
                        if len(url) > 500: return error(self,'url too long (max 500)')
                        news_id=secrets.token_hex(8)
                        cur.execute("INSERT INTO studio_news (id,title,summary,url,published_at,created_at) VALUES (?,?,?,?,CAST(strftime('%s','now') AS INTEGER),CAST(strftime('%s','now') AS INTEGER))",(news_id,title,summary,url))
                        action='site.news.created'
                        audit_target_type='studio_news'; audit_target_id=news_id; log_moderation=False
                        audit_detail=json.dumps({'title':title})
                        result_extra={'id':news_id}
                    else: return error(self,'admin endpoint not found',404)
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],action,audit_target_type,audit_target_id,audit_detail))
                    # Moderation timeline: append to the dedicated history table
                    # so the admin CP can show per-user history.
                    if log_moderation: cur.execute("INSERT INTO user_moderation_history (user_id,action,actor_id,actor_name,detail,created_at) VALUES (?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",(str(form.get('user_id','')),action,user['id'],user['username'],mod_detail))
            return oidc_json(self,{"ok":True,**result_extra})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def admin_news_update(self, news_id):
        """PUT /api/admin/news/:id — edit a curated studio_news row."""
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        if not news_id: return error(self,"news id required")
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(form, dict) or not form: return error(self,'nothing to update')
            allowed = ('title','summary','url')
            unknown = [key for key in form if key not in allowed]
            if unknown: return error(self,'unknown field: ' + ', '.join(sorted(unknown)[:5]))
            updates = {}
            if 'title' in form:
                title=str(form['title']).strip()
                if not title or len(title) > 140: return error(self,'title is required (max 140)')
                updates['title'] = title
            if 'summary' in form:
                summary=str(form['summary']).strip()
                if not summary or len(summary) > 500: return error(self,'summary is required (max 500)')
                updates['summary'] = summary
            if 'url' in form:
                url=str(form['url'] or '#news').strip() or '#news'
                if len(url) > 500: return error(self,'url too long (max 500)')
                updates['url'] = url
            if not updates: return error(self,'nothing to update')
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM studio_news WHERE id=?",(news_id,))
                    if not cur.fetchone(): return error(self,"news not found",404)
                    for column, value in updates.items():
                        cur.execute(f"UPDATE studio_news SET {column}=? WHERE id=?",(value,news_id))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'studio_news',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'site.news.updated',news_id,json.dumps({'fields':sorted(updates)})))
            return oidc_json(self,{"ok":True,"id":news_id,"updated":updates})
        except Exception as exc: return error(self,f"news unavailable: {exc}",503)

    def admin_news_delete(self, news_id):
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        if not news_id: return error(self,"news id required")
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT title FROM studio_news WHERE id=?",(news_id,))
                    row=cur.fetchone()
                    if not row: return error(self,"news not found",404)
                    cur.execute("DELETE FROM studio_news WHERE id=?",(news_id,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'studio_news',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'site.news.deleted',news_id,json.dumps({'deleted_title':row['title']})))
            return oidc_json(self,{"ok":True,"deleted":news_id})
        except Exception as exc: return error(self,f"news unavailable: {exc}",503)

    def admin_user_delete(self, uid):
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        if not uid: return error(self,"user id required")
        if uid == str(user.get('id','')): return error(self,"cannot delete your own account",409)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT username FROM users WHERE id=?",(uid,)); row=cur.fetchone()
                    if not row: return error(self,"user not found",404)
                    for table in ('support_ticket_replies','support_tickets','notification_reads','rust_sessions','sessions','email_verifications'):
                        cur.execute(f"DELETE FROM {table} WHERE user_id=?",(uid,))
                    cur.execute("DELETE FROM users WHERE id=?",(uid,))
                    cur.execute("INSERT INTO admin_audit_events (actor_id,actor_name,action,target_type,target_id,detail,created_at) VALUES (?,?,?,'user',?,?,CAST(strftime('%s','now') AS INTEGER))",(user['id'],user['username'],'admin.user.deleted',uid,json.dumps({'deleted_username':row['username']})))
            return oidc_json(self,{"ok":True,"deleted":uid})
        except Exception as exc: return error(self,f"admin unavailable: {exc}",503)

    def admin_audit_prune(self):
        user=self.current_user()
        if not user: return error(self,"admin session required",403)
        if str(user.get('role','')).lower() != 'admin': return error(self,"admin role required",403)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
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
        """Public read-only list of studio news for the homepage.

        Curated `studio_news` rows merged with recent commits across
        buffbot88's public GitHub repos (cached 30 min; DB rows alone when
        GitHub is unreachable), newest first.
        """
        ensure_news_refresher()
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
        except Exception as exc:
            return error(self, f"news unavailable: {exc}", 503)
        try:
            news = sorted(news + github_commit_news(),
                          key=lambda item: item["published_at"], reverse=True)[:20]
        except Exception:
            pass
        return oidc_json(self, {"news": news})

    def site_content(self):
        """Public homepage content (featured video, footer) for customization
        without a redeploy. Reads the Website admin tab's settings; stock
        defaults apply when nothing was ever saved."""
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    return oidc_json(self, site_content_payload(cur))
        except Exception as exc:
            return error(self, f"site content unavailable: {exc}", 503)

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
                    if path == "/api/account/referral":
                        cur.execute("SELECT code FROM referral_codes WHERE user_id=?",(user["id"],))
                        row = cur.fetchone()
                        code = row["code"] if row else self.mint_referral_code(cur, str(user["id"]))
                        cur.execute("SELECT COUNT(*) AS n FROM referral_uses WHERE code=? AND created_at > CAST(strftime('%s','now') AS INTEGER) - 86400",(code,))
                        uses_24h = cur.fetchone()["n"]
                        cur.execute("SELECT COUNT(*) AS n, COALESCE(SUM(bonus_acoins),0) AS earned FROM referral_uses WHERE code=?",(code,))
                        totals = cur.fetchone()
                        try:
                            bonus = site_setting_int(cur, "referral.bonus_acoins")
                            per_day = site_setting_int(cur, "referral.uses_per_day")
                        except Exception:
                            bonus, per_day = 100, 10
                        return oidc_json(self, {"code": code, "uses_24h": uses_24h,
                            "uses_total": totals["n"], "remaining_24h": max(0, per_day - uses_24h),
                            "rewards_earned_acoins": totals["earned"],
                            "bonus_acoins": bonus, "uses_per_day": per_day})
                    if path == "/api/account/transactions":
                        cur.execute("SELECT id,kind,title,detail,amount_cents,acoins_delta,game_slug,status,created_at,receipt FROM account_transactions WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 100",(str(user["id"]),))
                        rows=[]
                        for row in cur.fetchall():
                            try: receipt=json.loads(row["receipt"] or "{}")
                            except ValueError: receipt={}
                            if not isinstance(receipt,dict): receipt={}
                            rows.append({"id":row["id"],"kind":row["kind"],"title":row["title"],"detail":row["detail"],"amount_cents":row["amount_cents"],"acoins_delta":row["acoins_delta"],"game_slug":row["game_slug"],"status":row["status"],"created_at":row["created_at"],"receipt":receipt})
                        return oidc_json(self,{"transactions":rows})
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
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
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

    def moderation_subject(self, action):
        """Short human line for an account notification from a moderation action."""
        return {
            'user.role.updated': 'Your role was updated',
            'user.status.updated': 'Your account status was updated',
            'user.banned': 'Your account was banned',
            'user.unbanned': 'Your account was unbanned',
        }.get(str(action), str(action))

    def notification_items(self, cur, user_id):
        """Unread-candidate notifications: staff replies on the user's tickets plus
        the user's own moderation-timeline entries, newest first (max 20)."""
        cur.execute("SELECT r.id AS id, r.ticket_id AS ticket_id, t.subject AS subject, r.message AS message,"
                    " r.created_at AS created_at FROM support_ticket_replies r"
                    " JOIN support_tickets t ON t.id=r.ticket_id"
                    " WHERE t.user_id=? AND r.is_staff=1 ORDER BY r.created_at DESC LIMIT 20",(user_id,))
        replies=cur.fetchall()
        cur.execute("SELECT id, action, detail, created_at FROM user_moderation_history"
                    " WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 20",(user_id,))
        mods=cur.fetchall()
        cur.execute("SELECT ref FROM notification_reads WHERE user_id=?",(user_id,))
        read={row["ref"] for row in cur.fetchall()}
        items=[]
        for row in replies:
            ref='reply:'+str(row['id'])
            items.append({"id":ref,"kind":"ticket_reply","ticket_id":row['ticket_id'],
                            "subject":row['subject'],"message":(row['message'] or '')[:200],
                            "created_at":row['created_at'],"read":ref in read})
        for row in mods:
            ref='mod:'+str(row['id'])
            items.append({"id":ref,"kind":"account",
                            "subject":self.moderation_subject(row['action']),"message":row['detail'] or '',
                            "created_at":row['created_at'],"read":ref in read})
        def sort_key(item):
            stamp=item['created_at']
            try:
                if isinstance(stamp,(int,float)): return float(stamp)
                return time.mktime(time.strptime(str(stamp),"%Y-%m-%d %H:%M:%S"))
            except Exception: return 0.0
        items.sort(key=sort_key, reverse=True)
        return items[:20]

    def notifications_list(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    items=self.notification_items(cur,str(user['id']))
            return oidc_json(self,{"notifications":items,
                                    "unread_count":sum(1 for item in items if not item['read'])})
        except Exception as exc: return error(self,f"notifications unavailable: {exc}",503)

    def notifications_read(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(form,dict): return error(self,"invalid notification ids")
            with db() as conn:
                with conn.cursor() as cur:
                    if form.get('all'):
                        refs=[item['id'] for item in self.notification_items(cur,str(user['id'])) if not item['read']]
                    elif isinstance(form.get('ids'),list) and form['ids'] and all(
                            isinstance(ref,str) and re.fullmatch(r'(reply|mod):[A-Za-z0-9_-]+',ref) for ref in form['ids']):
                        refs=form['ids']
                    else:
                        return error(self,"invalid notification ids")
                    for ref in refs:
                        cur.execute("INSERT OR IGNORE INTO notification_reads (user_id,ref,read_at)"
                                    " VALUES (?,?,CAST(strftime('%s','now') AS INTEGER))",(str(user['id']),ref))
            return oidc_json(self,{"ok":True,"read":len(refs)})
        except Exception as exc: return error(self,f"notifications unavailable: {exc}",503)

    def account_update(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length)); username=form.get("username", "").strip(); email=form.get("email", "").strip(); display=form.get("display_name", user["display_name"]).strip()
            if not username or not email or not display: return error(self,"username, email, and display_name are required")
            problem = valid_username(username) or valid_email(email)
            if problem: return error(self, problem)
            if len(display) > 60: return error(self, "display name is required")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE (username=? OR LOWER(email)=LOWER(?)) AND id<>? LIMIT 1",(username,email,user["id"]))
                    if cur.fetchone(): return error(self,"username or email already in use",409)
                    cur.execute("UPDATE users SET username=?,email=?,display_name=? WHERE id=?",(username,email,display,user["id"]))
            return oidc_json(self,{"user":{**self.safe_user(user),"username":username,"email":email,"display_name":display}})
        except Exception as exc: return error(self,f"account unavailable: {exc}",503)

    def account_delete(self):
        user=self.current_user()
        if not user: return error(self,"authentication required",401)
        if not self.csrf_ok(user): return error(self,"csrf_failed",403)
        form=json.loads(getattr(self,'_delete_body',b'{}') or b'{}')
        if form.get('confirm') != 'DELETE': return error(self,"confirm DELETE to remove the account",400)
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    for table in ('support_ticket_replies','support_tickets','notification_reads','rust_sessions','sessions','email_verifications'):
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

    def register_challenge(self):
        """Issue a single-use proof-of-work challenge for registration."""
        now = time.time()
        for token, expires in list(REGISTER_CHALLENGES.items()):
            if expires <= now:
                del REGISTER_CHALLENGES[token]
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    difficulty = site_setting_int(cur, "register.challenge_difficulty")
        except Exception:
            difficulty = REGISTER_CHALLENGE_DIFFICULTY
        challenge = secrets.token_hex(16)
        REGISTER_CHALLENGES[challenge] = now + REGISTER_CHALLENGE_TTL
        return oidc_json(self, {"challenge": challenge,
                                "difficulty": difficulty,
                                "expires_in": int(REGISTER_CHALLENGE_TTL)})

    def register_proof_ok(self, form, difficulty):
        """Verify the proof-of-work solution; challenges are single-use."""
        proof = form.get("proof")
        if not isinstance(proof, dict):
            return False
        challenge, nonce = proof.get("challenge"), proof.get("nonce")
        if not isinstance(challenge, str) or not isinstance(nonce, str):
            return False
        if not nonce or len(nonce) > 64:
            return False
        expires = REGISTER_CHALLENGES.pop(challenge, None)
        if expires is None or expires < time.time():
            return False
        digest = sha256((challenge + nonce).encode()).hexdigest()
        return digest.startswith("0" * difficulty)

    def real_ip(self):
        """Client IP for rate limits and the per-IP account cap. Prefers the
        leftmost X-Forwarded-For entry (set by the alpha reverse proxy) and
        falls back to the direct peer, which is what local/dev traffic sees."""
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
        return self.client_address[0]

    def mint_referral_code(self, cur, user_id):
        """Insert one permanent referral code for a user; retries on collision."""
        for _ in range(5):
            code = secrets.token_hex(6)
            try:
                cur.execute("INSERT INTO referral_codes (user_id,code,created_at) VALUES (?,?,CAST(strftime('%s','now') AS INTEGER))",(user_id,code))
                return code
            except Exception:
                continue
        raise ValueError("could not mint a referral code")

    def redeem_referral(self, cur, code, user_id):
        """Credit a referral signup. Returns a referral result dict; never raises
        for bad input — an unknown, exhausted, or disabled code still lets the
        registration succeed, it just pays nothing."""
        code = str(code or "").strip()
        if not code:
            return {"credited": False}
        try:
            bonus = site_setting_int(cur, "referral.bonus_acoins")
            per_day = site_setting_int(cur, "referral.uses_per_day")
        except Exception:
            bonus, per_day = 100, 10
        cur.execute("SELECT rc.user_id AS referrer, u.is_active AS active FROM referral_codes rc JOIN users u ON u.id=rc.user_id WHERE rc.code=?",(code,))
        row = cur.fetchone()
        if not row or row["referrer"] == user_id:
            return {"credited": False, "reason": "unknown_referral_code"}
        if not row["active"]:
            return {"credited": False, "reason": "referral_code_inactive"}
        if bonus <= 0:
            return {"credited": False, "reason": "referrals_disabled"}
        cur.execute("SELECT COUNT(*) AS n FROM referral_uses WHERE code=? AND created_at > CAST(strftime('%s','now') AS INTEGER) - 86400",(code,))
        if cur.fetchone()["n"] >= per_day:
            return {"credited": False, "reason": "referral_limit_reached"}
        referrer = row["referrer"]
        cur.execute("UPDATE users SET acoins_balance=acoins_balance+? WHERE id=?",(bonus,referrer))
        cur.execute("UPDATE users SET acoins_balance=acoins_balance+? WHERE id=?",(bonus,user_id))
        use_id = secrets.token_hex(8)
        cur.execute("INSERT INTO referral_uses (id,code,referrer_user_id,referred_user_id,bonus_acoins,created_at) VALUES (?,?,?,?,?,CAST(strftime('%s','now') AS INTEGER))",(use_id,code,referrer,user_id,bonus))
        detail = f"Referral bonus ({code})"
        cur.execute("INSERT OR IGNORE INTO account_transactions (id,user_id,kind,title,detail,amount_cents,acoins_delta,game_slug,status,created_at,receipt) VALUES (?,?,?,?,?,?,?,NULL,'completed',CAST(strftime('%s','now') AS INTEGER),?)",
                    ('txn-ref-'+use_id+'-referrer',referrer,'referral_bonus',f"{bonus} ACoins referral bonus",detail,None,bonus,'{"referral_code":"'+code+'"}'))
        cur.execute("INSERT OR IGNORE INTO account_transactions (id,user_id,kind,title,detail,amount_cents,acoins_delta,game_slug,status,created_at,receipt) VALUES (?,?,?,?,?,?,?,NULL,'completed',CAST(strftime('%s','now') AS INTEGER),?)",
                    ('txn-ref-'+use_id+'-referred',user_id,'referral_bonus',f"{bonus} ACoins referral bonus",detail,None,bonus,'{"referral_code":"'+code+'"}'))
        return {"credited": True, "bonus_acoins": bonus}

    def auth_register(self):
        client = self.real_ip()
        now = time.time()
        try:
            with db() as conn:
                with conn.cursor() as cur:
                    budget = site_setting_int(cur, "register.budget")
                    window = site_setting_int(cur, "register.window_seconds")
                    difficulty = site_setting_int(cur, "register.challenge_difficulty")
                    max_accounts = site_setting_int(cur, "register.max_accounts_per_ip")
        except Exception:
            budget, window, difficulty = REGISTER_BUDGET, REGISTER_WINDOW, REGISTER_CHALLENGE_DIFFICULTY
            max_accounts = 2
        REGISTER_ATTEMPTS[client] = [stamp for stamp in REGISTER_ATTEMPTS.get(client, []) if now - stamp < window]
        if len(REGISTER_ATTEMPTS[client]) >= budget:
            return error(self, "too many registration attempts", 429)
        REGISTER_ATTEMPTS[client].append(now)
        try:
            length=int(self.headers.get("Content-Length","0")); form=json.loads(self.rfile.read(length))
            if not isinstance(form, dict):
                return error(self, "invalid registration")
            username=str(form.get("username", "")).strip(); email=str(form.get("email", "")).strip(); password=form.get("password", ""); display=str(form.get("display_name", username)).strip()
            problem = valid_username(username) or valid_email(email) or valid_password(password)
            if problem:
                return error(self, problem)
            if not display or len(display) > 60:
                return error(self, "display name is required")
            if not self.register_proof_ok(form, difficulty):
                return error(self, "human verification failed")
            with db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) AS n FROM users WHERE username=? OR LOWER(email)=LOWER(?)", (username,email))
                    if cur.fetchone()["n"]: return oidc_json(self, {"error":"username_or_email_taken"},409)
                    cur.execute("SELECT COUNT(*) AS n FROM users WHERE created_ip=?", (client,))
                    if cur.fetchone()["n"] >= max_accounts:
                        return error(self, "too many accounts from this address", 409)
                    user_id=secrets.token_hex(16); password_hash=crypt.crypt(password, crypt.mksalt(crypt.METHOD_BLOWFISH))
                    cur.execute("INSERT INTO users (id,username,email,password_hash,display_name,role,is_active,email_verified_at,created_ip) VALUES (?,?,?,?,?,'Member',1,CURRENT_TIMESTAMP,?)",(user_id,username,email,password_hash,display,client))
                    own_code = self.mint_referral_code(cur, user_id)
                    referral = self.redeem_referral(cur, form.get("referral_code"), user_id)
            result = {"registered":True,"verification_required":False,"user":{"id":user_id,"username":username,"email":email,"display_name":display,"role":"Member"},"referral_code":own_code,"referral":referral}
            return oidc_json(self, result,201)
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
