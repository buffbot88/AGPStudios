# Ashat Hub Python API contracts

Live games: **Paws & Parcels** and **Everenfell** (see
`docs/system-architecture.md`). The Python service owns the public `/api/*`
contract the Vite portal consumes.

> Stub warning: `password-reset/request`, `auth/github`, `account/github`,
> `admin/github/push`, and `admin/system/update` are listed below as stubs.
> They are not implemented in `services/python-api/proxy.py` and currently
> return `404 not_implemented`. Do not link them as working flows. The World
> of Ra ticket/grant flow is legacy and not one of the two live games;
> Everenfell access uses OIDC + the S1 entitlement instead.

## Public endpoints

| Method | Path | Success | Purpose |
|---|---|---:|---|
| GET | `/api/game-status` | 200 | Paws health projection: `online`, `source`, `health` |
| POST | `/api/online-presence` | 200 | Public browser heartbeat with `{visitor_id}`; returns `{online_users}` from recent website visitors plus live Paws players |
| GET | `/api/games` | 200/503 | Public homepage games list: `{games: [{slug, title, summary, status, tags, url, poster, featured}]}` sorted by `sort_order` |
| GET | `/api/news` | 200/503 | Public homepage studio news: `{news: [{id, title, summary, url, published_at}]}` newest first — curated `studio_news` rows merged with recent commits across buffbot88's public GitHub repos (cached 30 min; curated rows alone when GitHub is unreachable). Commit items use `id: github:<sha>`, the commit subject as title, `{repo} · {subject}` as summary, and the commit URL |
| GET | `/api/auth/session` | 200 | `{authenticated: boolean, user?: User}` |
| POST | `/api/auth/login` | 200 | JSON `{identifier, password}`; returns `{user}` and secure session cookie |
| POST | `/api/auth/register` | 201 | JSON `{username, email, password, display_name}`; activates the account immediately |
| POST | `/api/auth/logout` | 200 | Revokes the current session and expires the cookie |
| GET | `/api/auth/verify-email?token=...` | 200 | Verifies a single-use email token |
| POST | `/api/auth/verify-email/resend` | 202 | JSON `{identifier}`; sends a fresh verification mail |
| POST | `/api/telemetry/client-error` | 202 | Unauthenticated browser error report for after-the-fact review; shape and limits documented below |
| POST | `/api/game/ticket` | 200/401/403/429/503 | Legacy World of Ra one-time ticket (stub/legacy — not a live game); requires alpha entitlement |
| POST | `/api/acoins/order` | 201/400/401/403/503 | Creates a PayPal order for one fixed ACoins package; returns an approval URL |
| POST | `/api/acoins/capture` | 200/401/403/404/409/503 | Captures the signed-in user's PayPal order and credits ACoins once after verified capture |
| POST | `/api/auth/password-reset/request` | 404 (stub) | Not implemented; documented shape only. Returns `404 not_implemented`. |
| GET | `/api/auth/github` | 404 (stub) | Not implemented; documented shape only. Returns `404 not_implemented`. |
| GET | `/api/auth/me` | 200/401 | Alias of session for authenticated callers |
| GET | `/api/account/summary` | 200/401 | Authenticated account and support summary; shape documented below |
| PUT | `/api/account` | 200/401 | Authenticated account update; requirements documented below |
| DELETE | `/api/account` | 200/401 | Authenticated account self-deletion. JSON body `{confirm: 'DELETE'}` required (400 otherwise). Deletes the user row, all associated data and sessions, and expires the `ashat_rust_sid` and `ashat_csrf` cookies in the response. |
| POST | `/api/account/github` | 404 (stub) | Not implemented. Always 404. |
| GET | `/api/support` | 200/401 | Authenticated ticket list |
| POST | `/api/support` | 201/401 | Authenticated ticket creation |
| GET | `/api/support/:id` | 200/401/404 | Authenticated ticket and replies |
| POST | `/api/support/:id/reply` | 200/401/404 | Authenticated reply |
| GET | `/api/notifications` | 200/401 | Authenticated notification list (staff ticket replies + account moderation); shape documented below |
| POST | `/api/notifications/read` | 200/401 | Mark notifications read; `{all: true}` or `{ids: [...]}` |

### Admin endpoints (session + role: Admin + CSRF header required)

