import { json } from '@remix-run/node';

/**
 * Cheap liveness endpoint for Alpha-style uptime probes; deliberately skips
 * the gateway fan-out that /api/status performs.
 */
export async function loader() {
  return json({ status: 'ok', service: 'galileo' }, { headers: { 'cache-control': 'no-store' } });
}
