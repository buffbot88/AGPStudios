import { useStore } from '@nanostores/react';
import { useEffect, useMemo, useState } from 'react';
import { chatId } from '~/lib/persistence';
import type { FileMap } from '~/lib/stores/files';
import { workbenchStore } from '~/lib/stores/workbench';
import { railOpen, railTab, type RailTab } from '~/lib/stores/ui';

type RailSection = 'project' | 'agent' | 'files' | 'session';

const TOOL_CHIPS = [
  { icon: 'i-ph:folder-open-duotone', label: 'list' },
  { icon: 'i-ph:file-code-duotone', label: 'read' },
  { icon: 'i-ph:magnifying-glass-duotone', label: 'search' },
  { icon: 'i-ph:arrows-counter-clockwise-duotone', label: 'refresh_context' },
];

export function InfoRail({ agentActive, messages }: { agentActive: boolean; messages: Array<{ role: string; content: string; createdAt?: Date | number }> }) {
  const open = useStore(railOpen);
  const tab = useStore(railTab);

  if (!open) {
    return null;
  }

  return (
    <aside aria-label="Project info" className="galileo-rail">
      <RailSection
        icon="i-ph:squares-four-duotone"
        label="PROJECT"
        open={tab === 'project'}
        onToggle={() => railTab.set(tab === 'project' ? 'agent' : 'project')}
        sectionKey="project"
      >
        <ProjectSection messages={messages} />
      </RailSection>
      <RailSection
        icon="i-ph:sparkle-duotone"
        label="AGENT"
        open={tab === 'agent'}
        onToggle={() => railTab.set(tab === 'agent' ? 'files' : 'agent')}
        sectionKey="agent"
      >
        <AgentSection active={agentActive} />
      </RailSection>
      <RailSection
        icon="i-ph:folder-duotone"
        label="FILES"
        open={tab === 'files'}
        onToggle={() => railTab.set(tab === 'files' ? 'session' : 'files')}
        sectionKey="files"
      >
        <FilesSection />
      </RailSection>
      <RailSection
        icon="i-ph:clock-counter-clockwise-duotone"
        label="SESSION"
        open={tab === 'session'}
        onToggle={() => railTab.set(tab === 'session' ? 'project' : 'session')}
        sectionKey="session"
      >
        <SessionSection messages={messages} />
      </RailSection>
    </aside>
  );
}

function RailSection({
  icon,
  label,
  open,
  onToggle,
  children,
  sectionKey,
}: {
  icon: string;
  label: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
  sectionKey: RailSection;
}) {
  return (
    <section className="galileo-rail-section" data-section={sectionKey}>
      <button className="galileo-rail-head" onClick={onToggle} type="button">
        <span className={`${icon} text-base`} />
        {label}
        <span className={`galileo-rail-caret i-ph:caret-up ${open ? '' : 'rotate-180'}`} />
      </button>
      {open && children}
    </section>
  );
}

