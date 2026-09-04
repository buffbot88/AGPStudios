import { useEffect, useState } from 'react';

type Status = {
  ok: boolean;
  gateway?: { reachable: boolean; latency_ms: number };
  workers?: { vision_worker_active: boolean; text_worker_healthy: boolean };
  pool?: { available_agent_slots: number | null };
};

export function StatusStrip() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let active = true;
    const load = () => fetch('/api/status', { cache: 'no-store' }).then((response) => response.json()).then((value: Status) => {
      if (active) setStatus(value);
    }).catch(() => active && setStatus({ ok: false }));
    load();
    const timer = window.setInterval(load, 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const gateway = Boolean(status?.gateway?.reachable);
  const vision = Boolean(status?.workers?.vision_worker_active);
  const peers = status?.pool?.available_agent_slots;

  return (
    <div className="galileo-status-strip" aria-label="System status">
      <StatusPill label="Liquid" healthy={gateway} detail={status?.gateway?.latency_ms ? `${status.gateway.latency_ms} ms` : undefined} />
      <StatusPill label="Vision" healthy={vision} detail={vision ? 'active' : 'idle'} />
      <StatusPill label="Execution" healthy={status?.ok ?? false} detail={peers === null || peers === undefined ? undefined : `${peers} slots`} />
    </div>
  );
}

function StatusPill({ label, healthy, detail }: { label: string; healthy: boolean; detail?: string }) {
  return <span className={`galileo-status-pill ${healthy ? 'is-healthy' : 'is-idle'}`} title={detail}><i aria-hidden="true" />{label}{detail && <small>{detail}</small>}</span>;
}
