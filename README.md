# AGP Studios

AGP Studios is the web portal and Python API project for AGP Studios.

## What this repo is

- `apps/ashat-hub-web/` — Vite + React portal. Public site, member area, and admin UI.
- `services/python-api/` — Python API service. Serves the public `/api/*` surface: auth,
  OIDC, account, support, and admin endpoints. SQL migrations for its MySQL schema
  live in `services/python-api/migrations/` and are applied by the tracked runner
  (`python3 migrate.py`), which records each file in a `schema_migrations` table so
  it runs exactly once, in filename order.
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
on Windows (where `cmd & cmd &` backgrounding doesn't work). The mock also exposes
loopback-only test controls: `POST /__reset` restores seed data and clears sessions;
`POST /__config` accepts `latency_ms`, `error_status`, `error_message`,
`error_paths`, `error_once`, and `verification_required`. For example:

```bash
curl -X POST http://127.0.0.1:3110/__config \
  -H 'Content-Type: application/json' \
  -d '{"latency_ms":250,"error_status":503,"error_paths":["/api/news"],"error_once":true}'
curl -X POST http://127.0.0.1:3110/__reset
```

When the real
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

### End-to-end tests

```bash
cd apps/ashat-hub-web && npm run test:e2e
```

Playwright drives the real UI against the mock API. `playwright.config.ts`
starts both servers itself (mock on 3110, Vite on 3111) so runs never collide
with a dev server on 3101. `tests/e2e/surface-error-boundary.spec.ts` covers the
error boundary: a member or admin surface is made to throw during render and the
test asserts the fallback appears, the app shell survives, and retry recovers.

Browser source: by default the suite uses the installed Chrome channel, so no
browser download is needed. In CI, run `npx playwright install chromium` and set
`E2E_CHANNEL=chromium`.

The crash injection lives in `src/components/devCrash.ts` behind
`import.meta.env.DEV`, so it is dead-code-eliminated from production builds
(verified: the built bundle contains neither the crash message nor its global).

### Database migrations

SQL migrations live in `services/python-api/migrations/` as `NNNN_description.sql` and
are applied by `services/python-api/migrate.py`, which:

1. creates a `schema_migrations` table (`filename`, `checksum`, `statements`,
   `applied_at`, `applied_by`) if it doesn't exist,
2. reads the files in numeric filename order, and
3. runs the ones that aren't recorded yet, recording each only after all of its
   statements succeed.

```bash
cd services/python-api
python3 migrate.py --status     # applied / pending / drifted, changes nothing
python3 migrate.py              # apply everything pending
python3 migrate.py --check      # exit 1 if anything is pending (deploy gate)
```

The connection string comes from `ASHAT_DATABASE_URL`
(`mysql://user:pass@host:3306/database`) — the same variable `proxy.py` uses.

**A database migrated by hand before this runner existed** should be baselined once:

```bash
python3 migrate.py --baseline   # record current files as applied WITHOUT running them
```

Without the baseline, the runner would try to re-run `0001`, which is an
`ALTER TABLE ... ADD COLUMN` that is *not* idempotent (it fails with "Duplicate
column name"). Tracking exists precisely so migrations like that run once. Other
files are written to be convergent anyway, so a re-run is harmless — but a recorded
file is never executed twice.

Editing an applied migration changes its checksum. The runner reports that as
**drift** and leaves it alone; converge with `python3 migrate.py --reapply-drifted`.
A migration that fails partway is left unrecorded (MySQL commits DDL implicitly),
so fix the file and re-run it.

Unit tests for the runner need no database — `python3 services/python-api/test_migrate.py`
(also run in CI).

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
