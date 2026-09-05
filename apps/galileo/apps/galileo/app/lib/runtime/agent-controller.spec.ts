import { afterEach, describe, expect, it, vi } from 'vitest';
import { runAgentTurn } from './agent-controller';
import { encodeGalileoEvent, type AgentEvent } from './galileo-stream';
import { executeReadOnlyTool } from './tool-executor';

vi.mock('./tool-executor', () => ({ executeReadOnlyTool: vi.fn() }));

const executeMock = vi.mocked(executeReadOnlyTool);
const encoder = new TextEncoder();
const userMessage = { id: 'u1', role: 'user' as const, content: 'inspect the project', createdAt: new Date() };

/** Serves one canned gateway turn of canonical Galileo SSE events per request. */
function stubTurns(turns: AgentEvent[][], capture?: { bodies: string[] }) {
  let index = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      capture?.bodies.push(String(init?.body));

      const events = turns[Math.min(index, turns.length - 1)];
      index += 1;

      const text = events.map((event) => encodeGalileoEvent(event)).join('');

      return new Response(encoder.encode(text), {
        status: 200,
        headers: { 'Content-Type': 'text/event-stream' },
      });
    }),
  );
}

async function collect(iterator: AsyncGenerator<AgentEvent>) {
  const events: AgentEvent[] = [];

  for await (const event of iterator) {
    events.push(event);
  }

  return events;
}

describe('agent controller tool loop', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it('returns without tool calls when the gateway answers with text only', async () => {
    stubTurns([
      [
        { type: 'response.start', response_id: 'r1' },
        { type: 'text.delta', delta: 'done' },
        { type: 'response.complete' },
      ],
    ]);
    executeMock.mockResolvedValue({ ok: true, result: 'unused' });

    const events = await collect(runAgentTurn([userMessage]));

    expect(events.map((event) => event.type)).toEqual(['response.start', 'text.delta', 'response.complete']);
    expect(executeMock).not.toHaveBeenCalled();
  });

  it('executes tool calls, streams results, and feeds them into the next request', async () => {
    const bodies: string[] = [];
    stubTurns(
      [
        [
          { type: 'tool.start', id: 'c1', name: 'list' },
          { type: 'tool.arguments', id: 'c1', arguments: { path: '.' } },
          { type: 'response.complete' },
        ],
        [{ type: 'text.delta', delta: 'index.html found' }, { type: 'response.complete' }],
      ],
      { bodies },
    );
    executeMock.mockResolvedValue({ ok: true, result: 'index.html' });

    const events = await collect(runAgentTurn([userMessage]));

    expect(events.map((event) => event.type)).toEqual([
      'tool.start',
      'tool.arguments',
      'response.complete',
      'tool.result',
      'text.delta',
      'response.complete',
    ]);

    const result = events.find((event) => event.type === 'tool.result');
    expect(result).toMatchObject({ type: 'tool.result', id: 'c1', ok: true, result: 'index.html' });
    expect(executeMock).toHaveBeenCalledWith('list', { path: '.' }, undefined);

    const followUp = JSON.parse(bodies[1]) as { messages: { role: string }[] };
    expect(followUp.messages.map((message) => message.role)).toEqual(['user', 'assistant', 'tool']);
  });

  it('stops after the same tool call repeats beyond its allowance', async () => {
    stubTurns([
      [
        { type: 'tool.start', id: 'c1', name: 'list' },
        { type: 'tool.arguments', id: 'c1', arguments: { path: '.' } },
        { type: 'response.complete' },
      ],
    ]);
    executeMock.mockResolvedValue({ ok: true, result: 'same' });

    const events = await collect(runAgentTurn([userMessage]));

    const error = events.find((event) => event.type === 'error');
    expect(error).toMatchObject({ type: 'error', code: 'repeated_tool_call' });
  });

  it('aborts the output cap when assistant text exceeds its limit', async () => {
    const flood = 'x'.repeat(20_001);
    stubTurns([[{ type: 'text.delta', delta: flood }, { type: 'response.complete' }]]);

    const events = await collect(runAgentTurn([userMessage]));

    expect(events.at(-1)).toMatchObject({ type: 'error', code: 'agent_output_limit' });
  });

  it('rejects before any request when the signal is already aborted', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const controller = new AbortController();
    controller.abort();

    await expect(collect(runAgentTurn([userMessage], { signal: controller.signal }))).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('propagates a mid-stream abort instead of yielding it as an event', async () => {
    stubTurns([[{ type: 'text.delta', delta: 'partial' }, { type: 'response.complete' }]]);

    const controller = new AbortController();

    const iterator = runAgentTurn([userMessage], { signal: controller.signal });
    const first = await iterator.next();
    expect(first.value.type).toBe('text.delta');
    controller.abort();

    await expect(iterator.next()).rejects.toMatchObject({ name: 'AbortError' });
  });
});
