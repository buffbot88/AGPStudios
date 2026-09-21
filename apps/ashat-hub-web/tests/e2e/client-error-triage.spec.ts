import { expect, test } from '@playwright/test';

/**
 * Client error triage, through the real panel.
 *
 * The contract suite covers the endpoints; this covers the thing an operator
 * actually does: a report arrives, it shows up under **New**, they resolve or
 * mute it, and it stops looking like fresh news. The second test is the one that
 * matters most — a mute has to hold for reports that arrive *afterwards*, which
 * is the whole reason muting is message-level rather than per row.
 *
 * Messages are unique per run because the shared mock accumulates reports and
 * other spec files are using it at the same time.
 *
 * One caveat about this panel in a browser: `select`-free and toast-free enough
 * to assert directly, but the rows are matched by their message text.
 */

const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 3112);
const MOCK = `http://127.0.0.1:${MOCK_PORT}`;

const signInAsAdmin = async (page: import('@playwright/test').Page) => {
  await page.goto('/');
  await page.locator('.auth-sign-in').click();
  await expect(page.locator('.auth-modal')).toBeVisible();
  await page.getByPlaceholder('Username or email').fill('boss');
  await page.getByPlaceholder('Password').fill('admin-pass-1');
  await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('.auth-menu')).toBeVisible();
};

/** Report an uncaught browser failure the way the reporter would. */
const report = (request: import('@playwright/test').APIRequestContext, message: string) =>
  request.post(`${MOCK}/api/telemetry/client-error`, {
    data: { source: 'boundary', message, detail: 'client-error-triage.spec', count: 1, path: '/#account' },
  });

const openPanel = async (page: import('@playwright/test').Page) => {
  await page.goto('/admin');
  await page.getByRole('button', { name: 'System', exact: true }).click();
  await expect(page.locator('.adm-client-errors')).toBeVisible();
  return page.locator('.adm-client-errors');
};

test.describe('client error triage', () => {
  test('a resolved report leaves the New list and is listed under Resolved', async ({ page, request }) => {
    const message = `triage-resolve-${Date.now().toString(36)}`;
    await report(request, message);
    await signInAsAdmin(page);
    const panel = await openPanel(page);

    // New is the default view, so an untriaged report is what an operator sees.
    const row = panel.locator('li', { hasText: message });
    await expect(row).toBeVisible();
    await expect(row.locator('.adm-triage-new')).toBeVisible();

    await row.getByRole('button', { name: 'Mark resolved' }).click();

    // Feedback, then the row is gone from New — the point of the feature.
    await expect(page.locator('.toast-stack')).toContainText('Report marked resolved.');
    await expect(panel.locator('li', { hasText: message })).toHaveCount(0);

    await panel.getByRole('tab', { name: 'Resolved' }).click();
    await expect(panel.getByRole('tab', { name: 'Resolved' })).toHaveAttribute('aria-selected', 'true');
    const resolvedRow = panel.locator('li', { hasText: message });
    await expect(resolvedRow).toBeVisible();
    await expect(resolvedRow.locator('.adm-triage-resolved')).toBeVisible();
    await expect(resolvedRow).toContainText('resolved by');

    // Reopening puts it back in the operator's queue.
    await resolvedRow.getByRole('button', { name: 'Reopen' }).click();
    await panel.getByRole('tab', { name: 'New' }).click();
    await expect(panel.locator('li', { hasText: message })).toBeVisible();
  });

  test('a muted message stays out of New for reports that arrive later', async ({ page, request }) => {
    const message = `triage-mute-${Date.now().toString(36)}`;
    await report(request, message);
    await signInAsAdmin(page);
    const panel = await openPanel(page);

    const row = panel.locator('li', { hasText: message });
    await expect(row).toBeVisible();
    await row.getByRole('button', { name: 'Mute this message' }).click();
    await expect(page.locator('.toast-stack')).toContainText('Message muted.');
    await expect(panel.locator('li', { hasText: message })).toHaveCount(0);

    // The report that arrives after the mute is the whole point: it must not
    // come back as new, or muting a noisy known-benign crash would be useless.
    await report(request, message);
    await panel.getByRole('tab', { name: 'New' }).click();
    await expect(panel.locator('li', { hasText: message })).toHaveCount(0);

    await panel.getByRole('tab', { name: 'Muted' }).click();
    const muted = panel.locator('li', { hasText: message }).first();
    await expect(muted).toBeVisible();
    await expect(muted).toContainText('message muted by');

    // Unmuting applies to the message, so everything comes back at once.
    await muted.getByRole('button', { name: 'Unmute message' }).click();
    await expect(page.locator('.toast-stack')).toContainText('Message unmuted.');
    await panel.getByRole('tab', { name: 'New' }).click();
    await expect(panel.locator('li', { hasText: message }).first()).toBeVisible();
  });
});