function ProjectSection({ messages }: { messages: Array<{ role: string; content: string; createdAt?: Date | number }> }) {
  const id = useStore(chatId);
  const files = useStore(workbenchStore.files);
  const [copied, setCopied] = useState(false);

  const stack = useMemo(() => detectStack(files), [files]);
  const firstUser = messages.find((message) => message.role === 'user');
  const summary = firstUser ? summarize(firstUser.content) : 'Describe what to build and Galileo will inspect, change, and deploy.';

  const copyId = () => {
    if (!id) return;
    navigator.clipboard.writeText(id).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_500);
    }).catch(() => setCopied(false));
  };

  return (
    <div>
      <div className="galileo-rail-title">{id ? `${id}` : 'New project'}</div>
      <div className="mt-0.5 flex items-center gap-2">
        <span className="galileo-rail-id">/home/project</span>
        <button aria-label="Copy project id" className="galileo-msg-action h-6 w-6" onClick={copyId} title="Copy project id" type="button">
          <span className={copied ? 'i-ph:check text-xs' : 'i-ph:copy text-xs'} />
        </button>
      </div>
      <p className="galileo-rail-muted mt-2">{summary}</p>
      {stack.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {stack.map((chip) => (
            <span key={chip.label} className={`galileo-chip ${chip.tone ? `is-${chip.tone}` : ''}`}>
              {chip.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function AgentSection({ active }: { active: boolean }) {
  return (
    <div>
      <div className="flex items-center gap-2">
        <span className="i-ph:sparkle-fill text-accent-500 text-base" />
        <span className="galileo-rail-title">Galileo Agent</span>
        <span className={`galileo-badge ml-auto ${active ? '' : 'is-idle'}`}>
          <i />
          {active ? 'Active' : 'Idle'}
        </span>
      </div>
      <p className="galileo-rail-muted mt-2">Coding agent with project awareness and read-only tool access.</p>
      <div className="mt-3 text-xs text-bolt-elements-textTertiary">Capabilities</div>
      <div className="mt-1.5 flex flex-wrap gap-1.5">
        {TOOL_CHIPS.map((tool) => (
          <span key={tool.label} className="galileo-chip" title={tool.label}>
            <span className={`${tool.icon} text-sm`} />
          </span>
        ))}
      </div>
    </div>
  );
}

function FilesSection() {
  const files = useStore(workbenchStore.files);

  const list = useMemo(
    () =>
      Object.entries(files)
        .filter(([path, entry]) => entry?.type === 'file' && !path.includes('/.'))
        .map(([path]) => path)
        .sort((a, b) => a.split('/').length - b.split('/').length || a.localeCompare(b))
        .slice(0, 30),
    [files],
  );
  const total = useMemo(() => Object.values(files).filter((entry) => entry?.type === 'file').length, [files]);

  const openFile = (path: string) => {
    workbenchStore.setSelectedFile(path);
    workbenchStore.showWorkbench.set(true);
  };

  return (
    <div>
      {list.length === 0 && <div className="galileo-rail-muted">Workspace is empty — start a chat to generate files.</div>}
      {list.map((path) => {
        const name = path.split('/').pop() || path;
        const ext = name.includes('.') ? name.split('.').pop()!.slice(0, 3).toUpperCase() : '—';
        return (
          <button className="galileo-file-row" key={path} onClick={() => openFile(path)} title={path} type="button">
            <span className="galileo-file-ext">{ext}</span>
            <span className="galileo-file-name">{name}</span>
          </button>
        );
      })}
      {total > list.length && <div className="galileo-rail-muted mt-1">+{total - list.length} more files</div>}
    </div>
  );
}

function SessionSection({ messages }: { messages: Array<{ role: string; content: string; createdAt?: Date | number }> }) {
  const startedAt = useMemo(() => {
    const first = messages.find((message) => message.createdAt);
    return first?.createdAt ? new Date(first.createdAt).getTime() : Date.now();
  }, [messages]);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const elapsed = Math.max(0, now - startedAt);
  const duration = `${String(Math.floor(elapsed / 3_600_000)).padStart(2, '0')}:${String(Math.floor((elapsed % 3_600_000) / 60_000)).padStart(2, '0')}:${String(Math.floor((elapsed % 60_000) / 1_000)).padStart(2, '0')}`;
  const chars = messages.reduce((sum, message) => sum + message.content.length, 0);
  const tokens = Math.round(chars / 4);

  return (
    <div className="galileo-session-grid">
      <Stat icon="i-ph:clock-duotone" label="Duration" value={duration} />
      <Stat icon="i-ph:chats-duotone" label="Messages" value={String(messages.length)} />
      <Stat icon="i-ph:database-duotone" label="Tokens (est.)" value={tokens.toLocaleString()} />
    </div>
  );
}

function Stat({ icon, label, value }: { icon: string; label: string; value: string }) {
  return (
    <div className="galileo-session-stat">
      <span className={`${icon} text-lg text-bolt-elements-textSecondary`} />
      <span className="galileo-session-stat-value">{value}</span>
      <span className="galileo-session-stat-label">{label}</span>
    </div>
  );
}

function summarize(content: string) {
  const clean = content.replace(/\s+/g, ' ').trim();
  return clean.length > 140 ? `${clean.slice(0, 137)}…` : clean || 'No requests yet.';
}

function detectStack(files: FileMap): Array<{ label: string; tone?: string }> {
  const chips: Array<{ label: string; tone?: string }> = [];
  const paths = new Set(Object.keys(files));
  const pkgKey = [...paths].find((path) => path === 'package.json' || path.endsWith('/package.json'));
  const pkgDirent = pkgKey ? files[pkgKey] : undefined;
  const pkg = pkgDirent && 'content' in pkgDirent ? pkgDirent.content : undefined;

  if (paths.has('tsconfig.json')) chips.push({ label: 'TypeScript', tone: 'accent' });

  if (pkg) {
    try {
      const deps = JSON.parse(pkg) as { dependencies?: Record<string, string>; devDependencies?: Record<string, string> };
      const all = { ...deps.dependencies, ...deps.devDependencies };

      if (all['react']) chips.push({ label: 'React', tone: 'blue' });
      if (all['vite'] || paths.has('vite.config.ts')) chips.push({ label: 'Vite', tone: 'purple' });
      if (all['tailwindcss'] || all['@unocss/reset']) chips.push({ label: 'Tailwind' });
      if (all['astro']) chips.push({ label: 'Astro', tone: 'teal' });
      if (all['next']) chips.push({ label: 'Next.js' });
    } catch {
      // Unparseable manifests simply contribute no chips.
    }
  }

  return chips;
}
