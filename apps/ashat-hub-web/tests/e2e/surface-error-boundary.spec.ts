import { expect, test } from '@playwright/test';

/**
 * Error-boundary regression tests.
 *
 * They arm the dev-only crash hook (src/components/devCrash.ts) so a member or
 * admin surface throws during render — the same failure mode a bad API field
 * caused — and assert that:
 *
 *   1. the surface degrades to its fallback panel,
 *   2. the app shell (header, session) stays mounted and usable, and
 *   3. disarming and clicking "Try again" recovers the surface.
 *
 * Without the boundary, step 1 would blank the whole page instead.
 *
 * The crash repeats while armed (React retries concurrent renders, so a
 * one-shot throw would silently recover and make this pass vacuously), which
 * is why recovery is tested by disarming first.
 */

const arm = (surface: 'member' | 'admin') => `window.__AGP_CRASH_SURFACE = '${surface}';`;
const disarm = () => `delete window.__AGP_CRASH_SURFACE;`;

test.describe('surface error boundary', () => {
  test('member surface crash degrades to the support fallback and recovers', async ({ page }) => {
    // React catches render throws, so the crash surfaces as a console error
    // (the boundary's componentDidCatch), not as an uncaught page error.
    const consoleErrors: string[] = [];
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text());
    });

    // Arm from the homepage, then navigate to #support (which renders
    // MemberSurfaces even when signed out) so the surface mounts armed.
    await page.goto('/');
    await page.evaluate(arm('member'));
    await page.evaluate(() => { window.location.hash = '#support'; });

    const fallback = page.getByRole('alert');
    await expect(fallback).toContainText("Support couldn't load");
    await expect(fallback.getByRole('button', { name: 'Try again' })).toBeVisible();

    // Containment: the app shell is still mounted and the page is not blank.
    await expect(page.locator('header, .site-header')).toBeVisible();

    // Guards against a silently-passing test: the crash really happened.
    expect(consoleErrors.join('\n')).toContain('Test crash: simulated member');

    // Recovery: disarm (as if the bad data were fixed), then retry.
    await page.evaluate(disarm());
    await fallback.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Member navigation' })).toBeVisible();
    await expect(page.getByText('Sign in to create and view support tickets.')).toBeVisible();
  });

  test('admin surface crash is isolated from the header and recovers', async ({ page }) => {
    // Sign in as the seeded admin first (mock API).
    await page.goto('/');
    await page.locator('.auth-sign-in').click();
    await expect(page.locator('.auth-modal')).toBeVisible();
    await page.getByPlaceholder('Username or email').fill('boss');
    await page.getByPlaceholder('Password').fill('admin-pass-1');
    await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
    await expect(page.locator('.auth-menu')).toBeVisible();

    await page.evaluate(arm('admin'));
    await page.evaluate(() => { window.location.hash = '#admin'; });

    const fallback = page.getByRole('alert');
    await expect(fallback).toContainText("The admin panel couldn't load");

    // Session/header survived the surface crash.
    await expect(page.locator('.auth-menu')).toBeVisible();

    await page.evaluate(disarm());
    await fallback.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Admin Control' })).toBeVisible();
  });

  test('an unarmed surface never shows the fallback', async ({ page }) => {
    await page.goto('/#support');
    await expect(page.locator('.member-section')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(page.getByText('Sign in to create and view support tickets.')).toBeVisible();
  });
});
