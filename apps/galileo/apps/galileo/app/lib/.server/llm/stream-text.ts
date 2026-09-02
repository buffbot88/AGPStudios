import { createHash } from 'node:crypto';
import { getAPIKey, getGatewayURL } from '~/lib/.server/config';
import { GATEWAY_TIMEOUT_MS, MAX_TOKENS } from './constants';
import { BUILD_READY_MARKER, CHAT_READINESS_PROMPT, getSystemPrompt } from './prompts';
import { encodeGalileoEvent } from '~/lib/runtime/galileo-stream';

interface Message {
  role: 'user' | 'assistant';
  content: unknown;
}

export type Messages = Message[];

export async function completeText(messages: Messages, mode: 'chat' | 'build' = 'chat', projectContext = '', sessionCookie = '') {
  const response = await streamText(messages, mode, projectContext, sessionCookie);
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
  return { text: text.replace(/(?:<!--\s*)?GALILEO_BUILD_READY(?:\s*-->)?/g, BUILD_READY_MARKER), usage: {} };
}

export async function streamText(messages: Messages, mode: 'chat' | 'build' = 'chat', projectContext = '', sessionCookie = '', events = false) {
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
        'x-ashat-mode': mode,
        'x-ashat-account': createHash('sha256').update(sessionCookie).digest('hex'),
      },
      body: JSON.stringify({
        model: 'ashat',
        messages: [{ role: 'system', content: `${mode === 'build' ? getSystemPrompt() : CHAT_READINESS_PROMPT}${projectContext ? `\n\n<project_context>\n${projectContext}\n</project_context>` : ''}` }, ...messages],
        max_tokens: MAX_TOKENS,
        temperature: 0,
        stream: true,
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const error = new Error(`Alpha returned HTTP ${response.status}`) as Error & { statusCode?: number };
      error.statusCode = response.status;
      throw error;
    }
    if (!response.body) throw new Error('Alpha returned an empty stream');

    const encoder = new TextEncoder();
    const decoder = new TextDecoder();
    const reader = response.body.getReader();
    const responseId = crypto.randomUUID();
    let started = false;
    let eventText = '';
    let buffer = '';
    const stream = new ReadableStream<Uint8Array>({
      async pull(streamController) {
        const next = await reader.read();
        if (next.done) {
          if (events && eventText.trimStart().startsWith('{')) {
            try {
              const request = JSON.parse(eventText) as { tool?: { name?: string; path?: string; query?: string } };
              const tool = request.tool;
              if (tool && typeof tool.name === 'string' && ['list', 'read', 'search', 'refresh_context'].includes(tool.name)) {
                const args = Object.fromEntries(Object.entries(tool).filter(([key, value]) => key !== 'name' && typeof value === 'string'));
                const toolCallId = crypto.randomUUID();
                streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'tool.start', id: toolCallId, name: tool.name })));
                streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'tool.arguments', id: toolCallId, arguments: args })));
              } else {
                streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'text.delta', delta: eventText })));
              }
            } catch {
              streamController.enqueue(encoder.encode(encodeGalileoEvent({ type: 'text.delta', delta: eventText })));
            }
          }
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
              if (events && (eventText || content.trimStart().startsWith('{'))) eventText += content;
              else streamController.enqueue(encoder.encode(events ? encodeGalileoEvent({ type: 'text.delta', delta: content }) : `0:${JSON.stringify(content)}\n`));
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
