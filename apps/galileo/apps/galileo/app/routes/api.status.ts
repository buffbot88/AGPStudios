import { json } from '@remix-run/node';
import { getAgents, getGatewayURL } from '~/lib/.server/config';

const PROBE_TIMEOUT_MS = 5_000;

interface GatewayHealth {
  status: string;
  version: string;
}

interface GatewayStatus {
  queued_requests: number;
  max_queue: number;
  active_requests: number;
  available_text_slots: number;
  available_agent_slots: number;
  available_vision_slots: number;
}

interface GatewayWorkers {
  text_worker_healthy: boolean;
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
 * Reports gateway reachability, queue capacity, local worker health, and
 * Omega/Beta/Delta pool health. Always returns 200; degradation is in the body.
 */
export async function loader() {
  const gatewayURL = getGatewayURL().replace(/\/+$/, '');
  const baseURL = gatewayURL.endsWith('/v1') ? gatewayURL.slice(0, -3) : gatewayURL;

  const [health, status, workers, ...agents] = await Promise.all([
    probe<GatewayHealth>(`${baseURL}/health`),
    probe<GatewayStatus>(`${baseURL}/status`),
    probe<GatewayWorkers>(`${baseURL}/workers`),
    ...getAgents().map(async (agent) => {
      const headers: Record<string, string> = { accept: 'application/json' };
      const agentKey = process.env.ASHAT_AGENT_API_KEY;
      if (agentKey) headers['X-Ashat-Key'] = agentKey;
      const result = await probe<unknown>(`${agent.url}/health`, headers);

      return {
        id: agent.id,
        healthy: result.ok,
        latency_ms: result.latency_ms,
        error: result.error ?? null,
      };
    }),
  ]);

  const reachable = health.ok && health.data?.status === 'ok';
  const healthyAgents = agents.filter((agent) => agent.healthy).length;

  return json(
    {
      ok: reachable && healthyAgents > 0,
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
        healthy_agents: healthyAgents,
        total_agents: agents.length,
      },
      agents,
      updated_at: new Date().toISOString(),
    },
    { headers: { 'cache-control': 'no-store' } },
  );
}
