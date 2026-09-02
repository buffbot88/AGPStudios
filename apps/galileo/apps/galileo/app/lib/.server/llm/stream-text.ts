import { createHash } from 'node:crypto';
import { getAPIKey, getGatewayURL } from '~/lib/.server/config';
import { GATEWAY_TIMEOUT_MS, MAX_TOKENS } from './constants';
import { getSystemPrompt } from './prompts';
import { encodeGalileoEvent } from '~/lib/runtime/galileo-stream';

interface Message {
  role: 'user' | 'assistant';
  content: unknown;
}

export type ToolDefinition = { type: 'function'; function: { name: string; description?: string; parameters: Record<string, unknown> } };
export type Messages = Message[];

export async function completeText(messages: Messages, projectContext = '', sessionCookie = '') {
  const response = await streamText(messages, projectContext, sessionCookie);
  if (!response.body) throw new Error('Alpha returned an empty stream');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let text = '';
  for (;;) {
    const next = await reader.read();
    if (next.done) break;
    for (const line of decoder.decode(next.value, { stream: true }).split('\n')) {
      if (!line.startsWith('0:')) continue;
      try { text += JSON.parse(line.slice(2)) as string; } catch { /* Ignore incomplete data frames. */ }
    }
  }
  return { text, usage: {} };
}

export async function streamText(messages: Messages, projectContext = '', sessionCookie = '', events = false, tools: ToolDefinition[] = []) {
  const base = getGatewayURL().trim().replace(/\/+$/, '');
  const endpoint = `${base.endsWith('/v1') ? base : `${base}/v1`}/chat/completions`;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), GATEWAY_TIMEOUT_MS);

  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(getAPIKey() ? { Authorization: `Bearer ${getAPIKey()}` } : {}),
        'x-ashat-account': createHash('sha256').update(sessionCookie).digest('hex'),
        ...(events ? { 'X-Galileo-Protocol': 'events' } : {}),
      },
      body: JSON.stringify({
        model: 'ashat',
        messages: [{ role: 'system', content: `${getSystemPrompt()}${projectContext ? `\n\n<bootstrap_context>\n${projectContext.slice(0, 15000)}\n</bootstrap_context>` : ''}` }, ...messages],
        max_tokens: MAX_TOKENS,
        temperature: 0,
        stream: true,
        ...(tools.length ? { tools, tool_choice: 'auto' } : {}),
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const error = new Error(`Alpha returned HTTP ${response.status}`) as Error & { statusCode?: number };
      error.statusCode = response.status;
      throw error;
    }
    if (!response.body) throw new Error('Alpha returned an empty stream');
    if (events) return response;

    const encoder = new TextEncoder();
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    const responseId = crypto.randomUUID();
    let started = false;
    let buffer = '';
    const stream = new ReadableStream<Uint8Array>({
      async pull(streamController) {
        const next = await reader.read();
        if (next.done) {
          if (events) streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'response.complete' })));
          else streamController.enqueue(encoder.encode(`d:${JSON.stringify({ finishReason: 'stop' })}\n`));
          streamController.close();
          clearTimeout(timeout);
          return;
        }
        buffer += decoder.decode(next.value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data: ') || line === 'data: [DONE]') continue;
          try {
            const chunk = JSON.parse(line.slice(6)) as { choices?: { delta?: { content?: unknown } }[] };
            const content = chunk.choices?.[0]?.delta?.content;
            if (typeof content === 'string' && content) {
              if (!started && events) {
                started = true;
                streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'response.start', response_id: responseId })));
              }
              streamController.enqueue(encoder.encode(events ? encodeGalileoEvent({ type: 'text.delta', delta: content }) : `0:${JSON.stringify(content)}\n`));
            }
          } catch {
            // Ignore malformed or incomplete SSE records.
          }
        }
      },
      cancel() { void reader.cancel(); clearTimeout(timeout); },
    });
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Cache-Control': 'no-cache' } });
  } catch (error) {
    clearTimeout(timeout);
    throw error;
  }
}
