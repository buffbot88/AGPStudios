import { type ActionFunctionArgs } from '@remix-run/node';
import { gatewayErrorResponse } from '~/lib/.server/llm/errors';
import { streamText, type Messages, type ToolDefinition } from '~/lib/.server/llm/stream-text';
import { authenticated } from '~/lib/.server/auth';

export async function action(args: ActionFunctionArgs) {
  return chatAction(args);
}

async function chatAction({ request }: ActionFunctionArgs) {
  const requestId = crypto.randomUUID();
  const started = Date.now();
  if (!(await authenticated(request))) return new Response('Unauthorized', { status: 401, headers: { 'X-Request-Id': requestId } });
  const { messages, projectContext, tools } = (await request.json()) as { messages: Messages; projectContext?: string; tools?: ToolDefinition[] };
  console.info(JSON.stringify({ event: 'chat.start', request_id: requestId, message_count: messages.length, project_context_chars: projectContext?.length ?? 0 }));

  try {
    const response = await streamText(messages, projectContext, request.headers.get('cookie') || '', request.headers.get('x-galileo-protocol') === 'events', tools);
    console.info(JSON.stringify({ event: 'chat.success', request_id: requestId, duration_ms: Date.now() - started }));
    response.headers.set('X-Request-Id', requestId);
    return response;
  } catch (error) {
    const cause = error instanceof Error && error.cause instanceof Error ? error.cause.message : undefined;
    console.error(JSON.stringify({ event: 'chat.failure', request_id: requestId, duration_ms: Date.now() - started, error: error instanceof Error ? error.message : String(error), cause }));

    return gatewayErrorResponse(error, requestId);
  }
}
