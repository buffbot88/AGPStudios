# AGP Studios system architecture

This repo contains the AGP Studios web portal and the Python API services that back it.

## Live games (two)

Only two game servers are online:

- **Paws & Parcels** (`https://pawsandparcels.agpstudios.org`) — live. OIDC client
  `paws-and-parcels`; health/presence via `paws_health_url` / `paws_online_url`.
- **Everenfell** (`https://everenfell.agpstudios.org`) — live. OIDC client
  `everenfell`; Season 1 access is gated by the `everenfell_s1_entitlements`
  table and the `everenfell_s1` ID-token claim.

Anything else that looks like a game in code, seeds, or docs — `World of Ra`,
`world-of-ra-horizon`, `more-titles`, `Omega` servers, `Galileo` — is a
stub, placeholder, or historical reference, not a live server.

## Active components

- `apps/ashat-hub-web/` — Vite + React portal. The public-facing site, member
  area, and admin control panel UI. The homepage spotlights the two live games
  above; it carries no third game card.
- `services/python-api/` — Python API service. Serves the public `/api/*`
  surface: authentication, OIDC, account, support, and admin endpoints.

## Request boundary

```text
Browser -> Python API (/api/*)
```

The portal calls the Python API directly. The Python service owns authentication,
sessions, OIDC discovery, account and support data, and the admin
control-panel surface.

## Stubbed / not implemented

These are documented in `docs/python-api-contracts.md` as stubs and return
`404 not_implemented` (via `forward()`) until they are built:

- `POST /api/auth/password-reset/request` (+ any password-reset confirm flow)
- `GET /api/auth/github` and `POST /api/account/github`
- `POST /api/admin/github/push` and `POST /api/admin/system/update`
- World of Ra ticket/grant (`POST /api/game/ticket`,
  `POST /api/admin/world-of-ra/grant`) — legacy flow, not one of the two live
  games. Everenfell access uses OIDC + S1 entitlement instead.

The portal proxies API requests during local development through the Vite dev
server. See `apps/ashat-hub-web/vite.config.ts`.

## There is no in-repo AI runtime

This repository does **not** contain an AI inference runtime, model host, or
agent gateway.

AI inference, model hosting, and agent runtimes are not part of this portal. Any
AI services deployed separately are external dependencies, not components of this
repository.

## Ports

- Python API: configured at runtime, default local port `3110`.
- Portal dev server: `apps/ashat-hub-web` dev server port (see its `vite.config.ts`).

## Public API

The public API contract is documented in `docs/python-api-contracts.md`.

## What was removed

The retired Rust runtime is kept in Git history only; it is not built or deployed
from this checkout.
