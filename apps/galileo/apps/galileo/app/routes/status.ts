import { json } from '@remix-run/node';
import { collectStatus } from '~/lib/.server/status';

/**
 * Alpha-style alias for /api/status so gateway-shaped probes answer at the
 * conventional /status path instead of 404ing.
 */
export async function loader() {
  return json(await collectStatus(), { headers: { 'cache-control': 'no-store' } });
}
