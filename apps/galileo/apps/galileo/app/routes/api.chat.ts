import { type ActionFunctionArgs } from '@remix-run/node';
import { MAX_TOKENS } from '~/lib/.server/llm/constants';
import { gatewayErrorResponse } from '~/lib/.server/llm/errors';
import { streamText, type Messages, type StreamingOptions } from '~/lib/.server/llm/stream-text';
import { authenticated } from '~/lib/.server/auth';

export async function action(args: ActionFunctionArgs) {
  return chatAction(args);
}

async function chatAction({ request }: ActionFunctionArgs) {
  const requestId = crypto.randomUUID();
  const started = Date.now();
  if (!(await authenticated(request))) return new Response('Unauthorized', { status: 401, headers: { 'X-Request-Id': requestId } });
  const { messages, mode } = (await request.json()) as { messages: Messages; mode?: 'chat' | 'build' };
  console.info(JSON.stringify({ event: 'chat.start', request_id: requestId, message_count: messages.length }));

  try {
    const options: StreamingOptions = {
      toolChoice: 'none',
      onFinish: async ({ finishReason }) => {
        if (finishReason === 'length') {
          console.log(`Reached max token limit (${MAX_TOKENS})`);
        }
      },
    };

    const result = await streamText(messages, mode, options);

    console.info(JSON.stringify({ event: 'chat.success', request_id: requestId, duration_ms: Date.now() - started }));
    return new Response(result.toAIStream(), {
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
