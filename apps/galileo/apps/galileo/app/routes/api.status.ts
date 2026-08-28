import { json } from '@remix-run/node';
import { getGatewayURL } from '~/lib/.server/llm/api-key';

const PROBE_TIMEOUT_MS = 5_000;

/**
 * Defaults mirror alpha-server's deployed agent pool (crates/alpha-server/config.toml).
 * Override without code changes via ASHAT_AGENT_ENDPOINTS="omega=https://host,beta=https://host:8082,delta=https://host:8088".
 */
const DEFAULT_AGENTS = [
  { id: 'omega', url: 'https://129.213.94.124' },
  { id: 'beta', url: 'https://150.136.208.93:8082' },
  { id: 'delta', url: 'https://129.213.147.225:8088' },
];

interface GatewayHealth {
  status: string;
  version: string;
}

interface GatewayStatus {
  queue_size: number;
  max_queue: number;
  available_slots: number;
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

async function probe<T>(url: string): Promise<ProbeResult<T>> {
  const started = Date.now();

  try {
    const response = await fetch(url, {
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
      headers: { accept: 'application/json' },
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

function parseAgents() {
  const raw = process.env.ASHAT_AGENT_ENDPOINTS;

  if (!raw) {
    return DEFAULT_AGENTS;
  }

  return raw
    .split(',')
    .map((entry) => {
      const [id, url] = entry.split('=').map((part) => part.trim());

      return { id, url };
    })
    .filter((agent): agent is { id: string; url: string } => Boolean(agent.id && agent.url));
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
    ...parseAgents().map(async (agent) => {
      const result = await probe<unknown>(`${agent.url}/health`);

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
