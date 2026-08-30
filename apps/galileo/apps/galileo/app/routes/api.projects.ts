import { json, type ActionFunctionArgs, type LoaderFunctionArgs } from '@remix-run/node';
import { mkdir, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { authenticated } from '~/lib/.server/auth';

const ROOT = '/var/oled/data/users_projects';
const LIMIT = 200 * 1024 * 1024;
async function user(request: Request): Promise<{ username: string; githubLinked: boolean; csrf: string }> {
  const fallback = { username: '', githubLinked: false, csrf: '' }; 
  const response = await fetch('https://agpstudios.org/api/auth/session', { headers: { cookie: request.headers.get('cookie') || '' } });
  if (!response.ok) return fallback;
  const data = (await response.json()) as { authenticated?: boolean; user?: { username?: string }; github_linked?: boolean; csrf?: string };
  return data.authenticated && data.user?.username ? { username: data.user.username, githubLinked: data.github_linked === true, csrf: data.csrf || '' } : fallback;
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
  const account = await user(request);
  if (!account.username) return json({ error: 'unauthenticated' }, { status: 401 });
  const username = account.username;
  const params = new URL(request.url).searchParams;
  if (params.get('list') === '1') {
    const root = path.join(ROOT, safe(username));
    try {
      const entries = await readdir(root, { withFileTypes: true });
      const projects = await Promise.all(entries.filter((entry) => entry.isDirectory()).map(async (entry) => ({ id: entry.name, updated_at: (await stat(path.join(root, entry.name))).mtime.toISOString() })));
      return json({ projects: projects.sort((a, b) => b.updated_at.localeCompare(a.updated_at)) });
    } catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return json({ projects: [] }); throw error; }
  }
  const project = safe(params.get('project_id') || 'default');
  const directory = path.join(ROOT, safe(username), project);
  try { return json({ ok: true, project, files: await collect(directory) }); }
  catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return json({ ok: true, project, files: {} }); throw error; }
}

export async function action({ request }: ActionFunctionArgs) {
  if (!(await authenticated(request))) return json({ error: 'unauthenticated' }, { status: 401 });
  const account = await user(request);
  if (!account.username) return json({ error: 'unauthenticated' }, { status: 401 });
  const username = account.username;
  const body = (await request.json()) as { action?: string; name?: string; repository?: string; project_id?: string; files?: Record<string, string> };
  if (body.action === 'create' || body.action === 'import') {
    const repository = body.repository?.match(/^https:\/\/github\.com\/([a-zA-Z0-9_-]+)\/([a-zA-Z0-9_.-]+?)(?:\.git)?\/?$/);
    if (body.action === 'import' && !repository) return json({ error: 'github_url_required' }, { status: 400 });
    if (body.action === 'import') {
      const response = await fetch('https://agpstudios.org/api/github/app/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', cookie: request.headers.get('cookie') || '', 'x-csrf-token': account.csrf },
        body: JSON.stringify({ repository: body.repository, name: body.name || repository![2] }),
      });
      if (!response.ok) return json({ error: 'github_import_failed' }, { status: 502 });
      return json(await response.json());
    }
    const project = safe(body.name || repository?.[2] || 'new-project');
    const destination = path.join(ROOT, safe(username), project);
    await mkdir(path.dirname(destination), { recursive: true });
    await mkdir(destination, { recursive: true });
    await writeFile(path.join(destination, '.project-meta.json'), JSON.stringify({ name: body.name || repository?.[2] || project, created_at: new Date().toISOString() }));
    return json({ ok: true, project });
  }
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
