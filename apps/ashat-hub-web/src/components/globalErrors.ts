/**
 * Global error reporting: batch unexpected failures into toasts.
 *
 * Errors from event handlers, timers, and unawaited promises never reach the
 * React error boundary, so they were previously invisible. This installs
 * `window.onerror` / `unhandledrejection` listeners and exports a reporter for
 * crashes caught in code, which raise an error toast instead.
 *
 * Repeats of one message update a single toast with a ×N count, and the last
 * few occurrences stay behind its Details disclosure — an error storm reads as
 * one toast, not a stack of them. A message quiet for DEDUPE_WINDOW_MS starts a
 * fresh group.
 */
import { toast } from './toast';

/** Repeats within this window are grouped instead of shown separately. */
const DEDUPE_WINDOW_MS = 15_000;
/** Occurrences kept per group's Details list, newest first. */
const MAX_DETAILS = 5;
/** Groups kept in memory; the least recent are pruned. */
const MAX_GROUPS = 50;
/** Shortest gap between server reports for one group; a burst is summarised. */
const SEND_THROTTLE_MS = 10_000;
/** Where failures are logged for after-the-fact review. */
const REPORT_ENDPOINT = '/api/telemetry/client-error';

type Group = {
  count: number;
  details: string[];
  last: number;
  source: string;
  detail: string;
};

const groups = new Map<string, Group>();
const sentAt = new Map<string, number>();
const trailing = new Map<string, number>();

/** Benign noise that would only train users to ignore the toasts. */
const IGNORED = [
  'ResizeObserver loop', // browser layout warning, not actionable
  'Script error.', // cross-origin script with no details
  'AbortError', // fetch cancellation
  'The operation was aborted',
  'Non-Error promise rejection captured with value: undefined',
];

function describe(reason: unknown): string {
  if (reason instanceof Error) return reason.message || reason.name;
  if (typeof reason === 'string') return reason;
  try {
    return JSON.stringify(reason)?.slice(0, 200) ?? '';
  } catch {
    return String(reason);
  }
}

function forget(message: string): void {
  const timer = trailing.get(message);
  if (timer) window.clearTimeout(timer);
  trailing.delete(message);
  groups.delete(message);
  sentAt.delete(message);
}

function prune(now: number): void {
  for (const [message, group] of groups) {
    if (now - group.last > DEDUPE_WINDOW_MS) forget(message);
  }
  if (groups.size <= MAX_GROUPS) return;
  const byAge = [...groups.entries()].sort((a, b) => a[1].last - b[1].last);
  for (const [message] of byAge.slice(0, groups.size - MAX_GROUPS)) groups.delete(message);
}

/** `HH:MM:SS` local time for one detail line. */
function timestamp(): string {
  const now = new Date();
  return [now.getHours(), now.getMinutes(), now.getSeconds()]
    .map((part) => String(part).padStart(2, '0'))
    .join(':');
}

/** First line of a trace, with the `in`/`at` prefix and directory paths stripped. */
function cleanOrigin(trace: string): string {
  const first = trace.split('\n').map((line) => line.trim()).find(Boolean);
  if (!first) return '';
  return first
    .replace(/^in\s+/, '')
    .replace(/^at\s+/, '')
    .replace(/\s*\(.*\/([^/()]+)\)$/, ' ($1)')
    .replace(/\?[^\s):]*/g, ''); // drop bundler cache-busting queries (?t=…)
  
}

