import { describe, expect, it } from 'vitest';
import { decodeGalileoEvents, encodeGalileoEvent } from './galileo-stream';

describe('Galileo stream', () => {
  it('decodes fragmented SSE records', async () => {
    const event = encodeGalileoEvent({ type: 'text.delta', messageId: 'm1', delta: 'hello' });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const bytes = new TextEncoder().encode(event);
        controller.enqueue(bytes.slice(0, 12));
        controller.enqueue(bytes.slice(12));
        controller.close();
      },
    });
    const events = [];
    for await (const value of decodeGalileoEvents(stream)) events.push(value);
    expect(events).toEqual([{ type: 'text.delta', messageId: 'm1', delta: 'hello' }]);
  });

  it('ignores malformed records and preserves valid events', async () => {
    const input = new TextEncoder().encode('data: nope\n\nevent: x\ndata: {"type":"response.complete","messageId":"m1"}\n\n');
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(input); controller.close(); } });
    const events = [];
    for await (const value of decodeGalileoEvents(stream)) events.push(value);
    expect(events).toEqual([{ type: 'response.complete', messageId: 'm1' }]);
  });
});
