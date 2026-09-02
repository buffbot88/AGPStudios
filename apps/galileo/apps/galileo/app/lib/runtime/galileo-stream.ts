export type AgentMessage = {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: unknown;
};

export type AgentRequest = {
  conversation_id: string;
  project_id?: string;
  messages: AgentMessage[];
  operation?: 'chat' | 'agent' | 'vision';
  capabilities: {
    tools: boolean;
    vision: boolean;
  };
};

export type AgentEvent =
  | { type: 'response.start'; response_id: string }
  | { type: 'text.delta'; delta: string }
  | { type: 'tool.start'; id: string; name: string }
  | { type: 'tool.arguments'; id: string; arguments: unknown }
  | { type: 'tool.result'; id: string; ok: boolean; result?: unknown; error?: string }
  | { type: 'status'; state: 'inspecting' | 'working' | 'executing' | 'verifying'; message?: string }
  | { type: 'error'; code: string; message: string; retryable: boolean }
  | { type: 'response.complete' };

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function encodeGalileoEvent(event: AgentEvent) {
  return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

export async function* decodeGalileoEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<AgentEvent> {
  const reader = stream.getReader();
  let buffer = '';
  try {
    for (;;) {
      const next = await reader.read();
      buffer += decoder.decode(next.value, { stream: !next.done });
      const records = buffer.split('\n\n');
      buffer = records.pop() || '';
      for (const record of records) {
        const data = record.split('\n').find((line) => line.startsWith('data:'))?.slice(5).trim();
        if (!data) continue;
        try { yield JSON.parse(data) as AgentEvent; } catch { /* Ignore malformed SSE records. */ }
      }
      if (next.done) break;
    }
  } finally {
    reader.releaseLock();
  }
}

export function eventStream(events: AsyncIterable<AgentEvent>) {
  const iterator = events[Symbol.asyncIterator]();
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      const next = await iterator.next();
      if (next.done) return controller.close();
      controller.enqueue(encoder.encode(encodeGalileoEvent(next.value)));
    },
    async cancel() { await iterator.return?.(); },
  });
}
