import { json } from '@remix-run/node';
import { getGatewayURL } from '~/lib/.server/config';

const PROBE_TIMEOUT_MS = 5_000;

interface GatewayHealth {
  status: string;
  version: string;
}

interface GatewayStatus {
  queued_requests: number;
  max_queue: number;
  active_requests: number;
  available_liquid_slots: number;
  available_vision_slots: number;
}

interface GatewayWorkers {
  liquid_backend_healthy: boolean;
  vision_worker_active: boolean;
}

interface ProbeResult<T> {
  ok: boolean;
  data?: T;
  error?: string;
  latency_ms: number;
}

async function probe<T>(url: string, headers?: HeadersInit): Promise<ProbeResult<T>> {
  const started = Date.now();

  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      headers,
      cache: 'no-store',
    });

    if (!response.ok) {
      return { ok: false, error: `HTTP ${response.status}`, latency_ms: Date.now() - started };
    }

    return { ok: true, data: (await response.json()) as T, latency_ms: Date.now() - started };
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      latency_ms: Date.now() - started,
    };
  }
}

/**
 * Reports gateway reachability, queue capacity, Liquid backend health, and
 * Alpha-managed capacity. Peer telemetry remains owned by AshatHub.
 */
export async function loader() {
  const gatewayURL = getGatewayURL().replace(/\/+$/, '');
  const baseURL = gatewayURL.endsWith('/v1') ? gatewayURL.slice(0, -3) : gatewayURL;

  const [health, status, workers] = await Promise.all([
    probe<GatewayHealth>(`${baseURL}/health`),
    probe<GatewayStatus>(`${baseURL}/status`),
    probe<GatewayWorkers>(`${baseURL}/workers`),
  ]);

  const reachable = health.ok && health.data?.status === 'ok';

  return json(
    {
      ok: reachable,
      gateway: {
        url: gatewayURL,
        reachable,
        version: health.data?.version ?? null,
        error: health.error ?? null,
        latency_ms: health.latency_ms,
      },
      queue: status.data ?? null,
      workers: workers.data ?? null,
      pool: {
        managed_by: 'alpha',
        available_liquid_slots: status.data?.available_liquid_slots ?? null,
      },
      updated_at: new Date().toISOString(),
    },
    { headers: { 'cache-control': 'no-store' } },
  );
}
