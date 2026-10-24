# AGP Studios system architecture

This repo contains the AGP Studios web portal and the Python API services that back it.

## Active components

- `apps/ashat-hub-web/` — Vite + React portal. The public-facing site, member
  area, and admin control panel UI.
- `services/python-api/` — Python API service. Serves the public `/api/*`
  surface: authentication, OIDC, account, support, and admin endpoints.

## Request boundary

```text
Browser -> Python API (/api/*)
```

The portal calls the Python API directly. The Python service owns authentication,
sessions, OIDC/SAML-style discovery, account and support data, and the admin
control-panel surface.

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