| Method | Path | Success | Purpose |
|---|---|---:|---|
| POST | `/api/admin/audit/prune` | 200 | JSON `{before_id: int}`; deletes all audit events with `id < before_id`, then records an `admin.audit.pruned` event. Returns `{ok, deleted}`. |
| GET | `/api/admin/client-errors` | 200/403 | Reported client errors, newest first, paginated; `status` filter below |
| POST | `/api/admin/client-errors/status` | 200 | JSON `{id, status: 'new'|'resolved'}`; row-level triage |
| POST | `/api/admin/client-errors/mute` | 200 | JSON `{id, muted: bool}`; message-level mute |
| POST | `/api/admin/users/role` | 200 | JSON `{user_id, role}`; see request-body reference below |
| POST | `/api/admin/world-of-ra/grant` | 200 | JSON `{user_id}`; grants World of Ra alpha entitlement |
| POST | `/api/admin/users/ban` | 200 | JSON `{user_id, banned: bool}`; see request-body reference below |
| POST | `/api/admin/users/status` | 200 | JSON `{user_id, status}`; see request-body reference below |
| POST | `/api/admin/support/:id/reply` | 200 | Staff reply on a member's ticket; JSON `{message}`; creates the `is_staff=1` reply member notifications are built from |
| GET | `/api/admin/summary` | 200 | Admin overview counts |
| GET | `/api/admin/users` | 200 | Filtered admin user list; optional `q` query |
| GET | `/api/admin/users/:id/moderation` | 200 | Per-user moderation timeline; see shape reference below |
| GET | `/api/admin/database/status` | 200 | Database/Paws health plus the schema-migration summary |
| POST | `/api/admin/database/migrations/apply` | 200 | Run pending migrations (or baseline existing ones) |
| GET | `/api/admin/settings` | 200 | Runtime API settings |
| GET | `/api/admin/payments/settings` | 200 | PayPal ACoins settings; never returns the client secret |
| POST | `/api/admin/payments/settings` | 200 | Save PayPal email, Client ID, encrypted Client Secret, and sandbox/live mode |
| GET | `/api/admin/repo-status` | 200 | Live/repository commit comparison |
| GET | `/api/admin/games` | 200 | Admin game list incl. poster URLs |
| PUT | `/api/admin/games/:slug` | 200 | Update game poster URL without a migration; see shape reference below |
| POST | `/api/admin/github/push` | 404 (stub) | Not implemented. Returns `404 not_implemented`. |
| POST | `/api/admin/system/update` | 404 (stub) | Not implemented. Returns `404 not_implemented`. |
| POST | `/api/admin/everenfell/s1` | 200 | JSON `{user_id, enabled: bool}`; grants/revokes Everenfell S1 entitlement. Admin-only + CSRF. Only active `Admin` accounts may receive S1 (`400` otherwise). Idempotency-guarded per target + intent. |
| DELETE | `/api/admin/users/:id` | 200 | Permanently deletes a member: ticket replies, tickets, sessions, email verifications, then the user row. Self-delete returns 409. Audit event `admin.user.deleted` is recorded. |

### Request-body reference

All admin user mutations require the CSRF header (`X-CSRF-Token`) in addition to the
admin session; a missing/mismatched token returns 403 `csrf token required`.

All admin user mutations (role, status, ban) carry an **idempotency guard**: a
duplicate submission for the same `user_id` on the same endpoint within a short
window (~3 seconds) returns **409 `duplicate submission; retry shortly`** and
applies nothing. This protects against double-clicks and retried POSTs creating
duplicate audit events. Clients should treat 409 on these endpoints as "already
applied" and simply re-fetch the user list. Different endpoints and different
`user_id`s never collide with each other.

The two client-error triage endpoints share that guard, keyed by `id` instead of
`user_id` — so two different reports triaged in quick succession do not collide.

### Game access: Everenfell (live) vs World of Ra ticket (legacy)

**Everenfell (live):** OIDC client `everenfell` with PKCE `S256`. A `nonce` is
required for this client. When the user holds an S1 entitlement row, the
ID token carries `"everenfell_s1": true`. Grant/revoke via
`POST /api/admin/everenfell/s1` (`{user_id, enabled}`); only active `Admin`
accounts may be granted S1. The admin user list/detail surfaces expose
`everenfell_s1` so the grant is visible.

