import { json } from '@remix-run/node';
import { collectStatus } from '~/lib/.server/status';

/**
 * Reports gateway reachability, queue capacity, Liquid backend health, and
 * Alpha-managed capacity. Peer telemetry remains owned by AshatHub.
 */
export async function loader() {
  return json(await collectStatus(), { headers: { 'cache-control': 'no-store' } });
}
