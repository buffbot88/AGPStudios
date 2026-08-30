import { json, type ActionFunctionArgs, type LoaderFunctionArgs } from '@remix-run/node';
import { cp, mkdtemp, mkdir, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import path from 'node:path';
import { authenticated } from '~/lib/.server/auth';

const ROOT = '/var/oled/data/users_projects';
const LIMIT = 200 * 1024 * 1024;
const exec = promisify(execFile);

async function user(request: Request): Promise<{ username: string; githubLinked: boolean }> {
  const fallback = { username: '', githubLinked: false };
  const response = await fetch('https://agpstudios.org/api/auth/session', { headers: { cookie: request.headers.get('cookie') || '' } });
  if (!response.ok) return fallback;
  const data = (await response.json()) as { authenticated?: boolean; user?: { username?: string }; github_linked?: boolean };
  return data.authenticated && data.user?.username ? { username: data.user.username, githubLinked: data.github_linked === true } : fallback;
}

function safe(value: string) {
  return value.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80) || 'default';
}

async function githubAccessToken(request: Request): Promise<string> {
  const response = await fetch('https://agpstudios.org/api/account/github/token', {
    headers: { cookie: request.headers.get('cookie') || '' },
  });
  if (!response.ok) throw new Error('github_token_unavailable');
  const data = (await response.json()) as { access_token?: string };
  if (!data.access_token) throw new Error('github_token_unavailable');
  return data.access_token;
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
    if (body.action === 'import') {
      if (!repository) return json({ error: 'github_url_required' }, { status: 400 });
      if (!account.githubLinked) return json({ error: 'github_account_not_linked' }, { status: 403 });
    }
    const project = safe(body.name || repository?.[2] || 'new-project');
    const destination = path.join(ROOT, safe(username), project);
    await mkdir(path.dirname(destination), { recursive: true });
    if (body.action === 'import') {
      const temporary = await mkdtemp(path.join('/tmp', 'galileo-github-'));
      try {
        const githubToken = await githubAccessToken(request);
        const authenticatedUrl = `https://x-access-token:${encodeURIComponent(githubToken)}@github.com/${repository![1]}/${repository![2]}.git`;
        await exec('git', ['clone', '--depth', '1', authenticatedUrl, temporary], { timeout: 120_000 });
        await cp(temporary, destination, { recursive: true, filter: (source) => !source.endsWith(`${path.sep}.git`) });
      } catch (error) {
        if (error instanceof Error && error.message === 'github_token_unavailable') return json({ error: 'github_token_unavailable' }, { status: 502 });
        throw error;
      } finally { await rm(temporary, { recursive: true, force: true }); }
    } else await mkdir(destination, { recursive: true });
    await writeFile(path.join(destination, '.project-meta.json'), JSON.stringify({ name: body.name || repository?.[2] || project, source: body.action === 'import' ? body.repository : undefined, created_at: new Date().toISOString() }));
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
