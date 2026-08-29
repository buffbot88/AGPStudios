import { useEffect, useState } from 'react';

type Status = {
  gateway?: { reachable: boolean; latency_ms: number };
  queue?: { queued_requests: number; active_requests: number; max_queue: number; available_text_slots: number; available_agent_slots: number; available_vision_slots: number; running_vision_instances?: number; max_vision_instances?: number } | null;
  workers?: { text_worker_healthy: boolean; vision_worker_active: boolean } | null;
  agents?: { id: string; healthy: boolean; latency_ms: number; error: string | null }[];
  updated_at?: string;
};

function Pip({ label, state, details }: { label: string; state: 'green' | 'orange' | 'red'; details: string[] }) {
  return <div className="relative group flex items-center gap-1.5 px-2 py-1 text-xs text-bolt-elements-textSecondary cursor-default">
    <span className={`h-2.5 w-2.5 rounded-full shadow-sm ${state === 'green' ? 'bg-green-500' : state === 'orange' ? 'bg-orange-500' : 'bg-red-500'}`} />
    <span>{label}</span>
    <div className="pointer-events-none absolute top-8 left-0 z-50 hidden group-hover:block w-64 rounded-md border border-bolt-elements-borderColor bg-bolt-elements-background-depth-2 p-3 text-xs text-bolt-elements-textSecondary shadow-xl">
      {details.map((detail) => <div key={detail}>{detail}</div>)}
    </div>
  </div>;
}

export function EcosystemNavbar() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/api/status', { credentials: 'include', cache: 'no-store' }).then((response) => response.json()).then((value: Status) => { if (active) setStatus(value); }).catch(() => { if (active) setStatus(null); });
    load();
    const timer = window.setInterval(load, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const gateway = status?.gateway;
  const queue = status?.queue;
  const workers = status?.workers;
  const agents = status?.agents ?? [];
  const running = queue?.running_vision_instances ?? (workers?.vision_worker_active ? 1 : 0);
  const maxVision = queue?.max_vision_instances ?? 3;
  const agentBusy = queue ? queue.available_agent_slots < agents.length : false;

  return <nav className="flex items-center gap-1 min-h-10 px-4 border-b border-bolt-elements-borderColor bg-bolt-elements-background-depth-2" aria-label="Ecosystem model status">
    <span className="mr-3 text-xs font-semibold tracking-wide text-bolt-elements-textPrimary">Ecosystem</span>
    <Pip label="Alpha 350M" state={!workers?.text_worker_healthy ? 'red' : queue?.available_text_slots === 0 ? 'orange' : 'green'} details={[`State: ${!workers?.text_worker_healthy ? 'offline' : queue?.available_text_slots === 0 ? 'busy' : 'idle'}`, `Active lanes: ${queue ? 1 - queue.available_text_slots : 0}/1`, `Queue: ${queue?.queued_requests ?? 'unknown'}`]} />
    <Pip label="VL 450M" state={!gateway?.reachable ? 'red' : queue?.available_vision_slots === 0 ? 'orange' : 'green'} details={[`State: ${queue?.available_vision_slots === 0 ? 'busy' : 'idle'}`, `Running lanes: ${running}/${maxVision}`, `Queue: ${queue?.queued_requests ?? 'unknown'}`]} />
    <Pip label="Agents" state={agents.length === 0 || agents.some((agent) => !agent.healthy) ? 'red' : agentBusy ? 'orange' : 'green'} details={[`Healthy lanes: ${agents.filter((agent) => agent.healthy).length}/${agents.length}`, `Active lanes: ${queue ? agents.length - queue.available_agent_slots : 'unknown'}`, `Queue: ${queue?.queued_requests ?? 'unknown'}`, ...agents.map((agent) => `${agent.id}: ${agent.healthy ? `${agent.latency_ms}ms` : 'offline'}`)]} />
    <span className="ml-auto text-[10px] text-bolt-elements-textTertiary">{status?.updated_at ? `updated ${new Date(status.updated_at).toLocaleTimeString()}` : 'status unavailable'}</span>
  </nav>;
}
