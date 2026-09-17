# AGP Studios

AGP Studios is the web portal and Python API project for AGP Studios.

## What this repo is

- `apps/ashat-hub-web/` — Vite + React portal. Public site, member area, and admin UI.
- `services/python-api/` — Python API service. Serves the public `/api/*` surface: auth,
  OIDC, account, support, and admin endpoints.
- `docs/system-architecture.md` — what the active system is and its request boundary.
- `docs/python-api-contracts.md` — the public API contract source of truth.
- `docs/retired-components.md` — what was removed and where archival material lives.
- `docs/provider-credentials.md` — provider credential handling rules.
- `docs/security-remediation-ledger.md` — security remediation status.

## Request boundary

```text
Browser -> Python API (/api/*)
```

The portal talks to the Python API. There is no in-repo AI runtime, model host, or agent
gateway.

## Local development

```bash
cd apps/ashat-hub-web && npm install && npm run build
cd services/python-api && python3 proxy.py
```

Production services and environment configuration are runtime-managed. Credentials, service
environment files, logs, databases, `node_modules/`, and generated runtime data must not be
committed.

## Archive

Historical Rust implementation material is under `archive/retired-rust/`. That material is
archived record only — not active, not built, and not authoritative. See `archive/README.md`
and `docs/retired-components.md`.

## Storage boundary

Runtime code remains on the root filesystem; `/var/oled/pcp` is reserved for Oracle
Performance Co-Pilot data.
