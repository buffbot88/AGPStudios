import { streamText as _streamText, convertToCoreMessages } from 'ai';
import { getAPIKey, getGatewayURL } from '~/lib/.server/config';
import { getGatewayModel } from '~/lib/.server/llm/model';
import { GATEWAY_TIMEOUT_MS, MAX_TOKENS } from './constants';
import { getSystemPrompt } from './prompts';

interface ToolResult<Name extends string, Args, Result> {
  toolCallId: string;
  toolName: Name;
  args: Args;
  result: Result;
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  toolInvocations?: ToolResult<string, unknown, unknown>[];
}

export type Messages = Message[];

export type StreamingOptions = Omit<Parameters<typeof _streamText>[0], 'model'>;

export function streamText(messages: Messages, mode: 'chat' | 'build' = 'chat', options?: StreamingOptions) {
  return _streamText({
    model: getGatewayModel(getGatewayURL(), getAPIKey()),
    ...(mode === 'build' ? { system: getSystemPrompt() } : {}),
    maxTokens: MAX_TOKENS,
    abortSignal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS),
    messages: convertToCoreMessages(messages),
    headers: { 'x-ashat-mode': mode },
    ...options,
  });
}
