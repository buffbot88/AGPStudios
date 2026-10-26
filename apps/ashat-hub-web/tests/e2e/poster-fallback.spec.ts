import { expect, test, type APIRequestContext } from '@playwright/test';

/**
 * Poster fallback regression tests.
 *
 * The admin CP can point a game's poster at any URL (PUT /api/admin/games/:slug),
 * so a typo or a deleted asset is a realistic production state. When that
 * happens the homepage must show the branded gradient frame — the same artwork
 * as the "no art yet" state — and never the browser's broken-image icon.
 *
 * The studio runs two live games (Paws & Parcels, Everenfell), so the grid has
 * two cards. These tests share one mock API instance, so they run serially and
 * restore the seed poster afterwards: otherwise a broken URL set here would
 * leak into the other tests (and into the rest of the E2E run).
 */

const BROKEN_POSTER = '/definitely-not-a-real-poster.svg';
const GOOD_POSTER = '/everenfell-poster.svg';
const GAME = 'everenfell';
const MOCK_PORT = Number(process.env.E2E_MOCK_PORT ?? 3112);
const API = `http://127.0.0.1:${MOCK_PORT}`;

test.describe.configure({ mode: 'serial' });

/** Point a game's poster at an arbitrary URL via the real admin endpoint. */
async function setPoster(request: APIRequestContext, slug: string, poster: string) {
  const login = await request.post(`${API}/api/auth/login`, {
    data: { identifier: 'boss', password: 'admin-pass-1' },
  });
  expect(login.ok()).toBeTruthy();

  const state = await request.storageState();
  const csrf = state.cookies.find((cookie) => cookie.name === 'ashat_csrf')?.value;
  expect(csrf, 'admin login should set the CSRF cookie').toBeTruthy();

  const update = await request.put(`${API}/api/admin/games/${slug}`, {
    headers: { 'X-CSRF-Token': csrf ?? '' },
    data: { poster },
  });
  expect(update.ok()).toBeTruthy();
  expect((await update.json()).poster).toBe(poster);
}

test.describe('poster fallback', () => {
  test.afterEach(async ({ request }) => {
    // Restore seed art so this spec leaves the shared mock as it found it.
    await setPoster(request, GAME, GOOD_POSTER);
  });

  test('a broken poster URL degrades to the branded frame in the grid', async ({ page, request }) => {
    await setPoster(request, GAME, BROKEN_POSTER);
    // The homepage paints bundled fallback content (which has working art)
    // before the API answers, so wait for the API-backed render before asserting.
    const games = page.waitForResponse((response) => response.url().includes('/api/games'));
    await page.goto('/#games');
    await games;

    // The card for the broken game renders the fallback frame...
    const gridFallback = page.locator('.game-thumb .thumb-fallback');
    await expect(gridFallback).toBeVisible({ timeout: 10_000 });
    await expect(gridFallback).toHaveClass(/fallback-live/);

    // ...with the brand gradient applied, not an empty transparent box.
    const background = await gridFallback.evaluate((element) => getComputedStyle(element).backgroundImage);
    expect(background).toContain('gradient');

    // The failed image is gone from the DOM entirely — no broken-image icon.
    await expect(page.locator(`img[src="${BROKEN_POSTER}"]`)).toHaveCount(0);

    // The other live game still renders real art: the fallback is scoped to the
    // broken card, not the whole grid.
    await expect(page.locator('.game-thumb img')).toHaveCount(1);
  });

  test('the carousel slide for a broken poster shows the branded frame', async ({ page, request }) => {
    await setPoster(request, GAME, BROKEN_POSTER);
    const games = page.waitForResponse((response) => response.url().includes('/api/games'));
    await page.goto('/');
    await games;

    // Advance until the broken slide (the second live game) is active.
    const next = page.getByRole('button', { name: 'Next game' });
    for (let attempt = 0; attempt < 3; attempt += 1) {
      if (await page.locator('.featured-poster .poster-fallback').count()) break;
      await next.click();
      await page.waitForTimeout(250);
    }

    const slideFallback = page.locator('.featured-poster .poster-fallback');
    await expect(slideFallback).toBeVisible();
    const background = await slideFallback.evaluate((element) => getComputedStyle(element).backgroundImage);
    expect(background).toContain('gradient');

    // The featured slot shows the frame rather than a broken image.
    await expect(page.locator('.featured-poster img')).toHaveCount(0);
  });

  test('valid posters still render as images (no spurious fallback)', async ({ page, request }) => {
    await setPoster(request, GAME, GOOD_POSTER);
    const games = page.waitForResponse((response) => response.url().includes('/api/games'));
    await page.goto('/#games');
    await games;

    await expect(page.locator('.game-thumb .thumb-fallback')).toHaveCount(0);
    await expect(page.locator('.game-thumb img')).toHaveCount(2);

    // Art actually decoded, not just present in the DOM.
    const naturalWidth = await page
      .locator('.game-thumb img')
      .first()
      .evaluate((image) => (image as HTMLImageElement).naturalWidth);
    expect(naturalWidth).toBeGreaterThan(0);
  });
});
