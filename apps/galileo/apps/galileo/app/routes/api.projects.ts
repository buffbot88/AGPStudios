import { json, type ActionFunctionArgs, type LoaderFunctionArgs } from '@remix-run/node';
import { mkdir, readdir, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { authenticated } from '~/lib/.server/auth';

const ROOT = '/var/oled/data/users_projects';
const LIMIT = 200 * 1024 * 1024;

async function user(request: Request): Promise<string | null> {
  const response = await fetch('https://agpstudios.org/api/auth/session', { headers: { cookie: request.headers.get('cookie') || '' } });
  if (!response.ok) return null;
  const data = (await response.json()) as { authenticated?: boolean; user?: { username?: string } };
  return data.authenticated && data.user?.username ? data.user.username : null;
}

function safe(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80) || 'default';
}

async function collect(directory: string, prefix = ''): Promise<Record<string, string>> {
  const files: Record<string, string> = {};
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name === '.project-meta.json') continue;
    const name = prefix ? `${prefix}/${entry.name}` : entry.name;
    const full = path.join(directory, entry.name);
    if (entry.isDirectory()) Object.assign(files, await collect(full, name));
    else files[name] = await readFile(full, 'utf8');
  }
  return files;
}

export async function loader({ request }: LoaderFunctionArgs) {
  if (!(await authenticated(request))) return json({ error: 'unauthenticated' }, { status: 401 });
  const username = await user(request);
  if (!username) return json({ error: 'unauthenticated' }, { status: 401 });
  const project = safe(new URL(request.url).searchParams.get('project_id') || 'default');
  const directory = path.join(ROOT, safe(username), project);
  try { return json({ ok: true, project, files: await collect(directory) }); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return json({ ok: true, project, files: {} }); throw error; }
}

export async function action({ request }: ActionFunctionArgs) {
  if (!(await authenticated(request))) return json({ error: 'unauthenticated' }, { status: 401 });
  const username = await user(request);
  if (!username) return json({ error: 'unauthenticated' }, { status: 401 });
  const body = (await request.json()) as { project_id?: string; files?: Record<string, string> };
  const project = safe(body.project_id || 'default');
  const files = body.files || {};
  let size = 0;
  for (const [name, content] of Object.entries(files)) {
    if (!name || name.includes('..') || name.startsWith('/') || typeof content !== 'string') return json({ error: 'invalid_project_files' }, { status: 400 });
    size += Buffer.byteLength(content);
    if (size > LIMIT) return json({ error: 'project_quota_exceeded', limit_bytes: LIMIT }, { status: 413 });
  }
  const destination = path.join(ROOT, safe(username), project);
  await rm(destination, { recursive: true, force: true });
  for (const [name, content] of Object.entries(files)) {
    const target = path.join(destination, name);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, content);
  }
  await writeFile(path.join(destination, '.project-meta.json'), JSON.stringify({ project, bytes: size, updated_at: new Date().toISOString() }));
  return json({ ok: true, project, bytes: size });
}
