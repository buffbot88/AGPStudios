# AGP Studios

AGP Studios is the web portal and Python API project for AGP Studios.

Live games: **Paws & Parcels** and **Everenfell**. Anything referencing World of
Ra, `more-titles`, Omega, or Galileo is a stub or historical reference — not a
live server.

## What this repo is

- `apps/ashat-hub-web/` — Vite + React portal. Public site, member area, and admin UI.
- `services/python-api/` — Python API service and SQLite persistence for the public
  `/api/*` surface: auth, OIDC, account, support, and admin endpoints. SQL migrations
  live in `services/python-api/migrations/` and are applied by the tracked runner
  (`python3 migrate.py`), which records each file in `schema_migrations`.
- `docs/system-architecture.md` — what the active system is and its request boundary.
- `docs/python-api-contracts.md` — the public API contract source of truth.
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
cd services/python-api && python3 migrate.py && python3 proxy.py
```

### Frontend with the mock API

`scripts/mock-api.py` is a documented dev utility: an in-memory stand-in for the
Python API implementing the endpoints the portal consumes (session, register with
login, logout, account, support, games, news, and the admin surface). Email
verification is disabled by default to match production; it can be enabled for
focused tests. The mock exists so the frontend can be developed and E2E-tested
without a database, an OIDC key, or the production service. It is a test double only —
not a reference implementation, and never pointed at production.

```bash
cd apps/ashat-hub-web
npm run dev:mock        # mock API (127.0.0.1:3110) + vite (localhost:3101) together
npm run mock-api        # mock API only, if you're running vite yourself
npm run dev:mock -- --verification-required   # opt in to test verification
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
wrong-shape payloads (see **Drift resilience** below). Both switches are remembered as startup controls, so `POST /__reset`
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

### Live games and stubs

Live: Paws & Parcels (`paws-and-parcels`) and Everenfell (`everenfell`, Season 1
via S1 entitlement). Stubbed / not implemented: password-reset request, GitHub
OAuth (`auth/github`, `account/github`), `admin/github/push`,
`admin/system/update`, and the legacy World of Ra ticket/grant flow. The mock
API and seeds carry only the two live games.

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
npm run dev:mock -- --hostile             # or poke at it by hand
npm run dev:mock -- --hostile=games,news  # only some endpoints
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

`api-ci.yml` runs SQLite schema/persistence checks, migration-runner tests, and
checks that the migration files are well formed.

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

That panel is triaged, not just read (migration `0006`): **Mark resolved**
records a judgement on one report and removes it from the default **New** view,
while **Mute this message** covers every report of the same message — the rows
already logged *and* every burst that arrives later, until it is unmuted. That
distinction is the reason muting is message-level: one row is one reported burst,
so muting a row would do nothing about the next one. Mutes key on
`SHA-256(message)` and are applied when the list is read, so they also survive
the 30-day retention pass (the mutes table is never pruned). The panel's
**New / Resolved / Muted / All** chips are that filter, the filter defaults to
New, and both writes are audited (`client_error.status.updated`,
`client_error.message.muted`). They share the admin idempotency guard, keyed by
report id *and* the requested state — so resolving a report and immediately
reopening it is a change of mind, not a duplicate submission.

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

Runtime settings come from the repository-root `config.json`:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Set private values there; `database_path` defaults to `storage/ashathub.sqlite3`.
The API, migration runner, and importer read this same ignored file.

**A database migrated by hand before this runner existed** can be baselined once, so
its files are recorded as applied instead of executed:

```bash
python3 migrate.py --baseline   # record current files as applied WITHOUT running them
```

Migrations are applied once per recorded filename. Baseline only when the SQLite
schema has already been applied manually and matches the current migration.

Editing an applied migration changes its checksum. The runner reports that as
**drift** and leaves it alone; converge with `python3 migrate.py --reapply-drifted`.
Each migration runs in a SQLite transaction, so a failed migration is rolled back
and left unrecorded; fix the file and re-run it.

Unit tests for the runner need no database — `python3 services/python-api/test_migrate.py`.

SQLite schema and API flows are checked by
`test_migrations_sqlite.py`, `test_api_sqlite.py`, and `test_import_mysql.py`.

#### Importing the existing MySQL data

The API no longer connects to MySQL. Put the MySQL connection URL in the ignored
`mysql_source_url` field in `config.json`, back up MySQL, and put the portal in maintenance
(stop API writes). Run the one-time importer with PyMySQL temporarily installed:

```bash
python3 -m pip install pymysql
python3 services/python-api/import_mysql.py --sqlite-path /secure/path/ashathub.sqlite3
```

The importer reads a consistent MySQL snapshot, writes a new file, refuses to overwrite
an existing destination, and stops if it finds unknown tables or columns. It reports known legacy tables outside this API schema that it leaves in MySQL. Review row counts and the foreign-key check,
then set `database_path` in `config.json` before restarting the API. Keep MySQL available for other consumers and retain the backup until SQLite is verified.

Credentials, `config.json`, logs, databases, `node_modules/`, and generated runtime data
must not be committed.

## Retired runtime

The Rust runtime and its local config are not part of the current portal. The old source
remains recoverable through Git history; the current runtime is Python + React/Vite/TypeScript.

## Storage boundary

Runtime code remains on the root filesystem; `/var/oled/pcp` is reserved for Oracle
Performance Co-Pilot data.
