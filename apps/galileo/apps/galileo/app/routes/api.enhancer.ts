import { type ActionFunctionArgs } from '@remix-run/node';
import { gatewayErrorResponse } from '~/lib/.server/llm/errors';
import { completeText } from '~/lib/.server/llm/stream-text';
import { stripIndents } from '~/utils/stripIndent';
import { authenticated } from '~/lib/.server/auth';

export async function action(args: ActionFunctionArgs) {
  return enhancerAction(args);
}

async function enhancerAction({ request }: ActionFunctionArgs) {
  if (!(await authenticated(request))) return new Response('Unauthorized', { status: 401 });
  const { message } = (await request.json()) as { message: string };

  try {
    const enhanced = await completeText(
      [
        {
          role: 'user',
          content: stripIndents`
          I want you to improve the user prompt that is wrapped in \`<original_prompt>\` tags.

          IMPORTANT: Only respond with the improved prompt and nothing else!

          <original_prompt>
            ${message}
          </original_prompt>
        `,
        },
      ]);

    return new Response(enhanced, {
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    });
  } catch (error) {
    console.log(error);

    return gatewayErrorResponse(error);
  }
}
