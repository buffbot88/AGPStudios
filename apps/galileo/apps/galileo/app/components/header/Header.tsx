import { useStore } from '@nanostores/react';
import { ClientOnly } from 'remix-utils/client-only';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChatHistoryItem } from '~/lib/persistence';
import { db, getAll } from '~/lib/persistence';
import { chatStore } from '~/lib/stores/chat';
import { liquidHealthy, statusUpdatedAt, systemStatus, visionActive } from '~/lib/stores/system';
import { railOpen, sidebarOpen } from '~/lib/stores/ui';
import { classNames } from '~/utils/classNames';
import { ChatDescription } from '~/lib/persistence/ChatDescription.client';
import { HeaderActionButtons } from './HeaderActionButtons.client';
import { StatusStrip } from './StatusStrip.client';

type SessionRow = { authenticated: boolean; user?: { username: string; githubLinked: boolean } };

export function Header() {
  const chat = useStore(chatStore);
  const sidebarVisible = useStore(sidebarOpen);

  return (
    <>
      <header
        className={classNames(
          'galileo-command-bar relative flex items-center bg-bolt-elements-background-depth-1 px-4 py-2.5 border-b h-[var(--header-height)] gap-3',
          {
            'border-transparent': !chat.started,
            'border-bolt-elements-borderColor': chat.started,
          },
        )}
      >
        <button
          aria-label="Toggle sidebar"
          className="galileo-icon-btn"
          onClick={() => sidebarOpen.set(!sidebarVisible)}
          type="button"
        >
          <span className={sidebarVisible ? 'i-ph:caret-left' : 'i-ph:caret-right'} />
        </button>

        <a href="/" className="galileo-brand">
          <span className="galileo-logo-mark i-ph:sparkle-fill text-lg" />
          <span className="text-lg font-semibold tracking-tight">Galileo 3</span>
        </a>

        <div className="flex flex-1 justify-center">
          <ProjectSelector />
        </div>

        <div className="flex items-center gap-2">
          {chat.started && (
            <ClientOnly>
              {() => (
                <div className="mr-1">
                  <HeaderActionButtons />
                </div>
              )}
            </ClientOnly>
          )}
          <SearchButton />
          <WorkbenchButton />
          <SystemBell />
          <a className="galileo-icon-btn" href="https://agpstudios.org" rel="noreferrer" target="_blank" title="AGP Studios apps">
            <span className="i-ph:squares-four text-lg" />
          </a>
          <ClientOnly>{() => <AvatarMenu />}</ClientOnly>
        </div>
      </header>
      <ClientOnly>{() => <StatusStrip />}</ClientOnly>
    </>
  );
}

function useOutsideClose(onClose: () => void) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function onMouseDown(event: MouseEvent) {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        onClose();
      }
    }

    window.addEventListener('mousedown', onMouseDown);
    return () => window.removeEventListener('mousedown', onMouseDown);
  }, [onClose]);

  return ref;
}

function ProjectSelector() {
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(() => setOpen(false));

  return (
    <div className="relative" ref={ref}>
      <button className="galileo-project-pill" onClick={() => setOpen((value) => !value)} type="button">
        <span className="galileo-project-selector truncate">
          {/* ChatDescription already renders "Project: <id>" in project chats. */}
          <ClientOnly>{() => <ChatDescription />}</ClientOnly>
        </span>
        <span className="i-ph:caret-down text-xs text-bolt-elements-textTertiary" />
      </button>
      {open && <ProjectMenu />}
    </div>
  );
}

function ProjectMenu() {
  const [projects, setProjects] = useState<Array<{ id: string; updated_at: string }>>([]);

  useEffect(() => {
    fetch('/api/projects?list=1', { credentials: 'include' })
      .then((response) => (response.ok ? (response.json() as Promise<{ projects: Array<{ id: string; updated_at: string }> }>) : { projects: [] }))
      .then((value) => setProjects(value.projects || []))
      .catch(() => setProjects([]));
  }, []);

  return (
    <div className="galileo-menu left-1/2 -translate-x-1/2">
      {projects.length === 0 && <div className="px-3 py-2 text-xs text-bolt-elements-textTertiary">No projects yet</div>}
      {projects.map((project) => (
        <a key={project.id} className="galileo-menu-item" href={`/chat/${project.id}`}>
          <span className="i-ph:folder-duotone text-base" />
          <span className="flex-1 truncate">{project.id}</span>
          <span className="text-xs text-bolt-elements-textTertiary">{new Date(project.updated_at).toLocaleDateString()}</span>
        </a>
      ))}
      <a className="galileo-menu-item" href="/">
        <span className="i-ph:plus text-base" />
        New project…
      </a>
    </div>
  );
}

function SearchButton() {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button aria-label="Search conversations" className="galileo-icon-btn" onClick={() => setOpen(true)} type="button">
        <span className="i-ph:magnifying-glass text-lg" />
      </button>
      {open && <SearchDialog onClose={() => setOpen(false)} />}
    </>
  );
}

