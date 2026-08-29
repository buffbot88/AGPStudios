import { getAPIKey, getGatewayURL } from '~/lib/.server/config';
import { GATEWAY_TIMEOUT_MS, MAX_TOKENS } from './constants';
import { BUILD_READY_MARKER, CHAT_READINESS_PROMPT, getSystemPrompt } from './prompts';

interface Message {
  role: 'user' | 'assistant';
  content: unknown;
}

export type Messages = Message[];

export async function completeText(messages: Messages, mode: 'chat' | 'build' = 'chat') {
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
      },
      body: JSON.stringify({
        model: 'ashat',
        messages: [{ role: 'system', content: mode === 'build' ? getSystemPrompt() : CHAT_READINESS_PROMPT }, ...messages],
        max_tokens: MAX_TOKENS,
        temperature: 0,
        stream: false,
      }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const error = new Error(`Alpha returned HTTP ${response.status}`) as Error & { statusCode?: number };
      error.statusCode = response.status;
      throw error;
    }

    const body = (await response.json()) as {
      choices?: { message?: { content?: unknown } }[];
      usage?: { prompt_tokens?: number; completion_tokens?: number };
    };
    const content = body.choices?.[0]?.message?.content;
    return {
      text: typeof content === 'string' ? content.replace(/(?:<!--\s*)?GALILEO_BUILD_READY(?:\s*-->)?/g, BUILD_READY_MARKER) : '',
      usage: {
        promptTokens: body.usage?.prompt_tokens ?? 0,
        completionTokens: body.usage?.completion_tokens ?? 0,
      },
    };
  } finally {
    clearTimeout(timeout);
  }
}
