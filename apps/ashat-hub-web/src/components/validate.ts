/**
 * Runtime shape validation for API payloads.
 *
 * Hand-rolled because the portal has no runtime dependencies beyond React. The
 * validators are deliberately tolerant: unknown keys pass through untouched, and
 * only fields the frontend actually reads are checked, so a server that adds or
 * renames an unused field can never break the client.
 */

/** Describe a value the way the error message needs to read it. */
export function describeShape(value: unknown): string {
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

/** A payload that isn't the documented shape. Names the endpoint and the field. */
export class ShapeError extends Error {
  readonly endpoint: string;
  readonly path: string;

  constructor(endpoint: string, path: string, expected: string, value: unknown) {
    super(`${endpoint} → ${path || 'response'}: expected ${expected}, got ${describeShape(value)}`);
    this.name = 'ShapeError';
    this.endpoint = endpoint;
    this.path = path;
  }
}

export type Ctx = {
  endpoint: string;
  fail: (expected: string, path: string, value: unknown) => never;
};

export type Schema<T> = (value: unknown, path: string, ctx: Ctx) => T;

export const anyValue: Schema<unknown> = (value) => value;

export const str: Schema<string> = (value, path, ctx) =>
  typeof value === 'string' ? value : ctx.fail('string', path, value);

export const num: Schema<number> = (value, path, ctx) =>
  typeof value === 'number' && Number.isFinite(value) ? value : ctx.fail('number', path, value);

export const bool: Schema<boolean> = (value, path, ctx) =>
  typeof value === 'boolean' ? value : ctx.fail('boolean', path, value);

/** Numbers the API may serialise as strings (BIGINT columns, formatted counts). */
export const numish: Schema<number> = (value, path, ctx) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value))) return Number(value);
  return ctx.fail('number', path, value);
};

/** Timestamps the API may send as unix seconds or a SQL date string. */
export const stamp: Schema<number | string> = (value, path, ctx) => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') return value;
  return ctx.fail('timestamp', path, value);
};

export const lit = <T extends string>(...allowed: T[]): Schema<T> => (value, path, ctx) =>
  typeof value === 'string' && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : ctx.fail(allowed.join(' | '), path, value);

export const list = <T>(item: Schema<T>): Schema<T[]> => (value, path, ctx) => {
  if (!Array.isArray(value)) return ctx.fail('array', path, value);
  value.forEach((entry, index) => item(entry, `${path}[${index}]`, ctx));
  return value as T[];
};

/** Validate the declared fields in place; unknown keys are left alone. */
export const obj = <T>(fields: { [K in keyof T]: Schema<T[K]> }): Schema<T> => (value, path, ctx) => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return ctx.fail('object', path, value);
  }
  const source = value as Record<string, unknown>;
  for (const key of Object.keys(fields) as (keyof T)[]) {
    fields[key](source[key as string], path ? `${path}.${String(key)}` : String(key), ctx);
  }
  return value as T;
};

export const recordOf = <T>(item: Schema<T>): Schema<Record<string, T>> => (value, path, ctx) => {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return ctx.fail('object', path, value);
  }
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    item(entry, `${path}.${key}`, ctx);
  }
  return value as Record<string, T>;
};

export const nullable = <T>(inner: Schema<T>): Schema<T | null> => (value, path, ctx) =>
  value === null ? null : inner(value, path, ctx);

/** Absent is acceptable; absent *and* wrong is still an error. */
export const optional = <T>(inner: Schema<T>, fallback: T): Schema<T> => (value, path, ctx) =>
  value === undefined ? fallback : inner(value, path, ctx);

/** Run a schema against a parsed payload, throwing a ShapeError on mismatch. */
export function validatePayload<T>(endpoint: string, schema: Schema<T>, body: unknown): T {
  const ctx: Ctx = {
    endpoint,
    fail: (expected, path, value) => {
      throw new ShapeError(endpoint, path, expected, value);
    },
  };
  return schema(body, '', ctx);
}