**World of Ra ticket (legacy/stub):** `POST /api/game/ticket` requires the
signed-in session cookie and `X-CSRF-Token`. Email verification is disabled:
signup still requires and stores an email address, but ticket eligibility does
not require email confirmation. Eligibility errors are checked in order:
`suspended`, then `alpha_not_granted`. Eligible accounts may request at most 10
tickets per five minutes. A successful ticket lasts 300 seconds. The API sends
`{user_id, username}` to `world_of_ra_mint_url` from `config.json`, using the
configured `world_of_ra_mint_key` as the Bearer token; the mint service must
return `{ticket}` with a 43-character URL-safe token. Missing configuration,
network errors, or invalid mint responses return `503 mint_unavailable`.

`POST /api/admin/world-of-ra/grant` takes `{user_id}` and records the admin
grant in `world_of_ra_entitlements`; it is session-, role-, and CSRF-protected.
This flow is retained for history but is **not** one of the two live games.

#### POST `/api/admin/users/role`

```json
{ "user_id": "string", "role": "Member" | "Pro" | "Admin" }
```

Any other `role` value returns 400 `invalid role`. Audit event: `user.role.updated`.

#### POST `/api/admin/users/status`

```json
{ "user_id": "string", "status": "active" | "disabled" | "banned" }
```

- `active`: `is_active=1`, `banned_at` cleared.
- `disabled`: `is_active=0`, `banned_at` cleared.
- `banned`: `is_active=0`, `banned_at` set.

A bare `is_active` boolean is **not** read by this endpoint — the field is `status`.
Unknown values fall back to `active`. Audit event: `user.status.updated`.

#### POST `/api/admin/users/ban`

```json
{ "user_id": "string", "banned": true | false }
```

`banned` defaults to `true` for callers that omit it, so a body of only `{"user_id"}`
still bans. `true` bans (`is_active=0`, `banned_at` set); `false` unbans
(`is_active=1`, `banned_at` cleared). Audit event: `user.banned` / `user.unbanned`.
This is the endpoint for both ban and unban — there is no separate unban route.

### Account endpoint shapes

#### GET `/api/account/summary`

Requires an authenticated session (401 otherwise). Returns the user plus a stats
object keyed by what the API actually tracks:

```json
{
  "user": { "id", "username", "email", "display_name", "role", "diamonds", "acoins_balance" },
  "stats": { "support_tickets": 0 }
}
```

`user.acoins_balance` is the AGP-wide ACoins account balance. `stats.support_tickets` counts the member's support tickets. Fields the frontend
previously assumed here — `projects`, `deployments`, `conversation_messages` —
are **not** part of this response; consuming them crashes on `undefined`.

#### GET `/api/notifications` / POST `/api/notifications/read`

Requires an authenticated session (401 otherwise). Notifications are staff
replies on the member's own tickets plus the member's own moderation-timeline
entries, newest first (max 20). `id` is an opaque ref (`reply:<reply_id>` or
`mod:<event_id>`) the client passes back to mark it read.

```json
{
  "notifications": [
    { "id": "reply:abc123", "kind": "ticket_reply", "ticket_id": "t1",
      "subject": "string", "message": "string", "created_at": 1700000000, "read": false },
    { "id": "mod:12", "kind": "account",
      "subject": "Your role was updated", "message": "role: Pro", "created_at": 1700000000, "read": false }
  ],
  "unread_count": 1
}
```

`POST /api/notifications/read` requires the CSRF header. Body is either
`{all: true}` (marks every currently unread item read) or `{ids: [...]}`
(one or more `reply:`/`mod:` refs). Returns `{ok: true, read: n}`.

#### ACoins PayPal purchases

