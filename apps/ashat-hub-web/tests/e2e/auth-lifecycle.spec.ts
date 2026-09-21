import { expect, test } from '@playwright/test';

/**
 * The auth lifecycle, end to end.
 *
 * Register → verify → sign in → guarded routes as a member and as an admin →
 * sign out, each step driven through the real UI against the mock API. This is
 * the one place the *whole* path is exercised: the other specs assume a session
 * and sign in as the seeded admin, so a break in registration, the verification
 * gate, the route guards, or session revocation would otherwise ship unseen.
 *
 * Two details worth knowing before editing:
 *
 * 1. Verification is email-link based. The portal has no token-entry screen —
 *    a user clicks the link in the email, and the API consumes `?token=` — so
 *    the test plays the part of that click, reading the pending token from the
 *    mock's loopback-only `GET /__codes` control.
 * 2. Verification is toggled globally on the shared mock, so the test that needs
 *    it remembers the previous value and puts it back, leaving the specs running
 *    beside this file exactly as they found them. Nothing here calls
 *    `/__reset`, for the same reason: other files are using the same server.
 */

const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 3112);
const MOCK = `http://127.0.0.1:${MOCK_PORT}`;

/** Unique per run: the mock keeps users in memory for the whole suite. */
const uniqueName = (kind: string) => `e2e_${kind}_${Date.now().toString(36)}${Math.floor(Math.random() * 1000)}`;

const MEMBER_PASSWORD = 'member-pass-9';
const ADMIN = { identifier: 'boss', password: 'admin-pass-1' };

/** Sign in through the header modal, assuming it is closed. */
async function signIn(page: import('@playwright/test').Page, identifier: string, password: string) {
  if (await page.locator('.auth-menu').count()) return; // already signed in
  await page.locator('.auth-sign-in').click();
  await expect(page.locator('.auth-modal')).toBeVisible();
  await page.getByPlaceholder('Username or email').fill(identifier);
  await page.getByPlaceholder('Password').fill(password);
  await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
}

/** Register through the modal (verification off) and sign the new member in. */
async function registerMember(page: import('@playwright/test').Page) {
  const username = uniqueName('member');
  const email = `${username}@example.test`;
  await page.goto('/');
  await page.locator('.auth-sign-in').click();
  await page.getByRole('tab', { name: 'Register' }).click();
  await page.getByPlaceholder('Username').fill(username);
  await page.getByPlaceholder('Email').fill(email);
  await page.getByPlaceholder('Password').fill(MEMBER_PASSWORD);
  await page.locator('.auth-modal form').getByRole('button', { name: 'Create account' }).click();
  await expect(page.locator('.auth-success')).toContainText('Account created. Sign in to continue.');
  // Register closes nothing: the modal flips to sign-in with the name prefilled.
  await page.getByPlaceholder('Password').fill(MEMBER_PASSWORD);
  await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
  await expect(page.locator('.auth-menu')).toBeVisible();
  return { username, email };
}

const toast = (page: import('@playwright/test').Page) => page.locator('.toast-stack');

