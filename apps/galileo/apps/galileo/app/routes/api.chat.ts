import { type ActionFunctionArgs } from '@remix-run/node';
import { gatewayErrorResponse } from '~/lib/.server/llm/errors';
import { completeText, type Messages } from '~/lib/.server/llm/stream-text';
import { authenticated } from '~/lib/.server/auth';
import { BUILD_READY_MARKER } from '~/lib/.server/llm/prompts';

export async function action(args: ActionFunctionArgs) {
  return chatAction(args);
}

async function chatAction({ request }: ActionFunctionArgs) {
  const requestId = crypto.randomUUID();
  const started = Date.now();
  if (!(await authenticated(request))) return new Response('Unauthorized', { status: 401, headers: { 'X-Request-Id': requestId } });
  const { messages, mode } = (await request.json()) as { messages: Messages; mode?: 'chat' | 'build' };
  console.info(JSON.stringify({ event: 'chat.start', request_id: requestId, mode: mode ?? 'chat', message_count: messages.length }));

  if (mode === 'build' && !messages.some((message) => message.role === 'assistant' && typeof message.content === 'string' && message.content.includes(BUILD_READY_MARKER))) {
    return new Response('Galileo needs more context before Build mode is available.', { status: 409, headers: { 'X-Request-Id': requestId } });
  }

  try {
    const text = await completeText(messages, mode);
    console.info(JSON.stringify({ event: 'chat.success', request_id: requestId, duration_ms: Date.now() - started }));
    return new Response(`0:${JSON.stringify(text)}\nd:${JSON.stringify({ finishReason: 'stop' })}\n`, {
      status: 200,
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
        'X-Request-Id': requestId,
      },
    });
  } catch (error) {
    const cause = error instanceof Error && error.cause instanceof Error ? error.cause.message : undefined;
    console.error(JSON.stringify({ event: 'chat.failure', request_id: requestId, duration_ms: Date.now() - started, error: error instanceof Error ? error.message : String(error), cause }));

    return gatewayErrorResponse(error, requestId, mode);
  }
}
