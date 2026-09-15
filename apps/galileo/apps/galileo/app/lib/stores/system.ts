import { computed, atom, map } from 'nanostores';
import { useStore } from '@nanostores/react';

export type LatencySample = { t: number; ms: number };

export type SystemStatus = {
  ok: boolean;
  gateway?: { reachable: boolean; latency_ms: number; version?: string | null; error?: string | null };
  queue?: { queued_requests: number; max_queue: number; active_requests: number; available_vision_slots?: number } | null;
  workers?: { liquid_backend_healthy?: boolean; liquid_backend_configured?: boolean; vision_worker_active: boolean } | null;
  pool?: { available_liquid_slots: number | null };
  updated_at?: string;
};

export const systemStatus = atom<SystemStatus | null>(null);

export const statusError = atom<string | null>(null);

export const statusUpdatedAt = atom<Date | null>(null);

export const latencyHistory = atom<LatencySample[]>([]);

export const statusLoading = map({ active: false });

const POLL_MS = 15_000;
const HISTORY_MAX = 60;

export function startStatusPolling() {
  if (statusLoading.get().active) {
    return () => undefined;
  }

  statusLoading.setKey('active', true);
  const timer = window.setInterval(() => void load(), POLL_MS);
  const load = () => {
    fetch('/api/status', { cache: 'no-store' })
      .then((response) => (response.ok ? (response.json() as Promise<SystemStatus>) : Promise.reject(new Error(`HTTP ${response.status}`))))
      .then((value) => {
        systemStatus.set(value);
        statusError.set(null);
        statusUpdatedAt.set(new Date());

        const ms = value.gateway?.latency_ms;

        if (typeof ms === 'number') {
          const next = [...latencyHistory.get(), { t: Date.now(), ms }].slice(-HISTORY_MAX);
          latencyHistory.set(next);
        }
      })
      .catch((error: unknown) => {
        statusError.set(error instanceof Error ? error.message : String(error));
      });
  };

  void load();

  return () => {
    window.clearInterval(timer);
    statusLoading.setKey('active', false);
  };
}

export const liquidHealthy = computed(systemStatus, (status) => Boolean(status?.gateway?.reachable && (status?.workers?.liquid_backend_healthy ?? status?.workers?.liquid_backend_configured)));

export const visionActive = computed(systemStatus, (status) => Boolean(status?.workers?.vision_worker_active));

export const visionReady = computed(systemStatus, (status) => Boolean(status?.workers?.vision_worker_active || (status?.queue?.available_vision_slots ?? 0) > 0));

export const queuePct = computed(systemStatus, (status) => {
  const queue = status?.queue;
  if (!queue || !queue.max_queue) {
    return null;
  }
  return Math.min(100, Math.round((queue.queued_requests / queue.max_queue) * 100));
});

export const availableSlots = computed(systemStatus, (status) => status?.pool?.available_liquid_slots ?? null);

export function useSystemStatus() {
  return useStore(systemStatus);
}
