import { describe, expect, it } from 'vitest';
import { decodeGalileoEvents, encodeGalileoEvent } from './galileo-stream';

const encoder = new TextEncoder();

describe('Galileo stream', () => {
  it('decodes fragmented SSE records', async () => {
    const event = encodeGalileoEvent({ type: 'text.delta', delta: 'hello' });
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        const bytes = new TextEncoder().encode(event);
        controller.enqueue(bytes.slice(0, 12));
        controller.enqueue(bytes.slice(12));
        controller.close();
      },
    });
    const events = [];

    for await (const value of decodeGalileoEvents(stream)) {
      events.push(value);
    }
    expect(events).toEqual([{ type: 'text.delta', delta: 'hello' }]);
  });

  it('ignores malformed records and preserves valid events', async () => {
    const input = new TextEncoder().encode('data: nope\n\nevent: x\ndata: {"type":"response.complete"}\n\n');
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(input);
        controller.close();
      },
    });
    const events = [];

    for await (const value of decodeGalileoEvents(stream)) {
      events.push(value);
    }
    expect(events).toEqual([{ type: 'response.complete' }]);
  });

  it('preserves full event payloads with unicode and structured fields', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          encoder.encode(encodeGalileoEvent({ type: 'tool.result', id: 'c9', ok: true, result: 'résult ✓ "quoted"' })),
        );
        controller.close();
      },
    });
    const events = [];

    for await (const value of decodeGalileoEvents(stream)) {
      events.push(value);
    }
    expect(events).toEqual([{ type: 'tool.result', id: 'c9', ok: true, result: 'résult ✓ "quoted"' }]);
  });

  it('stops cleanly when the stream ends mid-record without a closing blank line', async () => {
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('event: text.delta\ndata: {"type":"text.delta","delta":"tail"}\n'));
        controller.close();
      },
    });
    const events = [];

    for await (const value of decodeGalileoEvents(stream)) {
      events.push(value);
    }
    expect(events).toEqual([]);
  });
});
