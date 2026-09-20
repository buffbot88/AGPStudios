# Ashat Hub Python API contracts

These are the retained public contracts for the Vite portal and Paws & Parcels.
The Python service is the public API at `/api/*`; Rust is not part of the contract.

## Public endpoints

| Method | Path | Success | Purpose |
|---|---|---:|---|
| GET | `/api/game-status` | 200 | Paws health projection: `online`, `source`, `health` |
| GET | `/api/games` | 200/503 | Public homepage games list: `{games: [{slug, title, summary, status, tags, url, poster, featured}]}` sorted by `sort_order` |
| GET | `/api/news` | 200/503 | Public homepage studio news: `{news: [{id, title, summary, url, published_at}]}` newest first |
| GET | `/api/auth/session` | 200 | `{authenticated: boolean, user?: User}` |
| POST | `/api/auth/login` | 200 | JSON `{identifier, password}`; returns `{user}` and secure session cookie |
| POST | `/api/auth/register` | 201 | JSON `{username, email, password, display_name}`; activates the account immediately |
| POST | `/api/auth/logout` | 200 | Revokes the current session and expires the cookie |
| GET | `/api/auth/verify-email?token=...` | 200 | Verifies a single-use email token |
| POST | `/api/auth/verify-email/resend` | 202 | JSON `{identifier}`; sends a fresh verification mail |
| GET | `/api/auth/me` | 200/401 | Alias of session for authenticated callers |
| GET | `/api/account/summary` | 200/401 | Authenticated account and support summary; shape documented below |
| PUT | `/api/account` | 200/401 | Authenticated account update; requirements documented below |
| DELETE | `/api/account` | 200/401 | Authenticated account self-deletion. JSON body `{confirm: 'DELETE'}` required (400 otherwise). Deletes the user row, all associated data and sessions, and expires the `ashat_rust_sid` and `ashat_csrf` cookies in the response. |
| GET | `/api/support` | 200/401 | Authenticated ticket list |
| POST | `/api/support` | 201/401 | Authenticated ticket creation |
| GET | `/api/support/:id` | 200/401/404 | Authenticated ticket and replies |
| POST | `/api/support/:id/reply` | 200/401/404 | Authenticated reply |

### Admin endpoints (session + role: Admin + CSRF header required)

| Method | Path | Success | Purpose |
|---|---|---:|---|
| POST | `/api/admin/audit/prune` | 200 | JSON `{before_id: int}`; deletes all audit events with `id < before_id`, then records an `admin.audit.pruned` event. Returns `{ok, deleted}`. |
| POST | `/api/admin/users/role` | 200 | JSON `{user_id, role}`; see request-body reference below |
| POST | `/api/admin/users/ban` | 200 | JSON `{user_id, banned: bool}`; see request-body reference below |
| POST | `/api/admin/users/status` | 200 | JSON `{user_id, status}`; see request-body reference below |
| DELETE | `/api/admin/users/:id` | 200 | Permanently deletes a member: ticket replies, tickets, sessions, email verifications, then the user row. Self-delete returns 409. Audit event `admin.user.deleted` is recorded. |

### Request-body reference

All admin user mutations require the CSRF header (`X-CSRF-Token`) in addition to the
admin session; a missing/mismatched token returns 403 `csrf token required`.

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
  "user": { "id", "username", "email", "display_name", "role", "diamonds" },
  "stats": { "support_tickets": 0 }
}
```

`stats.support_tickets` counts the member's support tickets. Fields the frontend
previously assumed here — `projects`, `deployments`, `conversation_messages` —
are **not** part of this response; consuming them crashes on `undefined`.

#### PUT `/api/account`

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

## OIDC issuer

Issuer: `https://www.agpstudios.org/api/oauth`

- `GET /.well-known/openid-configuration`
- `GET /.well-known/jwks.json`
- `GET /authorize`
- `POST /authorize`
- `POST /token`
- `GET /userinfo`

The authorization flow is `code` + PKCE `S256`. ID tokens use RS256. The signing
key is loaded from `ASHAT_OIDC_PRIVATE_KEY_FILE`; private material is not stored
in the repository. Authorization codes are single-use and expire after 10 minutes.

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
auth boundary without requiring credentials or mutating data.
