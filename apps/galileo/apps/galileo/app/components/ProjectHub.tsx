import { useEffect, useState } from 'react';

type Project = { id: string; updated_at: string };

export function ProjectHub() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState('');
  const [error, setError] = useState('');

  const load = () => fetch('/api/projects?list=1', { credentials: 'include' }).then(async (response) => {
    if (!response.ok) throw new Error('Projects could not be loaded');
    setProjects((await response.json() as { projects: Project[] }).projects);
  }).catch((reason: Error) => setError(reason.message));

  useEffect(() => { void load(); }, []);

  const create = async () => {
    const response = await fetch('/api/projects', { method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'create', name }) });
    if (!response.ok) { setError('Project could not be created'); return; }
    const project = await response.json() as { project: string };
    window.location.href = `/chat/${project.project}`;
  };

  return <main className="mx-auto w-full max-w-4xl p-8 text-bolt-elements-textPrimary">
    <h1 className="text-3xl font-bold">Your Galileo projects</h1>
    <p className="mt-2 text-bolt-elements-textSecondary">Create a workspace or continue where you left off.</p>
    {error && <p className="mt-4 text-red-400">{error}</p>}
    <section className="mt-8 grid gap-3 sm:grid-cols-2">
      {projects.map((project) => <a key={project.id} href={`/chat/${project.id}`} className="rounded-lg border border-bolt-elements-borderColor p-5 hover:bg-bolt-elements-item-backgroundActive">
        <strong>{project.id}</strong><div className="mt-2 text-sm text-bolt-elements-textSecondary">Last updated {new Date(project.updated_at).toLocaleString()}</div>
      </a>)}
      {!projects.length && <p className="text-bolt-elements-textSecondary">No projects yet.</p>}
    </section>
    <section className="mt-10 flex gap-2">
      <input value={name} onChange={(event) => setName(event.target.value)} placeholder="New project name" className="flex-1 rounded border border-bolt-elements-borderColor bg-transparent px-3 py-2" />
      <button type="button" disabled={!name.trim()} onClick={() => void create()} className="rounded bg-bolt-elements-item-backgroundAccent px-4 py-2">Create workspace</button>
    </section>
    <p className="mt-8 text-sm text-bolt-elements-textSecondary">GitHub import will be enabled through the account connection before importing private repositories.</p>
  </main>;
}
