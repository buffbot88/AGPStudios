import { defineConfig, devices } from '@playwright/test';

/**
 * E2E config with zero manual setup: one `webServer` command boots the entire
 * stack via `npm run dev:mock` (scripts/dev.py starts the mock API *and* Vite,
 * and shuts both down together), then Playwright waits for the web URL.
 *
 * Ports are injected per run (3111/3112 by default) rather than reusing the
 * developer's 3101/3110, so an E2E run never fights a running dev server —
 * including its own previous run's leftovers. Override with E2E_PORT /
 * E2E_MOCK_PORT. `strictPort` is passed through, so a silent port shift fails
 * the run instead of testing the wrong server.
 *
 * Browser source: defaults to the installed Chrome channel so no browser
 * download is needed. In CI run `npx playwright install chromium` and set
 * E2E_CHANNEL=chromium.
 *
 * Drift resilience: `E2E_HOSTILE=1` boots the same stack with the mock in
 * hostile mode (`--hostile`, or `E2E_HOSTILE=games,news` for a subset), which
 * serves deliberately wrong-shape payloads. Only `drift-resilience.spec.ts`
 * cares — the rest of the suite is written against healthy data and would fail
 * by design — so run that spec in a second pass with this set.
 */
const PORT = Number(process.env.E2E_PORT ?? 3111);
const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 3112);
const HOSTILE = process.env.E2E_HOSTILE
  ? process.env.E2E_HOSTILE === '1'
    ? ' --hostile'
    : ` --hostile=${process.env.E2E_HOSTILE}`
  : '';

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  // Two browsers by default: the toast assertions race the 8s auto-dismiss when
  // four Chromium instances share a small machine. Raise with E2E_WORKERS.
  workers: Number(process.env.E2E_WORKERS ?? 2),
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        channel: process.env.E2E_CHANNEL ?? 'chrome',
      },
    },
  ],
  webServer: {
    command: `npm run dev:mock -- --port ${PORT} --mock-port ${MOCK_PORT}${HOSTILE}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
