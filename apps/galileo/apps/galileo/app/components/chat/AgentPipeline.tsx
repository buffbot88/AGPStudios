import { useMemo } from 'react';
import type { AgentEvent } from '~/lib/runtime/galileo-stream';

export type PipelineEntry = { event: AgentEvent; at: Date };

function stepLabel(name: string, args: Record<string, unknown>) {
  if (name === 'read' && typeof args.path === 'string') return `Read ${args.path}`;
  if (name === 'list' && typeof args.path === 'string') {
    return `Inspected ${args.path === '.' || args.path === '' ? 'project' : args.path}`;
  }
  if (name === 'search' && typeof args.query === 'string') return `Searched ${JSON.stringify(args.query)}`;
  if (name === 'refresh_context') return 'Refreshed context';

  return name.replace(/_/g, ' ');
}

function stepIcon(name: string) {
  if (name === 'read') return 'i-ph:code-duotone';
  if (name === 'list') return 'i-ph:folder-duotone';
  if (name === 'search') return 'i-ph:magnifying-glass-duotone';
  return 'i-ph:arrows-counter-clockwise-duotone';
}

/**
 * Horizontal pipeline of the current agent turn: completed tool steps render
 * as settled chips, the running step as the glowing tail, matching the
 * "Galileo Agent" activity card.
 */
export function AgentPipeline({ events, now = new Date() }: { events: AgentEvent[]; now?: Date }) {
  const steps = useMemo(() => buildSteps(events), [events]);

  if (!steps.length) {
    return null;
  }

  const time = now.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });

  return (
    <div className="galileo-panel galileo-pipeline" aria-label="Agent activity">
      <div className="galileo-pipeline-head">
        <span className="i-ph:sparkle-fill text-accent-500" />
        Galileo Agent
        <span className="galileo-pipeline-time">· {time}</span>
      </div>
      <div className="galileo-pipeline-steps">
        {steps.map((step, index) => (
          <span key={step.key} className="contents">
            {index > 0 && <span className="galileo-pipeline-link" />}
            <span className={`galileo-pipeline-step ${step.running ? 'is-running' : ''}`} title={step.detail}>
              {step.running ? (
                <span className="i-svg-spinners:90-ring-with-bg text-lg" />
              ) : (
                <span className={`${stepIcon(step.name)} text-lg text-bolt-elements-textTertiary`} />
              )}
              <span className="flex flex-col">
                <span className="galileo-pipeline-step-label">{step.label}</span>
                <span className="galileo-pipeline-step-time">{time}</span>
              </span>
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

type Step = { key: string; name: string; label: string; detail: string; running: boolean };

function buildSteps(events: AgentEvent[]): Step[] {
  const calls = new Map<string, { name: string; label: string; detail: string }>();
  const steps: Step[] = [];

  for (const event of events) {
    if (event.type === 'tool.start') {
      calls.set(event.id, { name: event.name, label: event.name, detail: event.name });
    } else if (event.type === 'tool.arguments') {
      const call = calls.get(event.id);

      if (call && typeof event.arguments === 'object' && event.arguments !== null) {
        const args = event.arguments as Record<string, unknown>;
        call.label = stepLabel(call.name, args);
        call.detail = JSON.stringify(args);
      }
    } else if (event.type === 'tool.result') {
      const call = calls.get(event.id);

      if (call) {
        calls.delete(event.id);
        steps.push({ key: `done-${event.id}`, name: call.name, label: call.label, detail: call.detail, running: false });
      }
    } else if (event.type === 'status') {
      const message = event.message || event.state;
      const existing = steps.find((step) => step.key === `status-${message}`);

      if (!existing) {
        steps.push({ key: `status-${message}`, name: 'status', label: message, detail: message, running: true });
      }
    }
  }

  for (const [id, call] of calls) {
    steps.push({ key: `running-${id}`, name: call.name, label: call.label, detail: call.detail, running: true });
  }

  return steps;
}
