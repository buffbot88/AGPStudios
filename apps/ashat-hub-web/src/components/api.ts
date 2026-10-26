/**
 * Shared API utilities for Galileo components.
 * Consolidates csrfToken, request, types, and helpers that were duplicated across
 * GalileoStudio, GalileoDashboard, GalileoDeployments, AdminSurface, MemberSurfaces,
 * ProjectWorkspace, and App.tsx.
 */

import { reportShapeDrift, reportShapeError } from './globalErrors';
import { schemaFor } from './schemas';
import { validatePayload } from './validate';

export const API = '/api';

export type ApiError = string | { message?: string; code?: string };

export class TransientError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TransientError';
  }
}

export function csrfToken(): string {
  // The API sets the CSRF token in the `ashat_csrf` cookie at login.
  const cookie = document.cookie
    .split('; ')
    .find((value) => value.startsWith('ashat_csrf='));
  return cookie ? decodeURIComponent(cookie.slice('ashat_csrf='.length)) : '';
}

/**
 * Extract a human-readable error message from an ApiError value.
 */
export function errorMessage(error: ApiError | undefined, fallback: string): string {
  if (typeof error === 'string') return error;
  return error?.message || error?.code || fallback;
}

/* ------------------------------------------------------------------ */
/* Safe field accessors                                                */
/*                                                                     */
/* The conversation_messages incident: a surface rendered a stats field */
/* the API doesn't return and `undefined.toLocaleString()` unmounted    */
/* the whole app. These helpers make every API-derived render a         */
/* non-throw: shape drift degrades to a blank/0/placeholder instead.    */
/* Use them for any value that came from an API response.               */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/* Shape drift                                                         */
/*                                                                     */
/* A fallback used to be silent: the field rendered blank and nothing   */
/* said why. Every fallback now records which helper fell back, what    */
/* arrived instead, and where the call was, so drift is visible inline  */
/* and in the server-side error log.                                    */
/* ------------------------------------------------------------------ */

export type ShapeDrift = {
  /** Stable identity: helper + call site + what arrived. */
  key: string;
  helper: string;
  expected: string;
  /** Human description of what actually arrived. */
  got: string;
  /** Call site from the stack, e.g. 'MemberSurfaces.tsx:132' ('' when unavailable). */
  origin: string;
  count: number;
};

/** Distinct drifts remembered at once; the least recent are dropped. */
const MAX_DRIFTS = 25;

const drifts = new Map<string, ShapeDrift>();
const driftListeners = new Set<() => void>();
let driftSnapshot: ShapeDrift[] = [];

/** Call site that reached a helper, skipping this module's own frames. */
function callerOrigin(): string {
  const stack = new Error().stack;
  if (!stack) return '';
  for (const frame of stack.split('\n').slice(1)) {
    const text = frame.trim();
    if (!text.startsWith('at ')) continue;
    // Skip the helper, noteDrift, and this function itself.
    if (/[/\\]api\.ts|api\.ts:|[/\\]api\.js/.test(text)) continue;
    const match = text.match(/([^/\\()\s]+:\d+)(?::\d+)?\)?$/);
    if (match) return match[1];
  }
  return '';
}

/** Describe an unexpected value for the log, capped so payloads stay small. */
function describeShape(value: unknown): string {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (typeof value === 'number') return Number.isFinite(value) ? `number ${value}` : `number ${String(value)}`;
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) return 'empty string';
    return `string "${trimmed.slice(0, 40)}"`;
  }
  if (Array.isArray(value)) return `array(${value.length})`;
  if (typeof value === 'object') return `object(${Object.keys(value as object).slice(0, 4).join(',') || 'no keys'})`;
  return typeof value;
}

function publishDrifts(): void {
  driftSnapshot = [...drifts.values()].sort((a, b) => b.count - a.count);
  for (const listener of driftListeners) listener();
}

function noteDrift(helper: string, expected: string, value: unknown): void {
  const origin = callerOrigin();
  const got = describeShape(value);
  const key = `${helper}|${origin}|${got}`;
  const drift: ShapeDrift = { key, helper, expected, got, origin, count: (drifts.get(key)?.count ?? 0) + 1 };
  drifts.set(key, drift);
  if (drifts.size > MAX_DRIFTS) {
    const oldest = [...drifts.values()].sort((a, b) => a.count - b.count)[0];
    if (oldest) drifts.delete(oldest.key);
  }
  publishDrifts();
  // Built from plain strings on purpose: reporting must not itself coerce.
  reportShapeDrift({ helper, expected, got, origin });
}

/** Current drifts, most frequent first (snapshot for useSyncExternalStore). */
export function shapeDrifts(): ShapeDrift[] {
  return driftSnapshot;
}

