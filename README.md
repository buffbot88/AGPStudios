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
`error_paths`, `error_once`, and `verification_required`; and `GET /__codes`
returns the pending email-verification tokens (the portal has no screen for
typing one in — a user clicks the emailed link, so a test needs the value that
click would carry), plus `POST /__config {"hostile": [...]}` for wrong-shape
payloads. For example:

```bash
curl -X POST http://127.0.0.1:3110/__config \
  -H 'Content-Type: application/json' \
  -d '{"latency_ms":250,"error_status":503,"error_paths":["/api/news"],"error_once":true}'
curl -X POST http://127.0.0.1:3110/__reset
```

When the real
`services/python-api` is running instead, just use `npm run dev` — vite proxies
`/api/*` to `127.0.0.1:3110` either way.

The mock's other CLI switch is `--hostile[=keys]`, which serves deliberately
wrong-shape payloads (see **Drift resilience** below). Both it and
`--no-verification` are remembered as startup controls, so `POST /__reset`
returns the server to how it was launched rather than to bare defaults.

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
test asserts the fallback appears, the app shell survives, the crash is reported
as a toast (with the details disclosure), and retry recovers.
`tests/e2e/global-errors.spec.ts` covers the non-render path: repeated failures
from timers/handlers/unawaited promises batch into one counted toast, unrelated
messages stay separate, and aborted fetches stay silent.
`tests/e2e/auth-lifecycle.spec.ts` covers the whole auth path in one place:
register, the verification gate (sign-in refused until the emailed link is used,
then allowed), the route guards as a signed-out visitor, a member, and an admin,
and sign-out — including that the session is revoked server-side and the guards
re-engage. It flips `verification_required` on the shared mock for its duration
and puts the previous value back, so parallel specs are unaffected.

### Drift resilience against a hostile API

`scripts/mock-api.py --hostile` serves deliberately wrong-shape 200s across the
whole API at once — an array where an object belongs, `"many"` where a number
belongs, a required field missing (`HOSTILE_SHAPES` in the mock is the list, and
it is meant to move with `docs/python-api-contracts.md`). Where the other specs
prove one endpoint is handled, this proves the app survives a bad deploy of the
API: the public homepage still renders its bundled content, validated endpoints
name the field they rejected, unvalidated ones fall through to the safe-helper
notice instead of a blank, and the shell stays usable throughout.

```bash
cd apps/ashat-hub-web
E2E_HOSTILE=1 npx playwright test drift-resilience          # the hostile pass
npm run dev:mock -- --no-verification --hostile             # or poke at it by hand
npm run dev:mock -- --no-verification --hostile=games,news  # only some endpoints
```

The spec expects the full default set; a subset (`--hostile=games,news`, or
`E2E_HOSTILE=games,news`) is for inspecting one surface by hand.

The flag reaches the mock through `playwright.config.ts`, so a run cannot test
healthy data by accident; the spec's first assertion reads the mock's
`/__config` and checks a hostile payload really comes back through the vite
proxy. `session` is deliberately excluded from the default set — breaking it
would sign every test out before it reached the surface under test. Send
`{"hostile": ["games"]}` to `/__config` to change the set at runtime.
Every other spec assumes healthy data and would fail by design here, so the
hostile pass is its own command; CI runs both (see **Continuous integration**).

### Continuous integration

`.github/workflows/ci.yml` runs on **every push and pull request** (not just
`main`, so a feature branch fails fast), as two parallel jobs:

- **Build and check API contracts** — `npm run build`, the endpoint-docs check,
  then a mock API started in the background with both contract suites pointed at
  it (`npm run test:contracts` and `services/python-api/test_contracts.py` with
  mutations enabled). The mock's log is printed if anything fails.
- **Playwright against the mock API** — installs Chromium and runs the healthy
  suite, then the hostile pass in the same job, uploading traces on failure.
  Both jobs install their own dependencies; nothing is shared between them.

`api-ci.yml` remains separate because it needs a MySQL 8 service: it applies
every migration twice (parse + idempotency), runs the migration-runner unit
tests, and checks the migration files are well formed.

