import { expect, test } from '@playwright/test';

/**
 * Global (non-render) error reporting.
 *
 * Failures raised from timers, event handlers, and unawaited promises never
 * reach the React error boundary — they go through the window.onerror /
 * unhandledrejection handlers instead. These tests assert that repeated
 * failures batch into one counted toast with a details disclosure, that
 * unrelated messages stay separate, and that known-benign noise stays silent.
 */

/** Raise an unhandled rejection in the page, the way a stray promise does. */
const reject = (message: string) => {
  void Promise.reject(new Error(message));
};

/** Every telemetry POST the page made, with its parsed body. */
const reported = (page: import('@playwright/test').Page) => {
  const bodies: Record<string, unknown>[] = [];
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

test.describe('global error toasts', () => {
  test('repeated failures batch into one toast with a count and details', async ({ page }) => {
    await page.goto('/');

    const stack = page.locator('.toast-stack');
    await expect(stack).toHaveCount(0);

    // One round trip: separate evaluates can straddle the reporter's grouping
    // window under parallel load, which is a test artifact, not app behavior.
    await page.evaluate((count: number) => {
      for (let attempt = 0; attempt < count; attempt += 1) {
        void Promise.reject(new Error('Batched mock failure'));
      }
    }, 3);

    // One toast for three occurrences, not three toasts. Hovering pauses its
    // auto-dismiss (real app behavior), so a loaded machine can't race the
    // assertion and find it already gone.
    await stack.hover();
    await expect(stack.locator('.toast')).toHaveCount(1);
    await expect(stack).toContainText('Something went wrong: Batched mock failure');
    await expect(stack.locator('.toast-count')).toHaveText('×3');

    // The disclosure shows every occurrence it kept, each time-stamped.
    await stack.getByRole('button', { name: /details/i }).click();
    const lines = stack.locator('.toast-details li');
    await expect(lines).toHaveCount(3);
    await expect(lines.first()).toHaveText(/^\d{2}:\d{2}:\d{2}/);
  });

  test('a reported failure reaches the server-side log', async ({ page }) => {
    await page.goto('/');

    // Set the wait up before triggering, so the response can't be missed.
    const responsePromise = page.waitForResponse((response) =>
      response.url().includes('/api/telemetry/client-error'),
    );
    await page.evaluate(reject, 'Reported to the server');
    const response = await responsePromise;

    expect(response.status()).toBe(202);
    expect(await response.json()).toEqual({ logged: true });
    const payload = JSON.parse(response.request().postData() ?? '{}');
    expect(payload.message).toBe('Reported to the server');
    expect(payload.source).toBe('unhandledrejection');
    expect(payload.count).toBe(1);
    expect(payload.path).toBe('/');
    expect(Array.isArray(payload.occurrences)).toBe(true);
  });

  test('a burst is summarised by a trailing report carrying the total', async ({ page }) => {
    // The trailing report waits out the 10s send throttle, so this assertion is
    // inherently time-based: give it room for four parallel browsers to load.
    test.setTimeout(45_000);
    const posts = reported(page);
    await page.goto('/');

    await page.evaluate((count: number) => {
      for (let attempt = 0; attempt < count; attempt += 1) {
        void Promise.reject(new Error('Bursted failure'));
      }
    }, 3);

    // Leading report immediately, then one trailing report at the end of the
    // throttle window with the whole burst's count — not three posts.
    await expect.poll(() => posts.length, { timeout: 25_000 }).toBe(2);
    expect(posts[0].count).toBe(1);
    expect(posts[1].count).toBe(3);
    expect((posts[1].occurrences as string[]).length).toBe(3);
    expect(posts.every((post) => post.message === 'Bursted failure')).toBe(true);
  });

  test('different messages get their own toasts, and benign noise is ignored', async ({ page }) => {
    await page.goto('/');

    const stack = page.locator('.toast-stack');
    await page.evaluate(reject, 'First distinct failure');
    await page.evaluate(reject, 'Second distinct failure');

    // Hover pauses auto-dismiss so the count is stable to assert.
    await stack.hover();
    await expect(stack.locator('.toast')).toHaveCount(2);

    // Counted grouping is per message, so neither of these is a repeat.
    await expect(stack.locator('.toast-count')).toHaveCount(0);

    // Aborted fetches are routine and must not train users to ignore toasts.
    await page.evaluate(() => {
      void Promise.reject(new DOMException('The operation was aborted', 'AbortError'));
    });
    await expect(stack.locator('.toast')).toHaveCount(2);
  });
});
