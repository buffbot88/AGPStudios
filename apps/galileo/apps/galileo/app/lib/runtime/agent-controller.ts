import type { Message } from 'ai';
import { decodeGalileoEvents, type GalileoStreamEvent } from './galileo-stream';
import { executeReadOnlyTool } from './tool-executor';

interface AgentControllerOptions {
  api?: string;
  mode?: 'chat' | 'build';
  projectContext?: string;
  signal?: AbortSignal;
}

export async function* runAgentTurn(messages: Message[], options: AgentControllerOptions = {}): AsyncGenerator<GalileoStreamEvent> {
  const conversation = [...messages];
  for (let turn = 0; turn < 5; turn += 1) {
    const response = await fetch(options.api || '/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', 'X-Galileo-Protocol': 'events' },
      body: JSON.stringify({ messages: conversation, mode: options.mode || 'chat', projectContext: options.projectContext || '' }),
      signal: options.signal,
    });
    if (!response.ok || !response.body) throw new Error(`Galileo returned HTTP ${response.status}`);

    let toolCall: Extract<GalileoStreamEvent, { type: 'tool.start' }> | undefined;
    let text = '';
    for await (const event of decodeGalileoEvents(response.body)) {
      yield event;
      if (event.type === 'tool.start') toolCall = event;
      if (event.type === 'text.delta') text += event.delta;
    }
    if (!toolCall) return;

    const result = await executeReadOnlyTool(toolCall.name, toolCall.args || {});
    yield { type: 'tool.result', toolCallId: toolCall.toolCallId, ...result };
    conversation.push({ id: `${toolCall.toolCallId}-request`, role: 'assistant', content: text || JSON.stringify({ tool: { name: toolCall.name, ...(toolCall.args || {}) } }) });
    conversation.push({ id: `${toolCall.toolCallId}-result`, role: 'user', content: JSON.stringify({ tool_result: { tool_call_id: toolCall.toolCallId, ...result } }) });
  }
  yield { type: 'response.error', error: 'Tool loop exceeded its limit' };
}
