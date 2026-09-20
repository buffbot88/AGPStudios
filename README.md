# AGP Studios

AGP Studios is the web portal and Python API project for AGP Studios.

## What this repo is

- `apps/ashat-hub-web/` — Vite + React portal. Public site, member area, and admin UI.
- `services/python-api/` — Python API service. Serves the public `/api/*` surface: auth,
  OIDC, account, support, and admin endpoints. SQL migrations for its MySQL schema
  live in `services/python-api/migrations/` and are applied manually in filename
  order (`0001`, `0002`, ...); each file is idempotent, so re-running is safe.
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

### Frontend with the mock API

`scripts/mock-api.py` is a documented dev utility: an in-memory stand-in for the
Python API implementing the endpoints the portal consumes (session, register with
optional email verification, login, logout, account, support, games, news, and the
admin surface). It exists so the frontend can be developed and E2E-tested without
MySQL, an OIDC key, or the production service. Verification codes are printed to
stdout. It is a test double only — not a reference implementation, and never
pointed at production.

```bash
cd apps/ashat-hub-web
npm run dev:mock        # mock API (127.0.0.1:3110) + vite (localhost:3101) together
npm run mock-api        # mock API only, if you're running vite yourself
npm run dev:mock -- --no-verification   # register activates immediately (E2E style)
```

`npm run dev:mock` starts both processes and tears both down on Ctrl+C, including
on Windows (where `cmd & cmd &` backgrounding doesn't work). When the real
`services/python-api` is running instead, just use `npm run dev` — vite proxies
`/api/*` to `127.0.0.1:3110` either way.

### Frontend contract tests

`tests/test_frontend_contracts.py` asserts every API response shape the portal
reads (mirroring the frontend's TypeScript types against the documented
contracts). It exists so shape drift — like the `conversation_messages`
account-stats bug that crashed the whole app — fails in CI instead of in
production. With the mock running:

```bash
cd apps/ashat-hub-web && npm run test:contracts
# or point at a live deployment:
ASHAT_TEST_BASE=https://www.agpstudios.org python3 tests/test_frontend_contracts.py
```

The suite registers its own throwaway member, exercises member + admin flows
(reads, tickets, role/status/ban, PUT/DELETE account), and cleans up after
itself. It must pass against both the mock and the live API — if a shape only
passes on one, the mock has drifted and should be fixed to mirror reality.

### Database migrations

SQL migrations live in `services/python-api/migrations/` and are applied manually in
filename order against the production MySQL database. Each file is written to be
idempotent and safe to run on databases in any prior state — re-running a migration
converges to the same schema and seed data instead of failing or duplicating rows.

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