### Error reporting

Unexpected failures surface as toasts (`src/components/globalErrors.ts`), so a
silent failure is visible without devtools. Repeats of one message update a
single toast with a `×N` count instead of stacking, and its **Details**
disclosure lists the last five occurrences (time, failing surface, and the
trace line where one is available); opening it pins the toast so it can't
dismiss mid-read. A message quiet for 15s starts a fresh group. Render crashes
are reported through the same path by `SurfaceErrorBoundary`, with the component
stack as their detail line.

Responses are validated as they cross the fetch boundary: `src/components/schemas.ts`
declares the documented shape per endpoint, and `request()` rejects a
wrong-shape success payload with a `ShapeError` naming the endpoint and field
(`GET /api/account/summary → user: expected object, got undefined`) before any
component sees it. The rejection is reported to the same error log as
`shape-error`, and the surface shows it with a retry. Validators are tolerant by
design — unknown keys pass through, optional and nullable fields are declared,
and endpoints without a schema are simply not checked — because a schema
stricter than reality would turn a working page into an error.

Render code never reads an API field raw: `safeText` / `safeNumber` / `safeList` /
`safeTime` in `src/components/api.ts` fall back instead of throwing. A fallback is
no longer silent — it records which helper fell back, what actually arrived, and
the call site (from the stack), which surfaces as an inline **Unexpected data**
notice on the member and admin surfaces, is reported to the same endpoint tagged
`shape-drift`, and toasts once per distinct message per session. Panels that used
to say `Loading…` forever when a payload arrived unusable now say so and offer a
retry (`UnexpectedDataPanel`), and each successful load clears the record so the
notice describes the payload on screen rather than accumulated history. The public
homepage deliberately shows no notice — it falls back to bundled content, and a
data-shape banner is not a visitor's problem — but the drift is still logged.

The same failures are also logged server-side, so they are still there after the
tab is closed: the browser POSTs the group to `/api/telemetry/client-error`
(migration `0005`), sending immediately on the first occurrence and then at most
once per 10s, the trailing report carrying the burst's total count and details.
The endpoint is public — the pages that crash are the ones anyone can load — so
it is rate limited per IP (in memory only; no IP is stored), truncates every
field, caps bodies at 8 KB, and keeps only the page path. A failed report is
never re-reported. The admin CP's System tab renders the result in a **Client
errors** panel, which reads `GET /api/admin/client-errors`; rows older than 30
days are pruned opportunistically.

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

**A database migrated by hand before this runner existed** can be baselined once, so
its files are recorded as applied instead of executed:

```bash
python3 migrate.py --baseline   # record current files as applied WITHOUT running them
```

Every migration file is written to be idempotent, so baselining is a convenience —
not a requirement — but a recorded file is never executed twice.

Editing an applied migration changes its checksum. The runner reports that as
**drift** and leaves it alone; converge with `python3 migrate.py --reapply-drifted`.
A migration that fails partway is left unrecorded (MySQL commits DDL implicitly),
so fix the file and re-run it.

Unit tests for the runner need no database — `python3 services/python-api/test_migrate.py`.

Idempotency itself is checked in CI by `services/python-api/test_migrations_mysql.py`,
which applies every file twice against a throwaway MySQL 8 service and asserts the
second run succeeds without duplicating rows or columns. Run it locally the same way
(needs `pymysql` and `ASHAT_DATABASE_URL` pointing at a disposable database):

```bash
python3 -m pip install pymysql
ASHAT_DATABASE_URL=mysql://root:root@127.0.0.1:3306/ashat_migrations_test \
  python3 services/python-api/test_migrations_mysql.py
```

It exits 0 with a `SKIP:` notice when `ASHAT_DATABASE_URL` is unset. With the variable
set it is strict: a missing driver or a database whose name doesn't contain `test` is an
error, because the check applies every migration and its seed rows to whatever the URL
points at (`ASHAT_ALLOW_ANY_DATABASE=1` overrides the name guard).

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
