# Ashat Hub Python API contracts

These are the retained public contracts for the Vite portal and Paws & Parcels.
The Python service is the public API at `/api/*`; Rust is not part of the contract.

## Public endpoints

| Method | Path | Success | Purpose |
|---|---|---:|---|
| GET | `/api/game-status` | 200 | Paws health projection: `online`, `source`, `health` |
| GET | `/api/auth/session` | 200 | `{authenticated: boolean, user?: User}` |
| POST | `/api/auth/login` | 200 | JSON `{identifier, password}`; returns `{user}` and secure session cookie |
| POST | `/api/auth/register` | 201 | JSON `{username, email, password, display_name}`; activates the account immediately |
| POST | `/api/auth/logout` | 200 | Revokes the current session and expires the cookie |
| GET | `/api/auth/verify-email?token=...` | 200 | Verifies a single-use email token |
| POST | `/api/auth/verify-email/resend` | 202 | JSON `{identifier}`; sends a fresh verification mail |
| GET | `/api/auth/me` | 200/401 | Alias of session for authenticated callers |
| GET | `/api/account/summary` | 200/401 | Authenticated account and support summary |
| PUT | `/api/account` | 200/401 | Authenticated `{username, email, display_name}` update |
| GET | `/api/support` | 200/401 | Authenticated ticket list |
| POST | `/api/support` | 201/401 | Authenticated ticket creation |
| GET | `/api/support/:id` | 200/401/404 | Authenticated ticket and replies |
| POST | `/api/support/:id/reply` | 200/401/404 | Authenticated reply |

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
