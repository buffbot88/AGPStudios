import type { Message } from 'ai';
import { decodeGalileoEvents, type AgentEvent } from './galileo-stream';
import { executeReadOnlyTool } from './tool-executor';

interface AgentControllerOptions {
  api?: string;
  mode?: 'chat' | 'build';
  projectContext?: string;
  signal?: AbortSignal;
}

export async function* runAgentTurn(messages: Message[], options: AgentControllerOptions = {}): AsyncGenerator<AgentEvent> {
  const conversation = [...messages];
  const tools = [
    { type: 'function' as const, function: { name: 'list', parameters: { type: 'object', properties: { path: { type: 'string' } } } } },
    { type: 'function' as const, function: { name: 'read', parameters: { type: 'object', properties: { path: { type: 'string' } } } } },
    { type: 'function' as const, function: { name: 'search', parameters: { type: 'object', properties: { path: { type: 'string' }, query: { type: 'string' } } } } },
    { type: 'function' as const, function: { name: 'refresh_context', parameters: { type: 'object', properties: {} } } },
  ];
  for (let turn = 0; turn < 5; turn += 1) {
    const response = await fetch(options.api || '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Galileo-Protocol': 'events' },
      body: JSON.stringify({ messages: conversation, mode: options.mode || 'chat', projectContext: options.projectContext || '', tools }),
      signal: options.signal,
    });
    if (!response.ok || !response.body) throw new Error(`Galileo returned HTTP ${response.status}`);

    const calls = new Map<string, { name: string; arguments: Record<string, unknown> }>();
    for await (const event of decodeGalileoEvents(response.body)) {
      yield event;
      if (event.type === 'tool.start') calls.set(event.id, { name: event.name, arguments: {} });
      if (event.type === 'tool.arguments' && calls.has(event.id) && typeof event.arguments === 'object' && event.arguments !== null) calls.get(event.id)!.arguments = event.arguments as Record<string, unknown>;
    }
    if (!calls.size) return;

    for (const [id, call] of calls) {
      const result = await executeReadOnlyTool(call.name, call.arguments);
      yield {
        type: 'tool.result',
        id,
        ok: result.ok,
        ...(result.ok ? { result: result.result } : { error: { code: 'tool_failed', message: result.error || 'Tool failed' } }),
      };
      conversation.push({ id: `${id}-result`, role: 'tool', content: JSON.stringify({ tool_call_id: id, ...result }) });
    }
  }
  yield { type: 'error', code: 'tool_loop_limit', message: 'Tool loop exceeded its limit', retryable: false };
}
