/**
 * Response schemas for the endpoints the frontend reads.
 *
 * Written from docs/python-api-contracts.md plus the types the components
 * consume. Coverage is deliberately partial: an endpoint without an entry here
 * is simply not validated, which is how coverage grows without risking a page
 * that works today. Fields the API may omit are `optional`, fields it may send
 * as null are `nullable`, and timestamps accept either unix seconds or a SQL
 * date string because the API does both.
 */
import {
  anyValue, bool, lit, list, nullable, num, numish, obj, optional, recordOf, stamp, str,
  type Schema,
} from './validate';

/** Shape of `user` in session, account, and identity payloads. */
const user = obj<{
  id: string; username: string; email: string; display_name: string; role: string;
  tag_name?: string | null; discord_tag?: string | null; location?: string | null;
  interests?: string | null;
}>({
  id: str,
  username: str,
  email: str,
  display_name: str,
  role: str,
  tag_name: optional(nullable(str), null),
  discord_tag: optional(nullable(str), null),
  location: optional(nullable(str), null),
  interests: optional(nullable(str), null),
});

const gameStatus = lit('live', 'in-development', 'coming-soon');

const game = obj<{
  slug: string; title: string; summary: string; status: string; tags: string[];
  url: string; poster: string | null; featured: boolean; sort_order?: number;
}>({
  slug: str,
  title: str,
  summary: str,
  status: gameStatus,
  tags: list(str),
  url: str,
  // The API sends null for "no art configured", which the poster fallback handles.
  poster: nullable(str),
  featured: bool,
  sort_order: optional(num, 0),
});

const ticketSummary = obj<{
  id: string; subject: string; status: string; priority: string; category: string;
  preview: string; created_at: string | number; updated_at: string | number;
}>({
  id: str,
  subject: str,
  status: str,
  priority: str,
  category: str,
  preview: str,
  created_at: stamp,
  updated_at: stamp,
});

const ticketReply = obj<{
  id: string; ticket_id: string; user_id: string; message: string;
  is_staff: number; created_at: string | number; username?: string; display_name?: string;
}>({
  id: str,
  ticket_id: str,
  user_id: str,
  message: str,
  is_staff: numish,
  created_at: stamp,
  username: optional(str, ''),
  display_name: optional(str, ''),
});

const adminUser = obj<{
  id: string; username: string; email: string; display_name: string; role: string;
  is_active: number; banned_at?: number | null; email_verified_at?: number | string | null;
}>({
  id: str,
  username: str,
  email: str,
  display_name: str,
  role: str,
  is_active: numish,
  banned_at: optional(nullable(numish), null),
  email_verified_at: optional(nullable(stamp), null),
});

const migrationEntry = obj<{
  filename: string; applied_at?: number | string | null; statements?: number | null;
}>({
  filename: str,
  applied_at: optional(nullable(stamp), null),
  statements: optional(nullable(numish), null),
});

const migrationSummary = obj<{
  tracked: boolean; tracking_table: string; current_revision: string | null;
  applied: unknown[]; pending: unknown[]; drifted: string[]; missing: string[];
}>({
  tracked: bool,
  tracking_table: str,
  current_revision: nullable(str),
  applied: list(migrationEntry),
  pending: list(obj<{ filename: string; statements?: number }>({
    filename: str,
    statements: optional(numish, 0),
  })),
  drifted: list(str),
  missing: list(str),
});

const clientError = obj<{
  id: number; source?: string; message?: string; detail?: string; path?: string;
  count?: number; occurrences?: string; user_id?: string; created_at?: number;
}>({
  id: numish,
  source: optional(str, ''),
  message: optional(str, ''),
  detail: optional(str, ''),
  path: optional(str, ''),
  count: optional(numish, 1),
  occurrences: optional(str, '[]'),
  user_id: optional(str, ''),
  created_at: optional(numish, 0),
});

/** Endpoints whose whole response shape is known. Keyed by path after `/api`. */
const byPath: Record<string, Schema<unknown>> = {
  '/games': obj<{ games: unknown[] }>({ games: list(game) }),
  '/news': obj<{ news: unknown[] }>({
    news: list(obj<{ id: string; title: string; summary: string; url: string; published_at: number }>({
      id: str,
      title: str,
      summary: str,
      url: str,
      published_at: numish,
    })),
  }),
  '/auth/session': obj<{ authenticated: boolean; user?: unknown }>({
    authenticated: bool,
    user: optional(nullable(user), null),
  }),
  '/account/summary': obj<{ user: unknown; github_linked?: boolean; stats: unknown }>({
    user,
    github_linked: optional(bool, false),
    stats: obj<{ support_tickets: number }>({ support_tickets: numish }),
  }),
  '/support': obj<{ tickets: unknown[] }>({ tickets: list(ticketSummary) }),
  '/admin/summary': obj<{
    users: number; active_users: number; disabled_users: number;
    open_tickets: number; active_projects: number; active_deploys: number;
  }>({
    users: numish,
    active_users: numish,
    disabled_users: numish,
    open_tickets: numish,
    active_projects: numish,
    active_deploys: numish,
  }),
  '/admin/users': obj<{ users: unknown[]; page?: number; page_size?: number }>({
    users: list(adminUser),
    page: optional(numish, 1),
    page_size: optional(numish, 50),
  }),
  '/admin/database/status': obj<{
    database: string; version: string | number; paws?: unknown; migrations?: unknown;
  }>({
    database: str,
    version: stamp,
    paws: optional(anyValue, undefined),
    // Optional: a status response without migration state is reported by the
    // panel as "not reported", which is more useful than rejecting the page.
    migrations: optional(anyValue, undefined),
  }),
  '/admin/games': obj<{ games: unknown[] }>({ games: list(game) }),
  '/admin/client-errors': obj<{ errors: unknown[]; page?: number; page_size?: number }>({
    errors: list(clientError),
    page: optional(numish, 1),
    page_size: optional(numish, 50),
  }),
  '/admin/settings': recordOf(anyValue) as Schema<unknown>,
};

/** Endpoints with a path parameter. */
const byPattern: { pattern: RegExp; schema: Schema<unknown> }[] = [
  { pattern: /^\/support\/[^/]+$/, schema: obj<{ ticket: unknown; replies: unknown[] }>({
    ticket: obj<{ id: string; user_id: string; subject: string; status: string; priority: string; category: string; message: string }>({
      id: str,
      user_id: str,
      subject: str,
      status: str,
      priority: str,
      category: str,
      message: str,
    }),
    replies: list(ticketReply),
  }) },
  { pattern: /^\/admin\/users\/[^/]+\/moderation$/, schema: obj<{ user_id: string; events: unknown[] }>({
    user_id: str,
    events: list(obj<{ id: number; action: string; actor_name: string; detail: string; created_at: number }>({
      id: numish,
      action: str,
      actor_name: str,
      detail: str,
      created_at: numish,
    })),
  }) },
];

/** Schema for a request path (with or without `/api`), or undefined if unvalidated. */
export function schemaFor(path: string): Schema<unknown> | undefined {
  const clean = path.replace(/^\/api/, '').split('?')[0].replace(/\/$/, '') || '/';
  const exact = byPath[clean];
  if (exact) return exact;
  return byPattern.find((entry) => entry.pattern.test(clean))?.schema;
}
