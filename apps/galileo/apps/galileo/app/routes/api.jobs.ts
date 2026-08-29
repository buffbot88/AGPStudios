import { json, type ActionFunctionArgs } from '@remix-run/node';
import { authenticated } from '~/lib/.server/auth';

const RUST_ORIGIN = 'https://agpstudios.org';

async function rust(request: Request, path: string, body: unknown) {
  return fetch(`${RUST_ORIGIN}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', cookie: request.headers.get('cookie') || '' },
    body: JSON.stringify(body),
  });
}

export async function action({ request }: ActionFunctionArgs) {
  if (!(await authenticated(request))) return json({ error: 'unauthenticated' }, { status: 401 });
  const input = (await request.json()) as { project_id?: string; request?: string; files?: Record<string, string> };
  const projectId = input.project_id || 'default';
  const prompt = input.request?.trim();
  if (!prompt || prompt.length > 50_000 || !input.files || typeof input.files !== 'object') {
    return json({ error: 'invalid_job' }, { status: 400 });
  }

  const snapshot = await rust(request, `/api/galileo/projects/${encodeURIComponent(projectId)}/files/snapshot`, { files: input.files });
  if (!snapshot.ok) return new Response(await snapshot.text(), { status: snapshot.status, headers: { 'Content-Type': 'application/json' } });

  const discovery = await rust(request, '/api/galileo/discovery', { project_id: projectId, message: prompt });
  const discoveryBody = await discovery.json() as { plan_id?: string; kind?: string; content?: string };
  if (!discovery.ok || discoveryBody.kind !== 'plan' || !discoveryBody.plan_id) {
    return json({ error: 'plan_not_ready', kind: discoveryBody.kind, content: discoveryBody.content }, { status: 409 });
  }

  const job = await rust(request, '/api/galileo/agents/jobs', { project_id: projectId, request: prompt, plan_id: discoveryBody.plan_id });
  return new Response(await job.text(), { status: job.status, headers: { 'Content-Type': 'application/json' } });
}