function basename(path: string): string {
  return path.split(/[?#]/)[0].split('/').pop() ?? path;
}

function stackOrigin(reason: unknown): string {
  if (!(reason instanceof Error) || !reason.stack) return '';
  return reason.stack.split('\n').find((line) => line.trim().startsWith('at ')) ?? '';
}

/** Where and when one occurrence happened; the message is the toast's title. */
function detailLine(context: string, origin: string): string {
  return [timestamp(), context, origin].filter(Boolean).join(' · ');
}

/** POST one group's current state; a failed report is never re-reported. */
function send(message: string, group: Group): void {
  sentAt.set(message, Date.now());
  const body = JSON.stringify({
    source: group.source,
    message,
    detail: group.detail,
    count: group.count,
    occurrences: group.details,
    path: window.location.pathname,
  });
  void fetch(REPORT_ENDPOINT, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body,
    keepalive: true,
  }).catch(() => undefined);
}

/** Queue one trailing report so the end of a burst is recorded with its total. */
function scheduleTrailing(message: string): void {
  if (trailing.has(message)) return;
  const since = Date.now() - (sentAt.get(message) ?? 0);
  const delay = Math.max(250, SEND_THROTTLE_MS - since);
  trailing.set(
    message,
    window.setTimeout(() => {
      trailing.delete(message);
      const group = groups.get(message);
      if (group) send(message, group);
    }, delay),
  );
}

/** Shape drift toasts once per message per session, then only logs. */
const toastedDrift = new Set<string>();

function record(message: string, context: string, trace: string, source: string, toastIt = true): void {
  if (!message || IGNORED.some((noise) => message.includes(noise))) return;
  const origin = cleanOrigin(trace);
  const now = Date.now();
  const previous = groups.get(message);
  const fresh = !previous || now - previous.last > DEDUPE_WINDOW_MS;
  const details = [detailLine(context, origin), ...(fresh ? [] : previous.details)].slice(0, MAX_DETAILS);
  const group: Group = {
    count: fresh ? 1 : previous.count + 1,
    details,
    last: now,
    source,
    detail: [context, origin].filter(Boolean).join(' · '),
  };
  groups.set(message, group);
  prune(now);
  if (toastIt) {
    toast(`${source.startsWith('shape') ? 'Unexpected data' : 'Something went wrong'}: ${message}${context ? ` (${context})` : ''}`, 'error', 8000, {
      key: message,
      count: group.count,
      details: group.details,
    });
  }
  const last = sentAt.get(message);
  if (last === undefined || now - last >= SEND_THROTTLE_MS) send(message, group);
  else scheduleTrailing(message);
}

/** Report a response rejected at the fetch boundary for being the wrong shape. */
export function reportShapeError(message: string): void {
  const firstThisSession = !toastedDrift.has(message);
  if (firstThisSession) toastedDrift.add(message);
  record(message, '', '', 'shape-error', firstThisSession);
}

/** Report an API field that wasn't the shape the UI expects (a safe-helper fallback). */
export function reportShapeDrift(drift: { helper: string; expected: string; got: string; origin: string }): void {
  const where = drift.origin ? ` at ${drift.origin}` : '';
  const message = `${drift.helper} expected ${drift.expected}, got ${drift.got}${where}`;
  const firstThisSession = !toastedDrift.has(message);
  if (firstThisSession) toastedDrift.add(message);
  record(message, '', '', 'shape-drift', firstThisSession);
}

/** Report a failure caught in code (a crashed surface, a failed guard). */
export function reportAppError(reason: unknown, context = '', trace = '', source = 'boundary'): void {
  record(describe(reason) || 'unknown error', context, trace, source);
}

/**
 * Install the handlers. Safe to call repeatedly — listeners are added once and
 * the returned function removes them (used by the App effect cleanup).
 */
export function installGlobalErrorHandlers(): () => void {
  const onError = (event: ErrorEvent) => {
    const message = describe(event.error) || event.message;
    record(message, '', event.filename ? `${basename(event.filename)}:${event.lineno}` : '', 'window.onerror');
  };

  const onRejection = (event: PromiseRejectionEvent) => {
    record(describe(event.reason), '', stackOrigin(event.reason), 'unhandledrejection');
  };

  window.addEventListener('error', onError);
  window.addEventListener('unhandledrejection', onRejection);
  return () => {
    window.removeEventListener('error', onError);
    window.removeEventListener('unhandledrejection', onRejection);
  };
}
