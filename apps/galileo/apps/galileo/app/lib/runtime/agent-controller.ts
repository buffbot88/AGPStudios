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
  for (let turn = 0; turn < 5; turn += 1) {
    const response = await fetch(options.api || '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Galileo-Protocol': 'events' },
      body: JSON.stringify({ messages: conversation, mode: options.mode || 'chat', projectContext: options.projectContext || '' }),
      signal: options.signal,
    });
    if (!response.ok || !response.body) throw new Error(`Galileo returned HTTP ${response.status}`);

    let toolCall: Extract<AgentEvent, { type: 'tool.start' }> | undefined;
    let text = '';
    let toolArguments: Record<string, unknown> = {};
    for await (const event of decodeGalileoEvents(response.body)) {
      yield event;
      if (event.type === 'tool.start') toolCall = event;
      if (event.type === 'tool.arguments' && typeof event.arguments === 'object' && event.arguments !== null) toolArguments = event.arguments as Record<string, unknown>;
      if (event.type === 'text.delta') text += event.delta;
    }
    if (!toolCall) return;

    const result = await executeReadOnlyTool(toolCall.name, toolArguments);
    yield { type: 'tool.result', id: toolCall.id, ...result };
    conversation.push({ id: `${toolCall.id}-request`, role: 'assistant', content: text || JSON.stringify({ tool: { name: toolCall.name, ...toolArguments } }) });
    conversation.push({ id: `${toolCall.id}-result`, role: 'user', content: JSON.stringify({ tool_result: { tool_call_id: toolCall.id, ...result } }) });
  }
  yield { type: 'error', code: 'tool_loop_limit', message: 'Tool loop exceeded its limit', retryable: false };
}