`POST /api/acoins/order` and `/api/acoins/capture` require an authenticated session and
CSRF token. Only the fixed USD packages `{acoins: 1000}`, `{acoins: 5000}`, and
`{acoins: 10000}` are accepted. Credentials are stored encrypted with `payment_encryption_key` from the ignored
root `config.json`; generate a Fernet key with
`python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
and keep the config file private. Capture
amount, currency, PayPal order identity, and completion status are checked server-side
before the account balance is credited. The Admin CP never returns the saved secret.

### PUT `/api/account`

Requires an authenticated session and the CSRF header. Body must contain all three
required fields — omitted/blank fields return 400 `username, email, and display_name
are required`:

```json
{ "username": "string", "email": "string", "display_name": "string" }
```

An **absent** `display_name` key falls back to the current value; a **blank**
`display_name` (or blank `username`/`email`) returns 400. Extra fields (e.g. a
tag name shown in the UI) are accepted but ignored. Behavior:

- `username` and `email` are unique-checked against other users; a collision
  returns 409 `username or email already in use`.
- On success returns `{ "user": { ...updated safe user... } }` — the caller should
  replace its session-cached user with this value.
- The API does not expose GitHub unlinking; only linking exists, so a
  `POST /api/account/github` currently returns 404.

#### GET `/api/admin/users`

Requires an admin session (403 `admin authentication required` otherwise — not 401;
the admin guard collapses both no-session and non-admin into one 403). Returns the
first 50 matching users ordered by username:

```json
{
  "users": [
    {
      "id": "string",
      "username": "string",
      "email": "string",
      "display_name": "string",
      "role": "Member" | "Pro" | "Admin",
      "diamonds": 0,
      "is_active": 1,
      "banned_at": null,
      "email_verified_at": null
    }
  ],
  "page": 1,
  "page_size": 50
}
```

Field notes:

- `is_active` is the DB `TINYINT(1)` value — `0`/`1` integer, **not** a boolean.
  The frontend's enable/disable and ban checks must treat any truthy value as
  active; strict `=== true` comparisons silently fail.
- `banned_at` is a unix-seconds timestamp when banned, `null` otherwise. A user
  can be disabled (`is_active=0`, `banned_at=null`) without being banned.
- `email_verified_at` is a unix-seconds timestamp, or `null` when unverified.
- `diamonds` is the in-game currency balance; present in this listing even
  though `GET /api/account/summary`'s safe user omits it in some paths.
- `page` echoes the `?page=` query (default 1); `page_size` is fixed at 50.
  There is no total-count field — paginate until a page returns fewer than 50
  rows.
- Filtering: optional `?q=` does a `LIKE %q%` match against `username`, `email`,
  and `display_name` combined with OR; it is not an exact match.

#### GET `/api/admin/database/status` — `migrations` object

The existing database-status response carries the migration state, so the admin
CP needs no extra request. (Migration files themselves are owned by the runner
in `services/python-api/migrate.py`; this endpoint only reports and applies.)

```json
{
  "database": "healthy",
  "version": "3.x",
  "paws": { "status": "ok" },
  "migrations": {
    "tracked": true,
    "tracking_table": "schema_migrations",
    "current_revision": "0001_initial_sqlite.sql",
    "applied": [
      { "filename": "0001_initial_sqlite.sql", "applied_at": 1700000000, "statements": 31, "applied_by": "admin:boss" }
    ],
    "pending": [{ "filename": "0002_next.sql", "statements": 3 }],
    "drifted": ["0001_initial_sqlite.sql"],
    "missing": ["0099_removed.sql"]
  }
}
```

- `tracked` is `false` when the `schema_migrations` table doesn't exist yet — a
  database migrated by hand before the runner existed. That is reported as
  state, not an error; every file then shows as `pending`.
- `applied` is in application order; `current_revision` is its last entry, or
  `null` when nothing is recorded.
- `drifted` lists applied files whose contents changed since they were applied
  (checksum mismatch) — they are **not** re-run automatically.
- `missing` lists recorded files that are no longer in the repository.

#### POST `/api/admin/database/migrations/apply`

Requires an admin session and the CSRF header.

```json
{ "baseline": false }
```

- Default (`baseline` omitted/false): executes every **pending** migration in
  filename order and records each one only after all of its statements succeed.
  Returns `{"ok": true, "baseline": false, "applied": ["0002_next.sql"]}`;
  `applied` is empty when nothing was pending (safe to call repeatedly).
- `{"baseline": true}`: records pending **and** drifted files as applied
  **without executing them** — for a database migrated by hand, so its history is
  recorded instead of re-applied. Use it once; prefer `migrate.py --baseline` on the
  host when you have shell access.
- Audit events: `admin.migrations.applied` / `admin.migrations.baselined`,
  `target_type='migrations'`, `detail={baseline, files}`.
- DDL runs in a SQLite transaction. A migration that fails is rolled back and
  left unrecorded. Fix the file and call again; 503 `migration unavailable: …`
  carries the SQLite error.
- This is the convenience path. Deployments should still gate on
  `services/python-api/migrate.py --check`.

#### GET `/api/admin/users/:id/moderation`

Requires an admin session (403 otherwise). Returns the user's moderation
timeline, newest first, from the `user_moderation_history` table.

```json
{
  "user_id": "string",
  "events": [
    {
      "id": 12,
      "user_id": "string",
      "action": "user.banned",
      "actor_id": "string",
      "actor_name": "string",
      "detail": "banned",
      "created_at": 1700000000
    }
  ]
}
```

- `action` values: `user.role.updated`, `user.status.updated`, `user.banned`,
  `user.unbanned` — same vocabulary as the admin audit events for these flows.
- `detail` is a short human-readable summary: `role: <role>`,
  `status: <status>`, `banned`, or `unbanned`.
- `created_at` is unix seconds; `id` is a monotonically increasing sequence
  used as a stable tiebreaker for ordering.
- Events are recorded on every successful role/status/ban mutation (idempotency
  409s record nothing). History is **retained after the user is deleted** — the
  table has no FK and the deletion paths do not purge it.
- Limited to the 100 most recent events; there is no pagination yet.

#### PUT `/api/admin/games/:slug`

Requires an admin session and the CSRF header. Updates a game's `poster` URL in
place — no migration or redeploy needed; the public `GET /api/games` reflects
the change immediately. The homepage carousel and game cards pick up new art on
their next load.

```json
{ "poster": "/new-poster.svg" }
```

- `poster` must be a string, trimmed; max length 400 (400 `poster url required`
  when missing/not a string, 400 `poster url too long (max 400)` when over).
- Unknown slug returns 404 `game not found`.
- Success returns `{ "ok": true, "slug": "...", "poster": "..." }`.
- Other game fields (title, summary, status, tags, featured) are **not**
  editable here yet — they still require the seed migrations.
- The endpoint does not validate that the URL resolves or is an image; the
  frontend renders a poster fallback when the art fails to load.

#### GET `/api/admin/games`

Requires an admin session (403 otherwise). Returns all games with their
poster URLs for the admin CP:

```json
{ "games": [{ "slug", "title", "summary", "status", "tags", "url", "poster", "featured", "sort_order" }] }
```

Same shape as the public `GET /api/games` rows plus `sort_order`.

#### POST `/api/telemetry/client-error`

Public (no session, no CSRF) — the pages that can crash are the ones anyone can
load. The body is untrusted log data; the server truncates every field rather
than validating it:

```json
{
  "source": "window.onerror",
  "message": "Cannot read properties of undefined (reading 'title')",
  "detail": "Support couldn't load · MemberSurfaces (MemberSurfaces.tsx:23:34)",
  "count": 3,
  "occurrences": ["22:24:04 · ...", "22:24:02 · ..."],
  "path": "/"
}
```

- `source` is one of `window.onerror`, `unhandledrejection`, `boundary` (the
  React error boundary), `shape-error` (a response the frontend rejected at the
  fetch boundary for being the wrong shape), or `shape-drift` (a field a
  safe-helper had to fall back on because its shape wasn't what the UI expects);
  anything else is stored as sent.
- `message` is required (400 `message required`); `count` defaults to 1 and is
  clamped to 1..10000; `occurrences` keeps the first 5 entries and `path` is
  reduced to its path component (query string stripped).
- Success is `202 {"logged": true}`. The endpoint is fire-and-forget from the
  browser: a 429, 413, or 503 is never retried and never surfaced to the user.
- Limits: bodies over 8192 bytes are `413 error report too large`; more than 20
  reports per minute from one IP is `429 too many error reports`. The limit is
  per IP in memory only — **no IP address is stored anywhere**.
- `user_id` is attached server-side from the session cookie when one is
  present; the client never sends it.
- Rows older than 30 days are deleted opportunistically (at most once an hour
  per process), so the table is bounded without a cron job.
- A successful report also writes one `[client-error] ...` line to stdout, so
  container/journald logs keep a copy alongside the table.

#### GET `/api/admin/client-errors`

Requires an admin session (403 otherwise). Returns up to 50 reports, newest
first, filtered by `status`:

| `status` | Rows returned |
|---|---|
| `new` (default) | Not resolved and not covered by a message mute |
| `resolved` | Row-level triage marked resolved |
| `muted` | Covered by a message-level mute (whatever the row state) |
| `all` | Everything, no filter |

Any other value returns 400 `invalid status filter`. Rows carry their effective
triage state, so a client never has to derive it:

```json
{
  "errors": [{
    "id": 12, "source": "boundary", "message": "...", "detail": "...",
    "path": "/", "count": 3, "occurrences": "[\"22:24:04 · ...\"]",
    "user_id": "", "user_agent": "Mozilla/5.0 ...", "created_at": 1763835844,
    "triage_status": "new", "message_hash": "9f2c…", "triaged_at": 0,
    "triaged_by": "", "muted_at": 0, "muted_by": ""
  }],
  "page": 1, "page_size": 50, "status": "new"
}
```

`occurrences` is a JSON-encoded string (TEXT column), `created_at` is unix
seconds, and `user_id` is empty for signed-out visitors. `triage_status` is
`new`, `resolved`, or `muted`; **a message mute outranks the row's own state**,
which is why a resolved row whose message is muted reports `muted`.
`message_hash` is the lower-hex SHA-256 of the message — it identifies the message, not the row.
This is client-reported log data: it may repeat, and it must never be treated as
fact or joined against users. The admin CP renders it in the System tab's
**Client errors** panel.

#### POST `/api/admin/client-errors/status`

```json
{ "id": 12, "status": "resolved" | "new" }
```

Row-level triage. `resolved` records the judgement for that one
report; `new` removes it, reopening the report. Any other value returns 400
`invalid status`, a non-numeric `id` returns 400 `invalid client error id`, and an
id with no log row returns 404 `client error not found`. Audit event:
`client_error.status.updated`, targeted at the report rather than a user (so no
moderation-history entry is written).

#### POST `/api/admin/client-errors/mute`

```json
{ "id": 12, "muted": true | false }
```

Message-level mute. The row only identifies **which message** to mute — the mute
then covers every report of that message, including ones already logged and ones
arriving later, until it is unmuted. `muted` defaults to `true`, mirroring
`/api/admin/users/ban`. Mutes key on the lower-hex SHA-256 of the message and are applied when the
list is read, so they also survive the log's 30-day retention pass; the mutes
table is deliberately never pruned. Audit event: `client_error.message.muted` /
`client_error.message.unmuted`.

## Frontend payload validation

The portal validates the success payloads it reads against these contracts, in
`apps/ashat-hub-web/src/components/schemas.ts`, at the fetch boundary. A
rejected payload throws before any component sees it, naming the endpoint and
field (`GET /api/admin/users → users[0].is_active: expected number, got
undefined`) and reporting itself to `/api/telemetry/client-error` as
`shape-error`.

What that means for anyone changing the API:

- **Adding** a field is always safe — extra keys are ignored.
- **Renaming or removing** a validated field breaks the client with a clear
error instead of a blank. Validated today: `/api/games`, `/api/news`,
  `/api/auth/session`, `/api/account/summary`, `/api/support` and
  `/api/support/:id`, `/api/admin/summary`, `/api/admin/users`,
  `/api/admin/users/:id/moderation`, `/api/admin/database/status`,
  `/api/admin/games`, `/api/admin/client-errors`, `/api/admin/settings`.
- **Endpoints without a schema are not checked**, so coverage grows deliberately:
  add an entry in `schemas.ts` when a shape is documented here.
- Timestamps may be unix seconds or a SQL date string (the API does both), and
  numeric fields may be sent as numeric strings; those are accepted.

## OIDC issuer

Issuer: `https://www.agpstudios.org/api/oauth`

