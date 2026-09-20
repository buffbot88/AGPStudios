/**
 * Shared API utilities for Galileo components.
 * Consolidates csrfToken, request, types, and helpers that were duplicated across
 * GalileoStudio, GalileoDashboard, GalileoDeployments, AdminSurface, MemberSurfaces,
 * ProjectWorkspace, and App.tsx.
 */

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

/** Coerce an API value to a displayable string; anything non-string (or null) falls back. */
export function safeText(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'bigint') return String(value);
  return fallback;
}

/** Coerce an API value to a finite number; anything else falls back (default 0). */
export function safeNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number') return Number.isFinite(value) ? value : fallback;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return fallback;
}

/** Coerce an API value to a real array; anything non-array falls back to []. */
export function safeList<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

/**
 * Format an API timestamp (unix seconds or ISO string) for display;
 * unparseable/missing values degrade to the fallback text.
 */
export function safeTime(value: unknown, fallback = 'Unknown time'): string {
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(safeText(value));
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
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
 * - On 403 csrf_failed: refreshes the session cookie and retries once.
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
  if (response.status === 403 && message === 'csrf_failed' && !_retried) {
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

  return (body || {}) as T;
}
