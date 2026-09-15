import { json } from '@remix-run/node';
import { collectStatus } from '~/lib/.server/status';

/**
 * Alpha-style alias exposing the queue and worker subset of /api/status at
 * the conventional /workers path.
 */
export async function loader() {
  const status = await collectStatus();

  return json(
    {
      queue: status.queue,
      workers: status.workers,
      pool: status.pool,
      updated_at: status.updated_at,
    },
    { headers: { 'cache-control': 'no-store' } },
  );
}
