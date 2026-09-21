/**
 * Dev-only crash injection for the error-boundary tests.
 *
 * Two ways to arm it, both inert in production builds (the `import.meta.env.DEV`
 * guard is statically replaced by Vite, so this whole check is dead-code
 * eliminated from production bundles):
 *
 *   1. `window.__AGP_CRASH_SURFACE = 'member' | 'admin'` — set by the E2E tests
 *      via evaluate, which is deterministic and avoids any ambiguity about SPA
 *      navigations.
 *   2. `?crash=member` / `?crash=admin` in the URL — handy for manual poking.
 *
 * The crash repeats on every render while armed, deliberately. React recovers
 * from a concurrent-render error by synchronously re-rendering the root, so a
 * one-shot throw would succeed on the retry and the boundary would never
 * engage — making the test pass vacuously. To verify recovery, disarm (delete
 * the global or drop the query param) and then click "Try again".
 */
export function maybeCrash(surface: 'member' | 'admin'): void {
  if (!import.meta.env.DEV) return;
  const armed =
    window.__AGP_CRASH_SURFACE ??
    new URLSearchParams(window.location.search).get('crash') ??
    undefined;
  if (armed !== surface) return;
  throw new Error(`Test crash: simulated ${surface} surface shape drift`);
}

declare global {
  interface Window {
    __AGP_CRASH_SURFACE?: string;
  }
}
