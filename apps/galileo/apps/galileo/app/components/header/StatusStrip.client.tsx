import { useStore } from '@nanostores/react';
import { useEffect, useState } from 'react';
import { liquidHealthy, latencyHistory, startStatusPolling, statusUpdatedAt, visionActive, visionReady, type LatencySample } from '~/lib/stores/system';

export function StatusStrip() {
  useEffect(() => startStatusPolling(), []);

  const liquid = useStore(liquidHealthy);
  const vision = useStore(visionReady);
  const visionRunning = useStore(visionActive);
  const updatedAt = useStore(statusUpdatedAt);
  const history = useStore(latencyHistory);

  const latest = history.length ? history[history.length - 1].ms : null;

  return (
    <div className="galileo-status-strip" aria-label="System status">
      <StatusPill healthy={liquid} label="Qwen" detail={latest !== null ? `${latest} ms` : undefined} />
      <StatusPill healthy={vision} label="Vision" detail={visionRunning ? 'active' : vision ? 'ready' : 'offline'} />
      <span className="galileo-status-spacer" />
      {updatedAt && <span>updated {updatedAt.toLocaleTimeString()}</span>}
      <Sparkline history={history} />
    </div>
  );
}

function StatusPill({ label, healthy, detail }: { label: string; healthy: boolean; detail?: string }) {
  return (
    <span className={`galileo-status-pill ${healthy ? 'is-healthy' : ''}`} title={detail}>
      <i aria-hidden="true" />
      {label}
      {detail && <small>{detail}</small>}
    </span>
  );
}

function Sparkline({ history }: { history: LatencySample[] }) {
  const [, force] = useState(0);

  // Redraw every 5s so an idle strip still feels live without spamming re-renders.
  useEffect(() => {
    const timer = window.setInterval(() => force((value) => value + 1), 5_000);
    return () => window.clearInterval(timer);
  }, []);

  if (history.length < 2) {
    return <span className="galileo-sparkline i-ph:waves text-lg" aria-hidden="true" />;
  }

  const width = 90;
  const height = 22;
  const samples = history.slice(-40);
  const max = Math.max(...samples.map((sample) => sample.ms), 1);

  const points = samples
    .map((sample, index) => {
      const x = (index / (samples.length - 1)) * width;
      const y = height - (sample.ms / max) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <svg aria-hidden="true" className="galileo-sparkline" height={height} width={width}>
      <polyline fill="none" points={points} stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
