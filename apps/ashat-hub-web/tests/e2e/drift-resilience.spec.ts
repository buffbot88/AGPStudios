import { expect, test } from '@playwright/test';

/**
 * Drift resilience, against a deliberately hostile API.
 *
 * The other specs prove the app handles *one* malformed payload, stubbed ad hoc
 * with `page.route`. This one runs the whole stack against `mock-api.py
 * --hostile`, which serves wrong-shape 200s across the API at once (see
 * HOSTILE_SHAPES in scripts/mock-api.py). That is the difference between "this
 * endpoint is handled" and "the app survives a bad deploy of the API" — logged
 * out, signed in, and in the admin CP, with the shell still usable.
 *
 * Run it as a second pass with the mock in hostile mode:
 *   E2E_HOSTILE=1 npx playwright test drift-resilience
 * Without that env var the whole file is skipped: every other spec asserts
 * healthy data and would fail by design against these payloads.
 */

const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 3112);
const HOSTILE = process.env.E2E_HOSTILE && process.env.E2E_HOSTILE !== '0';

test.skip(!HOSTILE, 'set E2E_HOSTILE=1 (or E2E_HOSTILE=games,news) to run against the hostile mock');

/** Every telemetry POST the page made, with its parsed body. */
const reported = (page: import('@playwright/test').Page) => {
  const bodies: { source?: string; message?: string }[] = [];
  page.on('request', (request) => {
    if (!request.url().includes('/api/telemetry/client-error')) return;
    try {
      bodies.push(JSON.parse(request.postData() ?? '{}'));
    } catch {
      bodies.push({});
    }
  });
  return bodies;
};

const signInAsAdmin = async (page: import('@playwright/test').Page) => {
  await page.goto('/');
  await page.locator('.auth-sign-in').click();
  await expect(page.locator('.auth-modal')).toBeVisible();
  await page.getByPlaceholder('Username or email').fill('boss');
  await page.getByPlaceholder('Password').fill('admin-pass-1');
  await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('.auth-menu')).toBeVisible();
};

test.describe('hostile API shapes', () => {
  test('the run really is hostile', async ({ request }) => {
    // Read the mock directly: the dev proxy forwards /api and /health only, so
    // the control endpoint is not reachable through the web server.
    const config = await (await request.get(`http://127.0.0.1:${MOCK_PORT}/__config`)).json();
    const hostile: unknown = config.hostile;
    expect(Array.isArray(hostile) && hostile.length > 0).toBe(true);

    // And prove it end to end through the proxy the page actually uses, so a
    // dropped --hostile flag fails the job instead of passing it vacuously.
    const games = await (await request.get('/api/games')).json();
    expect(Array.isArray(games.games)).toBe(false);
  });

  test('the public homepage degrades to its bundled content', async ({ page }) => {
    await page.goto('/');

    // Games and news both arrive unusable; the homepage falls back to the
    // static content rather than showing an empty strip or a white screen.
    await expect(page.locator('.live-strip .live-card').first()).toBeVisible();
    await expect(page.locator('.news-list-panel')).toBeVisible();
    await expect(page.locator('.featured-card')).toBeVisible();
    await expect(page.locator('.surface-error')).toHaveCount(0);
  });

  test('validated endpoints are rejected with the field named', async ({ page }) => {
    const reports = reported(page);
    await signInAsAdmin(page);

    // Two notices can share the class (the coercion notice and the rejection
    // panel), so pick the one naming this endpoint.
    await page.evaluate(() => { window.location.hash = '#account'; });
    const account = page.locator('.data-notice').filter({ hasText: 'GET /api/account/summary' });
    await expect(account).toContainText('Unexpected data');
    await expect(page.getByText('Loading account...')).toHaveCount(0);

    await page.evaluate(() => { window.location.hash = '#support'; });
    await expect(page.locator('.support-layout')).toBeVisible();
    await expect(page.locator('.workspace-error')).toContainText('GET /api/support');
    // The unusable list is not faked: nothing invented, and no endless spinner.
    await expect(page.locator('.ticket-row')).toHaveCount(0);
    await expect(page.locator('.support-layout')).toContainText('No tickets yet.');

    // Rejections are reported, so the failure is visible after the fact.
    await expect.poll(() => reports.filter((r) => r.source === 'shape-error').length).toBeGreaterThan(0);
    expect(reports.some((r) => r.message?.includes('GET /api/account/summary'))).toBe(true);
  });

  test('the admin CP reports what it could not read and stays navigable', async ({ page }) => {
    const reports = reported(page);
    await signInAsAdmin(page);
    await page.goto('/admin');

    // Summary, user rows, and the client-error log are all hostile: the surface
    // names a rejection in place, keeps its tabs usable, and every rejected
    // endpoint is identifiable from the error line or the reports.
    await expect(page.locator('.adm-tabs')).toBeVisible();
    await expect(page.locator('.workspace-error')).toContainText('/api/admin/');
    await page.getByRole('button', { name: 'Users', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Users', exact: true })).toHaveClass(/selected/);
    await page.getByRole('button', { name: 'Games', exact: true }).click();
    await expect(page.locator('.adm-games-list')).toBeVisible();

    await expect.poll(() => reports.filter((r) => r.source === 'shape-error').length).toBeGreaterThan(1);
    const named = reports.map((r) => r.message ?? '').join('\n');
    expect(named).toContain('GET /api/admin/summary');
    expect(named).toContain('GET /api/admin/users');
  });

  test('unvalidated endpoints fall through to the safe helpers, not to blanks', async ({ page }) => {
    const reports = reported(page);
    await signInAsAdmin(page);
    await page.goto('/admin');

    // Telemetry and the Omega server list have no schema yet, so these payloads
    // reach the components and coerce. They must show the inline notice and
    // still render their panels, and the coercion must be reported as drift.
    await page.getByRole('button', { name: 'Omega', exact: true }).click();
    await expect(page.locator('.adm-telemetry-page')).toBeVisible();
    const notice = page.locator('.data-notice');
    await expect(notice.first()).toContainText('Unexpected data');

    await expect
      .poll(() => reports.filter((r) => r.source === 'shape-drift').length)
      .toBeGreaterThan(0);
  });
});