test.describe('auth lifecycle', () => {
  test('register, then sign in only after the emailed link is used', async ({ page, request }) => {
    const username = uniqueName('verify');
    const email = `${username}@example.test`;

    const previous = await (await request.get(`${MOCK}/__config`)).json();
    await request.post(`${MOCK}/__config`, { data: { verification_required: true } });
    try {
      await page.goto('/');
      await page.locator('.auth-sign-in').click();
      await page.getByRole('tab', { name: 'Register' }).click();
      await page.getByPlaceholder('Username').fill(username);
      await page.getByPlaceholder('Email').fill(email);
      await page.getByPlaceholder('Password').fill(MEMBER_PASSWORD);
      await page.locator('.auth-modal form').getByRole('button', { name: 'Create account' }).click();

      // The account exists but is unverified, and the UI says where to go next.
      await expect(page.locator('.auth-success')).toContainText(`Account created. Check ${email} for a verification link.`);

      // Signing in before verifying is refused, with an explanation rather than
      // a raw error — this is the gate the whole flow depends on.
      await page.getByPlaceholder('Username or email').fill(username);
      await page.getByPlaceholder('Password').fill(MEMBER_PASSWORD);
      await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
      await expect(page.locator('.auth-success')).toContainText('Verify your email address before signing in');
      await expect(page.locator('.auth-menu')).toHaveCount(0);

      // Click the link the email would carry. It is a GET, per the documented
      // contract and the real proxy — the portal has no screen for typing a
      // token in, so nothing else consumes this URL.
      const codes = await (await request.get(`${MOCK}/__codes`)).json();
      expect(codes.codes[username], 'mock kept no verification token for the new account').toBeTruthy();
      const verified = await request.get(`${MOCK}/api/auth/verify-email?token=${codes.codes[username]}`);
      expect(verified.status()).toBe(200);
      expect(await verified.json()).toEqual({ verified: true });

      // Same credentials, now the gate is open.
      await page.getByPlaceholder('Password').fill(MEMBER_PASSWORD);
      await page.locator('.auth-modal form').getByRole('button', { name: 'Sign in' }).click();
      await expect(page.locator('.auth-menu')).toBeVisible();
      await expect(page.locator('.auth-menu-trigger')).toContainText(username);
      await expect(page.locator('.auth-role-badge')).toHaveCount(0);
    } finally {
      // Restore what this test changed — but never let the cleanup mask the
      // real failure when the body above has already thrown.
      await request
        .post(`${MOCK}/__config`, { data: { verification_required: previous.verification_required } })
        .catch(() => undefined);
    }
  });

  test('a signed-out visitor is bounced from #account and #admin with a toast', async ({ page }) => {
    // Deep links, as if pasted into the address bar: the guard must wait for the
    // session to resolve before deciding, or it would bounce a real admin.
    await page.goto('/#account');
    await expect(page.locator('.account-grid')).toHaveCount(0);
    await expect(page.locator('.member-panel')).toHaveCount(0);
    await expect(toast(page)).toContainText('Sign in to view your account.');
    expect(new URL(page.url()).hash, 'guard sends the visitor home').toBe('#top');

    await page.goto('/#admin');
    await expect(page.locator('.adm-surface')).toHaveCount(0);
    await expect(toast(page)).toContainText('Sign in with an administrator account to view the admin panel.');
    expect(new URL(page.url()).hash).toBe('#top');
  });

  test('a member is bounced from #admin but may open #account', async ({ page }) => {
    const { username } = await registerMember(page);

    await page.evaluate(() => { window.location.hash = '#admin'; });
    await expect(page.locator('.adm-surface')).toHaveCount(0);
    await expect(toast(page)).toContainText('Administrator access required.');
    expect(new URL(page.url()).hash).toBe('#top');
    // The bounce is a redirect home, not a sign-out: the session survives it.
    await expect(page.locator('.auth-menu-trigger')).toContainText(username);

    // #account is the member's own surface and must stay reachable.
    await page.evaluate(() => { window.location.hash = '#account'; });
    await expect(page.locator('.account-grid')).toBeVisible();
    await expect(page.locator('.adm-surface')).toHaveCount(0);
  });

  test('an admin reaches #admin and the menu advertises the role', async ({ page }) => {
    await page.goto('/');
    await signIn(page, ADMIN.identifier, ADMIN.password);
    await expect(page.locator('.auth-menu')).toBeVisible();
    await expect(page.locator('.auth-role-badge')).toHaveText('Admin');

    await page.goto('/#admin');
    await expect(page.locator('.adm-surface')).toBeVisible();
    await expect(page.locator('.adm-tabs')).toBeVisible();
    await expect(toast(page)).toHaveCount(0);
  });

  test('sign out revokes the session server-side and the guards re-engage', async ({ page }) => {
    await page.goto('/');
    await signIn(page, ADMIN.identifier, ADMIN.password);
    await expect(page.locator('.auth-menu')).toBeVisible();

    await page.locator('.auth-menu summary').click();
    await page.locator('.auth-logout').click();
    await expect(page.locator('.auth-sign-in')).toBeVisible();

    // Revoked for real, not just forgotten by the UI: the cookie the page still
    // holds no longer resolves to a session.
    const session = await (await page.request.get('/api/auth/session')).json();
    expect(session.authenticated).toBe(false);

    // And the guard treats it as signed out again.
    await page.goto('/#account');
    await expect(toast(page)).toContainText('Sign in to view your account.');
    await expect(page.locator('.account-grid')).toHaveCount(0);
  });
});
