import type { AgentEvent } from '~/lib/runtime/galileo-stream';
import type { AgentPart } from '~/lib/runtime/agent-parts';
import { AgentPart as AgentPartView } from './AgentPart';

export function AgentActivity({ events }: { events: AgentEvent[] }) {
  const parts: AgentPart[] = [];

  for (const event of events) {
    if (event.type === 'tool.start') {
      parts.push({ type: 'tool-call', id: event.id, tool: event.name, args: {}, status: 'running', agent: 'Galileo' });
    } else if (event.type === 'tool.arguments') {
      const index = parts.findIndex((part) => part.type === 'tool-call' && part.id === event.id);
      if (index >= 0 && typeof event.arguments === 'object' && event.arguments !== null) {
        const part = parts[index] as Extract<AgentPart, { type: 'tool-call' }>;
        parts[index] = { ...part, args: event.arguments as Record<string, unknown>, status: 'complete' };
      }
    } else if (event.type === 'tool.result') {
      parts.push({
        type: 'tool-result',
        toolCallId: event.id,
        ...(event.ok ? { result: event.result } : { error: event.error?.message || 'Tool failed' }),
      });
    } else if (event.type === 'status') {
      parts.push({ type: 'status', state: event.state === 'inspecting' ? 'thinking' : event.state === 'executing' ? 'working' : event.state, text: event.message || event.state, agent: 'Galileo' });
    } else if (event.type === 'error') {
      parts.push({ type: 'status', state: 'verifying', text: event.message, agent: 'Verifier' });
    }
  }

  if (!parts.length) return null;
  return <div className="my-2" aria-label="Agent activity">{parts.map((part, index) => <AgentPartView key={`${part.type}-${index}`} part={part} />)}</div>;
}