function SearchDialog({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState('');
  const [items, setItems] = useState<ChatHistoryItem[]>([]);
  const ref = useOutsideClose(onClose);

  useEffect(() => {
    getAll(db)
      .then((entries) => setItems(entries.filter((item) => item.urlId || item.messages?.length)))
      .catch(() => setItems([]));
  }, []);

  const results = useMemo(() => {
    const needle = query.trim().toLowerCase();

    if (!needle) {
      return items.slice(0, 12);
    }

    return items
      .filter(
        (item) =>
          (item.description || '').toLowerCase().includes(needle) ||
          item.messages.some((message) => typeof message.content === 'string' && message.content.toLowerCase().includes(needle)),
      )
      .slice(0, 12);
  }, [items, query]);

  return (
    <div className="fixed inset-0 z-max flex items-start justify-center bg-black/60 pt-[12vh] backdrop-blur-sm" role="dialog">
      <div className="galileo-panel w-[560px] max-w-[92vw] p-3 shadow-2xl" ref={ref}>
        <input
          autoFocus
          className="w-full rounded-lg border border-bolt-elements-borderColor bg-black/40 px-4 py-3 text-sm text-bolt-elements-textPrimary outline-none focus:border-accent-500"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search conversations and messages…"
          value={query}
        />
        <div className="mt-2 max-h-[50vh] overflow-y-auto">
          {results.length === 0 && <div className="px-3 py-6 text-center text-sm text-bolt-elements-textTertiary">No matches</div>}
          {results.map((item) => (
            <a key={item.id} className="galileo-menu-item" href={`/chat/${item.urlId || item.id}`} onClick={onClose}>
              <span className="i-ph:chats-circle-duotone text-base" />
              <span className="flex-1 truncate">{item.description || 'Untitled chat'}</span>
              <span className="text-xs text-bolt-elements-textTertiary">{new Date(item.timestamp).toLocaleDateString()}</span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

function WorkbenchButton() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: (() => void) | undefined;

    import('~/lib/stores/workbench').then(({ workbenchStore }) => {
      if (cancelled) return;
      setVisible(workbenchStore.showWorkbench.get());
      unsubscribe = workbenchStore.showWorkbench.subscribe(setVisible);
    });

    return () => {
      cancelled = true;
      unsubscribe?.();
    };
  }, []);

  return (
    <button
      aria-label="Toggle workbench"
      className={`galileo-icon-btn ${visible ? 'is-on' : ''}`}
      onClick={() => {
        void import('~/lib/stores/workbench').then(({ workbenchStore }) => workbenchStore.showWorkbench.set(!workbenchStore.showWorkbench.get()));
      }}
      title="Toggle workbench"
      type="button"
    >
      <span className="i-ph:terminal-window text-lg" />
    </button>
  );
}

function SystemBell() {
  const status = useStore(systemStatus);
  const updatedAt = useStore(statusUpdatedAt);
  const liquid = useStore(liquidHealthy);
  const vision = useStore(visionActive);
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(() => setOpen(false));

  const degraded = !status || !liquid;

  return (
    <div className="relative" ref={ref}>
      <button aria-label="System notifications" className="galileo-icon-btn" onClick={() => setOpen((value) => !value)} type="button">
        <span className="i-ph:bell text-lg" />
        {degraded && <span className="galileo-bell-dot" />}
      </button>
      {open && (
        <div className="galileo-menu right-0 w-[300px]">
          <div className="px-3 py-2 text-xs font-semibold uppercase tracking-wider text-bolt-elements-textTertiary">System events</div>
          <div className="galileo-menu-item cursor-default">
            <span className="i-ph:drop-duotone text-base" />
            Liquid: {liquid ? 'healthy' : 'unreachable'}
          </div>
          <div className="galileo-menu-item cursor-default">
            <span className="i-ph:eye-duotone text-base" />
            Vision: {vision ? 'active' : 'idle'}
          </div>
          {status?.gateway && (
            <div className="galileo-menu-item cursor-default">
              <span className="i-ph:pulse-duotone text-base" />
              Gateway {status.gateway.latency_ms} ms{status.gateway.version ? ` · v${status.gateway.version}` : ''}
            </div>
          )}
          {statusErrorNode(status?.gateway?.error)}
          <div className="px-3 pb-2 pt-1 text-[11px] text-bolt-elements-textTertiary">
            {updatedAt ? `Checked ${updatedAt.toLocaleTimeString()}` : 'Waiting for first probe…'}
          </div>
        </div>
      )}
    </div>
  );
}

function statusErrorNode(error: string | null | undefined) {
  if (!error) {
    return null;
  }

  return (
    <div className="galileo-menu-item cursor-default">
      <span className="i-ph:warning-duotone text-base" />
      {error}
    </div>
  );
}

function AvatarMenu() {
  const [session, setSession] = useState<SessionRow>({ authenticated: false });
  const [open, setOpen] = useState(false);
  const ref = useOutsideClose(() => setOpen(false));

  useEffect(() => {
    fetch('/api/session', { credentials: 'include' })
      .then((response) => (response.ok ? (response.json() as Promise<SessionRow>) : { authenticated: false }))
      .then(setSession)
      .catch(() => setSession({ authenticated: false }));
  }, []);

  const initial = session.authenticated ? (session.user?.username?.[0] || 'A').toUpperCase() : 'A';

  return (
    <div className="relative" ref={ref}>
      <button aria-label="Account" className="galileo-avatar" onClick={() => setOpen((value) => !value)} type="button">
        {initial}
      </button>
      {open && (
        <div className="galileo-menu right-0">
          <div className="px-3 py-2">
            <div className="text-sm font-semibold text-bolt-elements-textPrimary">
              {session.authenticated ? session.user?.username || 'Signed in' : 'AGP Studios session'}
            </div>
            <div className="text-xs text-bolt-elements-textTertiary">
              {session.authenticated ? `GitHub ${session.user?.githubLinked ? 'linked' : 'not linked'}` : 'Local session'}
            </div>
          </div>
          <a className="galileo-menu-item" href="/">
            <span className="i-ph:plus-circle-duotone text-base" />
            New project
          </a>
          <a className="galileo-menu-item" href="https://agpstudios.org" rel="noreferrer" target="_blank">
            <span className="i-ph:squares-four-duotone text-base" />
            AGP Studios apps
          </a>
        </div>
      )}
    </div>
  );
}