/** Subscribe to drift changes; returns the unsubscribe function. */
export function subscribeShapeDrift(listener: () => void): () => void {
  driftListeners.add(listener);
  return () => driftListeners.delete(listener);
}

/** Forget every recorded drift (the inline notice's Dismiss). */
export function clearShapeDrifts(): void {
  drifts.clear();
  publishDrifts();
}

/** Coerce an API value to a displayable string; anything non-string (or null) falls back. */
export function safeText(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'bigint') return String(value);
  noteDrift('safeText', 'string', value);
  return fallback;
}

/** Coerce an API value to a finite number; anything else falls back (default 0). */
export function safeNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number') {
    if (Number.isFinite(value)) return value;
    noteDrift('safeNumber', 'finite number', value);
    return fallback;
  }
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value);
  }
  noteDrift('safeNumber', 'finite number', value);
  return fallback;
}

/** Coerce an API value to a real array; anything non-array falls back to []. */
export function safeList<T = unknown>(value: unknown): T[] {
  if (Array.isArray(value)) return value as T[];
  noteDrift('safeList', 'array', value);
  return [];
}

/**
 * Format an API timestamp (unix seconds or ISO string) for display;
 * unparseable/missing values degrade to the fallback text.
 */
export function safeTime(value: unknown, fallback = 'Unknown time'): string {
  // Deliberately not delegating to safeText: one bad value must produce one
  // drift entry, not a second one from the helper it leans on.
  const raw = typeof value === 'number' ? String(value) : typeof value === 'string' ? value : '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(raw);
  if (!Number.isNaN(date.getTime())) return date.toLocaleString();
  noteDrift('safeTime', 'timestamp', value);
  return fallback;
}

/**
 * Encode a file path for use in a URL, preserving slashes between segments.
 */
export function encodeFilePath(path: string): string {
  return path
    .split('/')
    .map((segment) => encodeURIComponent(segment))
    .join('/');
}

/**
 * Core API request helper with automatic CSRF handling and 503 retry.
 *
 * - Adds Accept/Content-Type/X-CSRF-Token headers automatically.
 * - On 403 csrf_failed (legacy: csrf token required): refreshes the session cookie and retries once.
 * - On 503: waits 1.2s and retries once (throws TransientError on second failure).
 * - Parses JSON body and surfaces `error` field from the response.
 */
export async function request<T>(
  url: string,
  init?: RequestInit,
  _retried = false,
  _retried503 = false,
): Promise<T> {
  const { headers: initHeaders, ...rest } = init || {};
  const method = (init?.method || 'GET').toUpperCase();
  const mutating = method === 'POST' || method === 'PUT' || method === 'DELETE';
  const response = await fetch(url, {
    ...rest,
    credentials: 'same-origin',
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(mutating ? { 'X-CSRF-Token': csrfToken() } : {}),
      ...(initHeaders || {}),
    },
  });

  const text = await response.text();
  let body: (T & { error?: ApiError }) | null = null;
  try {
    body = text ? (JSON.parse(text) as T & { error?: ApiError }) : null;
  } catch {
    /* handled below */
  }

  const error = body?.error;
  const message = errorMessage(error, '');

  // CSRF retry: refresh session cookie then retry once
  if (response.status === 403 && (message === 'csrf_failed' || message === 'csrf token required') && !_retried) {
    await fetch(`${API}/auth/session`, { credentials: 'same-origin' });
    return request<T>(url, init, true, _retried503);
  }

  // 503 retry: wait and retry once for transient errors
  if (response.status === 503 && !_retried503) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    return request<T>(url, init, _retried, true);
  }

  if (!response.ok) {
    const code =
      typeof error === 'string' ? error : error?.code || `http_${response.status}`;
    const requestId =
      typeof error === 'object' && error !== null
        ? (error as Record<string, unknown>).request_id
        : undefined;
    console.error(
      `[Galileo] ${response.status} ${code}: ${url}${requestId ? ` (${requestId})` : ''}`,
    );
    if (response.status === 503) {
      throw new TransientError(message || 'Service temporarily unavailable');
    }
    throw new Error(message || `Request failed (${response.status})`);
  }

  // Success payloads are checked against the documented contract here, at the
  // boundary, so a wrong shape names its field instead of reaching a component
  // that would render a blank. Error bodies are already handled above.
  const parsed = (body || {}) as T;
  const schema = schemaFor(new URL(url, window.location.origin).pathname);
  if (schema) {
    try {
      validatePayload(`${method} ${url}`, schema, parsed);
    } catch (reason) {
      if (reason instanceof Error) reportShapeError(reason.message);
      throw reason;
    }
  }
  return parsed;
}
