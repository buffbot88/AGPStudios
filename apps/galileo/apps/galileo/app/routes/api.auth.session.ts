import { json } from '@remix-run/node';
import { authenticated, getUser } from '~/lib/.server/auth';

/**
 * Session endpoint consumed by AuthGate (browser fetch with credentials).
 *
 * - Development: returns a mock session so the UI is reachable on localhost,
 *   where the agpstudios.org login cookies do not exist.
 * - Production: forwards the request's cookies to the agpstudios.org session
 *   endpoint, mirroring lib/.server/auth.ts, so the gate reflects the real
 *   sign-in state regardless of how the edge proxy routes /api/auth/*.
 */
export async function loader({ request }: { request: Request }) {
  if (import.meta.env.DEV) {
    return json({ authenticated: true as const, user: { username: 'dev' } });
  }

  if (!(await authenticated(request))) {
    return json({ authenticated: false as const });
  }

  const user = await getUser(request);

  return json({ authenticated: true as const, user });
}
