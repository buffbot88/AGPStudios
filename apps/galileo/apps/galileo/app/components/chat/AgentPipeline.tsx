import { useMemo } from 'react';
import type { AgentEvent } from '~/lib/runtime/galileo-stream';

export type PipelineEntry = { event: AgentEvent; at: Date };

type Step = { key: string; name: string; label: string; detail: string; status: 'running' | 'complete' | 'failed' };

function stepLabel(name: string, args: Record<string, unknown>) {
  if (name === 'read' && typeof args.path === 'string') return `Read ${args.path}`;
  if (name === 'list' && typeof args.path === 'string') return `Inspect ${args.path === '.' || args.path === '' ? 'project' : args.path}`;
  if (name === 'search' && typeof args.query === 'string') return `Search ${JSON.stringify(args.query)}`;
  if (name === 'refresh_context') return 'Refresh context';
  return name.replace(/_/g, ' ');
}

function icon(name: string) {
  if (name === 'read') return 'i-ph:file-text';
  if (name === 'list') return 'i-ph:folder-open';
  if (name === 'search') return 'i-ph:magnifying-glass';
  return 'i-ph:wrench';
}

export function AgentPipeline({ events, now = new Date() }: { events: AgentEvent[]; now?: Date }) {
  const steps = useMemo(() => buildSteps(events), [events]);
  if (!steps.length) return null;

  return <div className="galileo-panel galileo-pipeline" aria-label="Agent activity">
    <div className="galileo-pipeline-head"><span className="i-ph:sparkle-fill text-accent-500" /> Galileo Agent <span className="galileo-pipeline-time">· {now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</span></div>
    <div className="galileo-pipeline-steps">
      {steps.map((step) => <details key={step.key} className={`galileo-tool-surface is-${step.status}`} open={step.status === 'running'}>
        <summary><span className={step.status === 'running' ? 'i-svg-spinners:90-ring-with-bg' : step.status === 'complete' ? 'i-ph:check' : 'i-ph:x'} /><span className={`i-ph:${icon(step.name).slice(5)}`} /><strong>{step.label}</strong><span className="galileo-tool-status">{step.status === 'running' ? 'Running' : step.status === 'complete' ? 'Completed' : 'Failed'}</span></summary>
        {step.detail && <pre>{step.detail}</pre>}
      </details>)}
    </div>
  </div>;
}

function buildSteps(events: AgentEvent[]): Step[] {
  const calls = new Map<string, { name: string; label: string; args: string }>();
  const steps: Step[] = [];
  for (const event of events) {
    if (event.type === 'tool.start') calls.set(event.id, { name: event.name, label: event.name.replace(/_/g, ' '), args: '' });
    else if (event.type === 'tool.arguments') {
      const call = calls.get(event.id);
      if (call && typeof event.arguments === 'object' && event.arguments !== null) {
        const args = event.arguments as Record<string, unknown>;
        call.label = stepLabel(call.name, args);
        call.args = JSON.stringify(args, null, 2);
      }
    } else if (event.type === 'tool.result') {
      const call = calls.get(event.id);
      if (call) {
        calls.delete(event.id);
        steps.push({ key: `done-${event.id}`, name: call.name, label: call.label, detail: event.ok ? call.args : `${call.args}\n\n${event.error?.message || 'Tool failed'}`, status: event.ok ? 'complete' : 'failed' });
      }
    }
  }
  for (const [id, call] of calls) steps.push({ key: `running-${id}`, name: call.name, label: call.label, detail: call.args, status: 'running' });
  return steps;
}
