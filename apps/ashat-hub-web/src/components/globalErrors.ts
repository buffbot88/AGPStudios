/**
 * Global error reporting: surface unexpected errors as toasts.
 *
 * Errors from event handlers, timers, and unawaited promises never reach the
 * React error boundary, so they were previously invisible — the app just did
 * nothing. This installs `window.onerror` / `unhandledrejection` listeners that
 * raise an error toast so silent failures become visible.
 *
 * Noise control: duplicates of the same message are suppressed for a window,
 * and a small set of known-benign browser noise is ignored entirely.
 */
import { toast } from './toast';

/** Same message repeated within this window is shown once. */
const DEDUPE_WINDOW_MS = 15_000;

const recent = new Map<string, number>();

/** Benign noise that would only train users to ignore the toasts. */
const IGNORED = [
  'ResizeObserver loop', // browser layout warning, not actionable
  'Script error.', // cross-origin script with no details
  'AbortError', // fetch cancellation
  'The operation was aborted',
  'Non-Error promise rejection captured with value: undefined',
];

function shouldReport(message: string): boolean {
  if (!message) return false;
  if (IGNORED.some((noise) => message.includes(noise))) return false;
  const now = Date.now();
  const last = recent.get(message);
  if (last !== undefined && now - last < DEDUPE_WINDOW_MS) return false;
  for (const [key, timestamp] of recent) {
    if (now - timestamp > DEDUPE_WINDOW_MS) recent.delete(key);
  }
  recent.set(message, now);
  return true;
}

function describe(reason: unknown): string {
  if (reason instanceof Error) return reason.message || reason.name;
  if (typeof reason === 'string') return reason;
  try {
    return JSON.stringify(reason)?.slice(0, 200) ?? '';
  } catch {
    return String(reason);
  }
}

/**
 * Install the handlers. Safe to call repeatedly — listeners are added once and
 * the returned function removes them (used by the App effect cleanup).
 */
export function installGlobalErrorHandlers(): () => void {
  const onError = (event: ErrorEvent) => {
    const message = describe(event.error) || event.message;
    if (shouldReport(message)) {
      toast(`Something went wrong: ${message}`, 'error', 8000);
    }
  };

  const onRejection = (event: PromiseRejectionEvent) => {
    const message = describe(event.reason);
    if (shouldReport(message)) {
      toast(`Something went wrong: ${message}`, 'error', 8000);
    }
  };

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
  };
}
