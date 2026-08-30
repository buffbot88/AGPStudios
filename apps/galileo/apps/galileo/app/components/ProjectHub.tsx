import { useEffect, useState } from 'react';
import { ThemeSwitch } from '~/components/ui/ThemeSwitch';

type Project = { id: string; updated_at: string };

type GithubStatus = { connected?: boolean; username?: string };

export function ProjectHub() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState('');
  const [repository, setRepository] = useState('');
  const [error, setError] = useState('');
  const [github, setGithub] = useState<GithubStatus | null>(null);
  const [importing, setImporting] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = () => fetch('/api/projects?list=1', { credentials: 'include' }).then(async (response) => {
    if (!response.ok) throw new Error('Projects could not be loaded');
    setProjects((await response.json() as { projects: Project[] }).projects);
  }).catch((reason: Error) => setError(reason.message));

  useEffect(() => {
    void load().finally(() => setLoading(false));
    fetch('/api/account', { credentials: 'include' })
      .then((response) => response.ok ? response.json() : null)
      .then((value: { github_linked?: boolean; user?: { username?: string } } | null) => setGithub(value ? { connected: value.github_linked, username: value.user?.username } : null));
  }, []);

  const create = async (action: 'create' | 'import') => {
    setError('');
    setImporting(action === 'import');
    const response = action === 'create'
      ? await fetch('/api/galileo/projects', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) })
      : await fetch('/api/projects', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, name, repository }) });
    if (!response.ok) { setError(action === 'import' ? 'We could not import that repository.' : 'We could not create that project.'); setImporting(false); return; }
    const project = await response.json() as { project?: string; project_id?: string };
    window.location.href = `/chat/${project.project_id || project.project}`;
  };

  return <main className="flex-1 overflow-auto bg-bolt-elements-background-depth-1 px-6 py-10 text-bolt-elements-textPrimary sm:px-10">
    <div className="mx-auto w-full max-w-5xl">
      <div className="flex items-start justify-between gap-6">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.2em] text-accent">Galileo</p>
          <h1 className="mt-3 text-4xl font-semibold tracking-tight">Choose a project</h1>
          <p className="mt-3 max-w-xl text-base text-bolt-elements-textSecondary">Open a workspace and continue building, or start something new with Galileo.</p>
        </div>
        <ThemeSwitch className="shrink-0" />
      </div>

      {error && <p role="alert" className="mt-6 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-500">{error}</p>}
      <section className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {projects.map((project) => <a key={project.id} href={`/chat/${project.id}`} className="group rounded-xl border border-bolt-elements-borderColor bg-bolt-elements-background-depth-2 p-5 transition hover:-translate-y-0.5 hover:border-accent hover:shadow-lg">
          <div className="flex items-center justify-between"><strong className="text-lg group-hover:text-accent">{project.id}</strong><span className="i-ph:arrow-up-right text-lg text-bolt-elements-textTertiary" /></div>
          <div className="mt-8 text-sm text-bolt-elements-textSecondary">Updated {new Date(project.updated_at).toLocaleString()}</div>
        </a>)}
        {!loading && !projects.length && <div className="rounded-xl border border-dashed border-bolt-elements-borderColor p-6 text-bolt-elements-textSecondary sm:col-span-2 lg:col-span-3">No projects yet. Create your first workspace below.</div>}
      </section>

      <section className="mt-12 grid gap-5 lg:grid-cols-2">
        <div className="rounded-xl border border-bolt-elements-borderColor bg-bolt-elements-background-depth-2 p-6">
          <h2 className="text-xl font-semibold">Create manually</h2><p className="mt-1 text-sm text-bolt-elements-textSecondary">Start with an empty Galileo workspace.</p>
          <div className="mt-5 flex gap-3"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="Project name" aria-label="Project name" className="min-w-0 flex-1 rounded-lg border border-bolt-elements-borderColor bg-bolt-elements-background-depth-1 px-3 py-2.5 outline-none focus:border-accent" /><button type="button" disabled={!name.trim()} onClick={() => void create('create')} className="rounded-lg bg-accent px-4 py-2.5 font-medium text-white disabled:cursor-not-allowed disabled:opacity-40">Create</button></div>
        </div>
        <div className="rounded-xl border border-bolt-elements-borderColor bg-bolt-elements-background-depth-2 p-6">
          <h2 className="text-xl font-semibold">Import from GitHub</h2><p className="mt-1 text-sm text-bolt-elements-textSecondary">Connect your GitHub account to choose a repository securely.</p>
          <a href="/api/github/app/install" className="mt-5 inline-block rounded-lg border border-bolt-elements-borderColor px-4 py-2.5 font-medium hover:bg-bolt-elements-item-backgroundActive">{github?.connected ? `Reconnect ${github.username || 'GitHub'}` : 'Connect GitHub'}</a>
          <p className="mt-3 text-xs text-bolt-elements-textTertiary">The GitHub App receives repository access; Galileo never receives or stores GitHub credentials.</p>
        </div>
      </section>
    </div>
  </main>;
}
