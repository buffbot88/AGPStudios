import { json } from '@remix-run/node';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { authenticated, getUser } from '~/lib/.server/auth';

/**
 * Manual project creation for the ProjectHub "Create manually" card.
 *
 * Mirrors api.projects.ts storage conventions: projects live under
 * /var/oled/data/users_projects/<username>/<project> with a .project-meta.json
 * sidecar. Kept as a separate route because the edge already serves
 * POST /api/projects for the GitHub import flow.
 */
const ROOT = '/var/oled/data/users_projects';

function safe(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80) || 'default';
}

export async function action({ request }: { request: Request }) {
  if (!(await authenticated(request))) return json({ error: 'unauthenticated' }, { status: 401 });

  const account = await getUser(request);
  if (!account) return json({ error: 'unauthenticated' }, { status: 401 });

  const body = (await request.json()) as { name?: string };
  const name = (body.name || '').trim();

  if (!name) return json({ error: 'project_name_required' }, { status: 400 });

  const project = safe(name);
  const destination = path.join(ROOT, safe(account.username), project);

  await mkdir(destination, { recursive: true });
  await writeFile(
    path.join(destination, '.project-meta.json'),
    JSON.stringify({ name, created_at: new Date().toISOString() }),
  );

  return json({ ok: true, project });
}
