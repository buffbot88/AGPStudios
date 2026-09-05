import { afterEach, describe, expect, it, vi } from 'vitest';
import { decodeGalileoEvents, encodeGalileoEvent } from '~/lib/runtime/galileo-stream';
import { streamText } from './stream-text';

const encoder = new TextEncoder();

/** Serves a fabricated gateway response whose body arrives in byte-fragmented slices. */
function stubGateway(fragments: string[], events: boolean) {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      const bytes = encoder.encode(fragments.join(''));

      for (let offset = 0; offset < bytes.length; offset += 7) {
        controller.enqueue(bytes.slice(offset, offset + 7));
      }
      controller.close();
    },
  });
  vi.stubGlobal(
    'fetch',
    vi.fn(
      async () =>
        new Response(stream, { status: 200, headers: { 'Content-Type': events ? 'text/event-stream' : 'text/plain' } }),
    ),
  );
}

async function collect(response: Response) {
  const body = response.body;

  if (!body) {
    throw new Error('stream-text returned an empty body');
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let text = '';

  for (;;) {
    const next = await reader.read();

    if (next.done) {
      break;
    }

    text += decoder.decode(next.value, { stream: true });
  }

  return text;
}

function records(text: string) {
  return text
    .split('\n\n')
    .map((record) => record.trimEnd())
    .filter(Boolean);
}

describe('gateway stream translation', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('delivers OpenAI deltas fragmented across chunk boundaries and terminates', async () => {
    stubGateway(
      [
        'data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
        'data: [DONE]\n\n',
      ],
      false,
    );

    const response = await streamText([{ role: 'user', content: 'hi' }], '', '', false);
    expect(await collect(response)).toBe('0:"He"\n0:"llo"\nd:{"finishReason":"stop"}\n');
  });

  it('passes canonical Galileo events through verbatim without synthesizing a duplicate completion', async () => {
    const canonical = [
      encodeGalileoEvent({ type: 'response.start', response_id: 'resp-1' }),
      encodeGalileoEvent({ type: 'text.delta', delta: 'building' }),
      encodeGalileoEvent({ type: 'tool.start', id: 'call_1', name: 'list' }),
      encodeGalileoEvent({ type: 'tool.arguments', id: 'call_1', arguments: { path: '.' } }),
      'event: junk\ndata: {{{\n\n',
      'event: x\ndata: [DONE]\n\n',
      encodeGalileoEvent({ type: 'response.complete' }),
    ];
    stubGateway(canonical, true);

    const response = await streamText([{ role: 'user', content: 'hi' }], '', '', true);
    expect(response.headers.get('Content-Type')).toContain('text/event-stream');

    const text = await collect(response);
    const expected = canonical
      .map((record) => record.trimEnd())
      .filter((record) => !record.includes('[DONE]') && !record.includes('{{{'));
    expect(records(text)).toEqual(expected);

    const events = [];

    for await (const event of decodeGalileoEvents(new Response(text).body!)) {
      events.push(event);
    }
    expect(events.filter((event) => event.type === 'response.complete')).toHaveLength(1);
  });

  it('translates OpenAI chunks from a gateway that ignores the events protocol', async () => {
    stubGateway(
      [
        'data: {"choices":[{"delta":{"content":"legacy "}}]}\n\n',
        'data: {"choices":[{"delta":{"content":"gateway"}}]}\n\n',
        'data: [DONE]\n\n',
      ],
      true,
    );

    const response = await streamText([{ role: 'user', content: 'hi' }], '', '', true);
    expect(response.headers.get('Content-Type')).toContain('text/event-stream');

    const events = [];

    for await (const event of decodeGalileoEvents(response.body!)) {
      events.push(event);
    }
    expect(events.map((event) => event.type)).toEqual([
      'response.start',
      'text.delta',
      'text.delta',
      'response.complete',
    ]);

    const deltas = events.filter((event) => event.type === 'text.delta');
    expect(deltas.map((event) => (event as { delta: string }).delta).join('')).toBe('legacy gateway');
  });

  it('synthesizes exactly one response.complete when the gateway closes without one', async () => {
    stubGateway(
      [
        encodeGalileoEvent({ type: 'response.start', response_id: 'resp-2' }),
        encodeGalileoEvent({ type: 'text.delta', delta: 'partial' }),
      ],
      true,
    );

    const response = await streamText([{ role: 'user', content: 'hi' }], '', '', true);
    const events = [];

    for await (const event of decodeGalileoEvents(response.body!)) {
      events.push(event);
    }
    expect(events.map((event) => event.type)).toEqual(['response.start', 'text.delta', 'response.complete']);
  });
});
