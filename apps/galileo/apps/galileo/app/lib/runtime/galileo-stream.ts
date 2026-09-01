export type GalileoStreamEvent =
  | { type: 'response.start'; messageId: string }
  | { type: 'text.delta'; messageId: string; delta: string }
  | { type: 'tool.start'; toolCallId: string; name: string; args?: Record<string, unknown> }
  | { type: 'tool.result'; toolCallId: string; ok: boolean; result?: unknown; error?: string }
  | { type: 'response.complete'; messageId: string }
  | { type: 'response.error'; messageId?: string; error: string };

const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function encodeGalileoEvent(event: GalileoStreamEvent) {
  return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

export async function* decodeGalileoEvents(stream: ReadableStream<Uint8Array>): AsyncGenerator<GalileoStreamEvent> {
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
        try { yield JSON.parse(data) as GalileoStreamEvent; } catch { /* Ignore malformed SSE records. */ }
      }
      if (next.done) break;
    }
  } finally {
    reader.releaseLock();
  }
}

export function eventStream(events: AsyncIterable<GalileoStreamEvent>) {
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