Live OIDC clients: `paws-and-parcels` and `everenfell` (see
`config.example.json`). `everenfell` requires a `nonce` and receives the
`everenfell_s1` claim when entitled.

- `GET /.well-known/openid-configuration`
- `GET /.well-known/jwks.json`
- `GET /authorize`
- `POST /authorize`
- `POST /token`
- `GET /userinfo`

The authorization flow is `code` + PKCE `S256`. ID tokens use RS256. The signing
key path is set by `oidc_private_key_file` in the ignored `config.json`; private
material is not stored in the repository. Authorization codes are single-use and expire after 10 minutes.

## User shape

```json
{
  "id": "string",
  "username": "string",
  "email": "string",
  "display_name": "string",
  "role": "string"
}
```

## Error rules

- `401`: no valid session for an authenticated endpoint.
- `404`: unknown/unmigrated API endpoint or missing resource.
- `409`: duplicate account data.
- `429`: login rate limit.
- `503`: dependency/database unavailable.
- JSON errors use `{ "error": { "code": "...", "message": "..." } }` where available.

## Compatibility evidence

The smoke suite is `services/python-api/test_contracts.py`. It checks the live
public discovery, JWKS, health, session, status, authorize validation, and member
auth boundary without requiring credentials or mutating data. The frontend
contract suite (`tests/test_frontend_contracts.py`) repeats the discovery/JWKS
shape checks against the configured API origin, including issuer-derived endpoint
URLs, PKCE `S256`, and RSA `RS256` key metadata; CI runs it against the mock API.
