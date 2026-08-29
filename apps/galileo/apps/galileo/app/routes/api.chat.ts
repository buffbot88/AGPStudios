import { type ActionFunctionArgs } from '@remix-run/node';
import { MAX_TOKENS } from '~/lib/.server/llm/constants';
import { gatewayErrorResponse } from '~/lib/.server/llm/errors';
import { streamText, type Messages, type StreamingOptions } from '~/lib/.server/llm/stream-text';
import { authenticated } from '~/lib/.server/auth';

export async function action(args: ActionFunctionArgs) {
  return chatAction(args);
}

async function chatAction({ request }: ActionFunctionArgs) {
  if (!(await authenticated(request))) return new Response('Unauthorized', { status: 401 });
  const { messages } = (await request.json()) as { messages: Messages };

  try {
    const options: StreamingOptions = {
      toolChoice: 'none',
      onFinish: async ({ finishReason }) => {
        if (finishReason === 'length') {
          console.log(`Reached max token limit (${MAX_TOKENS})`);
        }
      },
    };

    const result = await streamText(messages, options);

    return new Response(result.toAIStream(), {
      status: 200,
      headers: {
        'Content-Type': 'text/plain; charset=utf-8',
      },
    });
  } catch (error) {
    console.log(error);

    return gatewayErrorResponse(error);
  }
}
