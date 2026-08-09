import type {
  LaneKey,
  LaneStatus,
  MetricsSummary,
  PublicMetrics,
  PublicStatus,
  TelemetryFrame,
  TelemetrySnapshot,
  TimeseriesResponse,
} from './types';

const POLL_MS = 8_000;

function demoFrames(): TelemetryFrame[] {
  const now = Date.now();
  return Array.from({ length: 24 }, (_, index) => {
    const wave = Math.sin(index * 0.64) * 3.4;
    const drift = index * 0.11;
    return {
      timestamp: new Date(now - (23 - index) * 8_000).toISOString(),
      generation_tokens_per_second: Math.max(1, 25 + wave + drift),
      prompt_tokens_per_second: Math.max(1, 68 + Math.cos(index * 0.42) * 8),
      total_latency_ms: 410 - wave * 12 + index * 1.4,
      time_to_first_token_ms: 82 + Math.cos(index * 0.3) * 8,
      success: true,
    };
  });
}

function demoOmegaLane(): LaneStatus {
  return {
    label: 'Omega',
    model: 'LFM2.5-1.2B-Instruct-Q4_K_M.gguf',
    ctx: 4096,
    available: true,
    ready: true,
    lane_state: 'online',
    total_requests: 1284,
    success_rate: 99.7,
    total_prompt_tokens: 482_190,
    total_completion_tokens: 146_820,
    quickest_generation_tokens_per_second: 31.8,
    slowest_generation_tokens_per_second: 19.4,
    latest_generation_tokens_per_second: 27.6,
    avg_generation_tokens_per_second: 25.8,
    avg_prompt_tokens_per_second: 72.4,
    avg_total_latency_ms: 428.6,
    last_time_to_first_token_ms: 79,
    avg_time_to_first_token_ms: 86.2,
    last_request_time: new Date().toISOString(),
    last_failure_code: null,
    reason_message: null,
  };
}

function offlineLane(label: LaneKey): LaneStatus {
  return {
    label,
    model: '—',
    ctx: 0,
    available: false,
    ready: false,
    lane_state: 'offline',
    total_requests: 0,
    success_rate: 0,
    total_prompt_tokens: 0,
    total_completion_tokens: 0,
    quickest_generation_tokens_per_second: 0,
    slowest_generation_tokens_per_second: 0,
    latest_generation_tokens_per_second: 0,
    avg_generation_tokens_per_second: 0,
    avg_prompt_tokens_per_second: 0,
    avg_total_latency_ms: 0,
    last_time_to_first_token_ms: null,
    avg_time_to_first_token_ms: null,
    last_request_time: null,
    last_failure_code: null,
    reason_message: null,
  };
}

function demoSummary(lane: LaneStatus): MetricsSummary {
  return {
    total_requests: lane.total_requests,
    success_count: lane.total_requests,
    failure_count: 0,
    success_rate: lane.success_rate,
    avg_generation_tokens_per_second: lane.avg_generation_tokens_per_second,
    avg_prompt_tokens_per_second: lane.avg_prompt_tokens_per_second,
    avg_total_latency_ms: lane.avg_total_latency_ms,
    quickest_generation_tokens_per_second: lane.quickest_generation_tokens_per_second,
    slowest_generation_tokens_per_second: lane.slowest_generation_tokens_per_second,
    latest_generation_tokens_per_second: lane.latest_generation_tokens_per_second,
    last_request_time: lane.last_request_time,
    last_success: lane.lane_state === 'online',
    total_prompt_tokens: lane.total_prompt_tokens,
    total_completion_tokens: lane.total_completion_tokens,
    last_time_to_first_token_ms: lane.last_time_to_first_token_ms,
    avg_time_to_first_token_ms: lane.avg_time_to_first_token_ms,
    last_failure_code: lane.last_failure_code,
    label: lane.label,
    model: lane.model,
    ctx: lane.ctx,
    available: lane.available,
    ready: lane.ready,
    lane_state: lane.lane_state,
    reason_message: lane.reason_message,
  };
}

export function demoSnapshot(): TelemetrySnapshot {
  const omega = demoOmegaLane();
  const beta = offlineLane('beta');
  const delta = offlineLane('delta');
  const frames = demoFrames();
  const metrics: PublicMetrics = {
    uptime_seconds: 86_420,
    summaries: { omega: demoSummary(omega), beta: demoSummary(beta), delta: demoSummary(delta) },
    total_events: 14,
    recent_events: [
      '[now] omega: inference completed (124+48 tokens)',
      '[2m ago] omega: inference completed (86+32 tokens)',
      '[5m ago] omega: server ready (backend=cpu)',
    ],
  };
  const status: PublicStatus = {
    uptime_seconds: metrics.uptime_seconds,
    llama_server_available: true,
    degraded: false,
    queue: { depth: 0, limit: 32 },
    lanes: { omega, beta, delta },
    all_ready: omega.lane_state === 'online',
    orchestrator_pool: {
      ports_total: 4,
      ports_active: 1,
      baseline_alive: true,
      extras_active: [],
      queue_depth: 0,
      queue_limit: 32,
      last_failure_reason: null,
    },
    coding_agent_pool: {
      ports_total: 3,
      ports_active: 0,
      baseline_alive: true,
      extras_active: [],
      queue_depth: 0,
      queue_limit: 32,
      last_failure_reason: null,
    },
  };
  const timeseries: TimeseriesResponse = {
    omega: frames,
    beta: demoFrames().map((f, i) => ({ ...f, generation_tokens_per_second: f.generation_tokens_per_second !== null ? Math.max(1, f.generation_tokens_per_second * 0.82) : null })),
    delta: demoFrames().map((f, i) => ({ ...f, generation_tokens_per_second: f.generation_tokens_per_second !== null ? Math.max(1, f.generation_tokens_per_second * 0.64) : null })),
    events: metrics.recent_events.map((msg) => ({ event: msg })),
  };
  return { status, metrics, timeseries, demo: true, updatedAt: Date.now() };
}

async function fetchJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${path} returned ${response.status}`);
  return response.json() as Promise<T>;
}

export async function fetchSnapshot(): Promise<TelemetrySnapshot> {
  const [status, metrics, timeseries] = await Promise.all([
    fetchJson<PublicStatus>('/api/public_status'),
    fetchJson<PublicMetrics>('/api/public_metrics'),
    fetchJson<TimeseriesResponse>('/api/dashboard_timeseries'),
  ]);
  return {
    status,
    metrics,
    timeseries,
    demo: false,
    updatedAt: Date.now(),
  };
}

export function startPolling(onSnapshot: (snapshot: TelemetrySnapshot) => void, onError: () => void): () => void {
  let disposed = false;
  const tick = async () => {
    try {
      const snapshot = await fetchSnapshot();
      if (!disposed) onSnapshot(snapshot);
    } catch {
      if (!disposed) onError();
    }
  };

  void tick();
  const timer = window.setInterval(() => void tick(), POLL_MS);
  return () => {
    disposed = true;
    window.clearInterval(timer);
  };
}
