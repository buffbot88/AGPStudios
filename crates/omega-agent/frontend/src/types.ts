export type LaneState = 'online' | 'busy' | 'waking' | 'degraded' | 'offline';

export interface LaneStatus {
  label: string;
  model: string;
  ctx: number;
  available: boolean;
  ready: boolean;
  lane_state: LaneState;
  total_requests: number;
  success_rate: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  quickest_generation_tokens_per_second: number;
  slowest_generation_tokens_per_second: number;
  latest_generation_tokens_per_second: number;
  avg_generation_tokens_per_second: number;
  avg_prompt_tokens_per_second: number;
  avg_total_latency_ms: number;
  last_time_to_first_token_ms: number | null;
  avg_time_to_first_token_ms: number | null;
  last_request_time: string | null;
  last_failure_code: string | null;
  reason_message: string | null;
}

export type LaneKey = 'omega' | 'beta' | 'delta';

export interface PoolSnapshot {
  ports_total: number;
  ports_active: number;
  baseline_alive: boolean;
  extras_active: number[];
  queue_depth: number;
  queue_limit: number;
  last_failure_reason: string | null;
}

export interface PublicStatus {
  uptime_seconds: number;
  llama_server_available: boolean;
  degraded: boolean;
  queue: { depth: number; limit: number };
  lanes: Record<LaneKey, LaneStatus>;
  all_ready: boolean;
  orchestrator_pool: PoolSnapshot;
  coding_agent_pool: PoolSnapshot;
  /** How many 1.2B Coding Agent lanes are alive right now (master). */
  lanes_in_use?: number;
  /** Maximum concurrent 1.2B Coding Agent lanes (master). */
  lanes_capacity?: number;
}

export interface MetricsSummary extends LaneStatus {
  success_count: number;
  failure_count: number;
  last_success: boolean;
  total_events?: number;
}

export interface PublicMetrics {
  uptime_seconds: number;
  summaries: Record<LaneKey, MetricsSummary>;
  total_events: number;
  recent_events: string[];
}

export interface TelemetryFrame {
  timestamp: string;
  generation_tokens_per_second: number | null;
  prompt_tokens_per_second: number | null;
  total_latency_ms: number;
  time_to_first_token_ms: number | null;
  success: boolean;
}

export interface TimeseriesResponse {
  omega: TelemetryFrame[];
  /** Peer-lane frames (master only; absent on slaves / older responses). */
  beta?: TelemetryFrame[];
  delta?: TelemetryFrame[];
  events: Array<{ event: string }>;
}

export interface TelemetrySnapshot {
  status: PublicStatus;
  metrics: PublicMetrics;
  timeseries: TimeseriesResponse;
  demo: boolean;
  updatedAt: number;
}
