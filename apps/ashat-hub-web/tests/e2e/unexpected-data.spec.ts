import { expect, test } from '@playwright/test';

/**
 * Shape-drift coverage.
 *
 * A field the UI expects but the API sends in the wrong shape used to render as
 * a blank, or leave a panel saying "Loading…" forever. These tests fulfil real
 * endpoints with malformed 200 responses (so no mock changes are needed) and
 * assert that the surface says what happened, that retry recovers, and that the
 * coercion is reported to the server-side error log.
 */

const driftReports = (page: import('@playwright/test').Page) => {
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

test.describe('unexpected data', () => {
  test('healthy homepage data reports no drift', async ({ page }) => {
    const reports = driftReports(page);
    await page.goto('/');
    await expect(page.locator('.games-grid .game-card').first()).toBeVisible();
    await expect(page.locator('.news-list-panel')).toContainText('A New Chapter Ahead');
    await page.waitForTimeout(500);

    // A correct payload must be silent: no drift and no rejection, because a
    // reporter that cries wolf during normal operation gets ignored.
    expect(reports.filter((report) => report.source?.startsWith('shape'))).toEqual([]);
  });

  test('every surface renders the healthy mock without a shape rejection', async ({ page }) => {
    const reports = driftReports(page);
    await signInAsAdmin(page);

    // The accounts page, the support page, and every admin tab, all against the
    // real mock: this is what catches a schema stricter than the API.
    await page.evaluate(() => { window.location.hash = '#account'; });
    await expect(page.locator('.account-grid form')).toBeVisible();
    await page.evaluate(() => { window.location.hash = '#support'; });
    await expect(page.locator('.support-layout')).toBeVisible();

    // A real navigation: the hash alone doesn't re-route the mounted surface.
    await page.goto('/admin');
    await expect(page.locator('.adm-tabs')).toBeVisible();
    for (const tab of ['Overview', 'Users', 'Games', 'Omega', 'System']) {
      await page.getByRole('button', { name: tab, exact: true }).click();
      await expect(page.getByRole('button', { name: tab, exact: true })).toHaveClass(/selected/);
    }
    await expect(page.locator('.adm-migrations')).toBeVisible();
    await page.waitForTimeout(500);

    expect(reports.filter((report) => report.source === 'shape-error')).toEqual([]);
    await expect(page.locator('.workspace-error')).toHaveCount(0);
  });

  test('a payload missing a required field is rejected with the field named', async ({ page }) => {
    const reports = driftReports(page);
    await signInAsAdmin(page);

    await page.route('**/api/account/summary', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ stats: { support_tickets: 2 } }),
      }),
    );

    await page.evaluate(() => { window.location.hash = '#account'; });

    // Rejected at the boundary: the panel explains and names the field, and the
    // spinner must not run forever.
    const panel = page.locator('.data-notice');
    await expect(panel).toContainText('Unexpected data');
    await expect(panel).toContainText('GET /api/account/summary → user: expected object, got undefined');
    await expect(page.getByText('Loading account...')).toHaveCount(0);

    // The rejection is reported too, so it is visible after the fact.
    await expect.poll(() => reports.length).toBeGreaterThan(0);
    expect(reports.some((report) => report.source === 'shape-error')).toBe(true);

    // Recovery: a healthy payload renders the account it withheld before.
    await page.unroute('**/api/account/summary');
    await page.getByRole('button', { name: 'Try again' }).click();
    await expect(page.locator('.data-notice')).toHaveCount(0);
    await expect(page.locator('.account-grid form')).toBeVisible();
  });

  test('a wrong-typed field never reaches the component', async ({ page }) => {
    const reports = driftReports(page);
    await signInAsAdmin(page);

    // Valid account, but a count the UI expects to be a number arrives as text.
    await page.route('**/api/account/summary', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          user: { id: '1', username: 'boss', email: 'boss@agpstudios.org', display_name: 'Boss', role: 'Admin' },
          github_linked: false,
          stats: { support_tickets: 'many' },
        }),
      }),
    );

    await page.evaluate(() => { window.location.hash = '#account'; });

    // The bad value is stopped at the boundary, so the form never renders it…
    const panel = page.locator('.data-notice');
    await expect(panel).toContainText('stats.support_tickets: expected number, got string "many"');
    await expect(page.locator('.account-grid form')).toHaveCount(0);
    // …and the safe-helper fallback never had to run, so no drift was recorded.
    expect(reports.some((report) => report.source === 'shape-drift')).toBe(false);
    await expect.poll(() => reports.length).toBeGreaterThan(0);
    expect(reports.some((report) => report.source === 'shape-error')).toBe(true);
  });

  test('an admin panel with missing pieces says so instead of Loading', async ({ page }) => {
    const reports = driftReports(page);
    await signInAsAdmin(page);

    // A status response with no `migrations` object: nothing to list, and it
    // is not a pending request.
    await page.route('**/api/admin/database/status', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ database: 'mysql', version: '8.0', paws: { status: 'ok' } }),
      }),
    );

    // Path change first (no hashchange from replaceState), then a real hash
    // change so the app re-routes into the admin surface's System tab.
    await page.evaluate(() => { window.history.replaceState({}, '', '/admin'); });
    await page.evaluate(() => { window.location.hash = '#system'; });

    const panel = page.locator('.adm-migrations');
    await expect(panel).toContainText('Not reported');
    await expect(panel).not.toContainText('Loading…');
    await expect(panel.locator('.data-notice')).toContainText('Unexpected data');
    await expect.poll(() => reports.length).toBeGreaterThan(0);
  });
});
