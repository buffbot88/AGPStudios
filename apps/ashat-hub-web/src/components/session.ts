/**
 * Shared auth session store.
 *
 * A single /api/auth/session request is shared by every consumer (App,
 * AuthPanel, route guards). The resolved user is mirrored to sessionStorage so
 * the next full page load hydrates instantly, then revalidates in the
 * background (stale-while-revalidate).
 *
 * Long-lived tabs stay fresh: the session is revalidated every five minutes
 * and whenever the tab becomes visible again, so a server-side expiry (or a
 * ban/logout in another tab) is detected without user interaction.
 *
 * Network failures never downgrade a cached session — only a definitive
 * `authenticated: false` from the API clears it.
 */

export type SessionUser = {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: string;
};

type Listener = (user: SessionUser | null) => void;

const STORAGE_KEY = 'ashat_session';
const REVALIDATE_INTERVAL_MS = 5 * 60_000;

/** Result of a revalidation attempt. 'offline' means the API was unreachable. */
type FetchResult = SessionUser | null | 'offline';

let cachedUser: SessionUser | null | undefined = readCached();
let inFlight: Promise<SessionUser | null> | undefined;
const listeners = new Set<Listener>();
const expiryListeners = new Set<() => void>();

function readCached(): SessionUser | null | undefined {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    if (raw === null) return undefined;
    const parsed = JSON.parse(raw) as SessionUser;
    return parsed && typeof parsed.id === 'string' ? parsed : null;
  } catch {
    return undefined;
  }
}

function writeCached(user: SessionUser | null) {
  try {
    if (user) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(user));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    /* storage unavailable (private mode, quota) — session still works, just without hydration */
  }
}

async function requestSession(): Promise<FetchResult> {
  try {
    const response = await fetch('/api/auth/session', { credentials: 'same-origin' });
    const data = (await response.json()) as { authenticated?: boolean; user?: SessionUser };
    return data.authenticated ? data.user ?? null : null;
  } catch {
    return 'offline';
  }
}

function apply(result: FetchResult): SessionUser | null {
  inFlight = undefined;
  if (result === 'offline') {
    // Unreachable API: keep whatever we had (hydrated cache or null on cold load).
    if (cachedUser === undefined) cachedUser = null;
    return cachedUser;
  }
  // Detect a session that expired server-side (revalidation found the user
  // gone) as opposed to an intentional local logout (setSessionUser(null)).
  const expired = cachedUser !== null && cachedUser !== undefined && result === null;
  cachedUser = result;
  writeCached(result);
  if (expired) expiryListeners.forEach((listener) => listener());
  notify(result);
  return result;
}

function notify(user: SessionUser | null) {
  for (const listener of listeners) listener(user);
}

/** Subscribe to session-expired events (server-side sign-out detected on revalidation). */
export function onSessionExpired(listener: () => void): () => void {
  expiryListeners.add(listener);
  return () => expiryListeners.delete(listener);
}

/**
 * Resolve the current session.
 *
 * Cold load (no cache): awaits the network so callers get a definitive answer.
 * Warm load (hydrated cache): returns the cached user immediately and kicks
 * off a background revalidation whose result flows to subscribers.
 */
export function getSession(): Promise<SessionUser | null> {
  if (inFlight) return inFlight;
  if (cachedUser !== undefined) {
    void revalidate();
    return Promise.resolve(cachedUser);
  }
  inFlight = requestSession().then(apply);
  return inFlight;
}

/** Force a fresh check against the API. Resolves with the (possibly unchanged) user. */
export function revalidate(): Promise<SessionUser | null> {
  inFlight ??= requestSession().then(apply);
  return inFlight;
}

/** Current cached user, if the session has already been resolved or hydrated. */
export function peekSession(): SessionUser | null | undefined {
  return cachedUser;
}

/** Subscribe to session changes. Returns an unsubscribe function. */
export function onSessionChange(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Update the shared session after a login or logout elsewhere in the app. */
export function setSessionUser(user: SessionUser | null) {
  cachedUser = user;
  writeCached(user);
  notify(user);
}

// Periodic revalidation, paused while the tab is hidden (the visibility
// listener catches up the moment the tab is shown again).
if (typeof document !== 'undefined') {
  window.setInterval(() => {
    if (document.visibilityState === 'visible') void revalidate();
  }, REVALIDATE_INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void revalidate();
  });
  window.addEventListener('focus', () => void revalidate());
}
