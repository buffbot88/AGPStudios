import { useStore } from '@nanostores/react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { chatId, db, deleteById, getAll, type ChatHistoryItem } from '~/lib/persistence';
import { availableSlots, liquidHealthy, systemStatus, visionActive } from '~/lib/stores/system';
import { autoEnhance, railOpen, railTab, sidebarOpen } from '~/lib/stores/ui';
import { ThemeSwitch } from '~/components/ui/ThemeSwitch';

type SidebarView = 'chats' | 'agents' | 'memory' | 'settings';

type SessionRow = { authenticated: boolean; user?: { username: string; githubLinked: boolean } };

const TOOLS = ['list', 'read', 'search', 'refresh_context'];

function binLabel(timestamp: string) {
  const date = new Date(timestamp);
  const now = new Date();
  const dayMs = 86_400_000;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();

  if (date.getTime() >= startOfToday) {
    return date.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  if (date.getTime() >= startOfToday - dayMs) {
    return 'Yesterday';
  }

  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function Sidebar() {
  const open = useStore(sidebarOpen);
  const railVisible = useStore(railOpen);
  const activeChatId = useStore(chatId);
  const [view, setView] = useState<SidebarView>('chats');
  const [list, setList] = useState<ChatHistoryItem[]>([]);
  const [filter, setFilter] = useState('');
  const [session, setSession] = useState<SessionRow>({ authenticated: false });

  const loadEntries = useCallback(() => {
    getAll(db)
      .then((entries) => setList(entries.filter((item) => item.id && (item.urlId || item.messages?.length))))
      .catch(() => setList([]));
  }, []);

  useEffect(() => {
    if (open) {
      loadEntries();
    }
  }, [open, loadEntries, activeChatId]);

  useEffect(() => {
    fetch('/api/session', { credentials: 'include' })
      .then((response) => (response.ok ? (response.json() as Promise<SessionRow>) : { authenticated: false }))
      .then(setSession)
      .catch(() => setSession({ authenticated: false }));
  }, []);

  const liquid = useStore(liquidHealthy);
  const vision = useStore(visionActive);
  const slots = useStore(availableSlots);
  const status = useStore(systemStatus);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();

    if (!needle) {
      return list;
    }

    return list.filter((item) => (item.description || '').toLowerCase().includes(needle));
  }, [list, filter]);

  const removeItem = useCallback(
    (event: React.UIEvent, item: ChatHistoryItem) => {
      event.preventDefault();
      event.stopPropagation();

      deleteById(db, item.id)
        .then(() => {
          loadEntries();

          if (chatId.get() === item.id) {
            window.location.pathname = '/';
          }
        })
        .catch(() => setList((current) => current));
    },
    [loadEntries],
  );

  return (
    <aside className={`galileo-sidebar ${open ? '' : 'is-collapsed'}`} aria-label="Galileo sidebar">
      <a href="/" className="galileo-new-chat">
        <span className="flex items-center gap-2">
          <span className="i-ph:plus-bold text-base" />
          New Chat
        </span>
        <span className="i-ph:sparkle text-accent-500" />
      </a>

      <nav className="galileo-nav">
        <a className="galileo-nav-item" href="/">
          <span className="i-ph:folder-duotone text-lg" />
          Projects
        </a>
        <button
          className={`galileo-nav-item ${view === 'chats' ? 'is-active' : ''}`}
          onClick={() => setView('chats')}
          type="button"
        >
          <span className="i-ph:clock-counter-clockwise-duotone text-lg" />
          Recent
        </button>
        <button
          className={`galileo-nav-item ${view === 'agents' ? 'is-active' : ''}`}
          onClick={() => setView('agents')}
          type="button"
        >
          <span className="i-ph:users-three-duotone text-lg" />
          Agents
        </button>
        <button
          className={`galileo-nav-item ${view === 'memory' ? 'is-active' : ''}`}
          onClick={() => setView('memory')}
          type="button"
        >
          <span className="i-ph:database-duotone text-lg" />
          Memory
        </button>
        <button
          className={`galileo-nav-item ${view === 'settings' ? 'is-active' : ''}`}
          onClick={() => setView('settings')}
          type="button"
        >
          <span className="i-ph:gear-six-duotone text-lg" />
          Settings
        </button>
      </nav>

      {view === 'chats' && (
        <>
          <div className="galileo-nav-label">
            <span>Recent Conversations</span>
            <span className="i-ph:magnifying-glass text-sm" />
          </div>
          <div className="galileo-search-wrap">
            <input
              aria-label="Filter conversations"
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Search conversations…"
              value={filter}
            />
          </div>
          <div className="galileo-convos">
            {filtered.length === 0 && <div className="px-2 py-1 text-xs text-bolt-elements-textTertiary">No conversations</div>}
            {filtered.map((item) => (
              <a
                key={item.id || item.timestamp}
                href={`/chat/${item.urlId || item.id}`}
                className={`galileo-convo ${activeChatId === item.id ? 'is-active' : ''}`}
              >
                <span className="galileo-dot" style={{ opacity: activeChatId === item.id ? 1 : 0.45 }} />
                <span className="flex-1 truncate">{item.description || 'Untitled chat'}</span>
                <span className="galileo-convo-time">{binLabel(item.timestamp)}</span>
                <span
                  aria-label="Delete conversation"
                  className="galileo-convo-del i-ph:trash text-sm"
                  onClick={(event) => removeItem(event, item)}
                  role="button"
                  tabIndex={0}
                />
              </a>
            ))}
          </div>
        </>
      )}

      {view === 'agents' && (
        <div className="mt-2 flex-1 overflow-y-auto">
          <div className="galileo-agent-row">
            <span className={`galileo-dot ${liquid ? '' : 'opacity-40'}`} />
            <span>
              <strong>Liquid</strong> · text worker ·{' '}
              {liquid ? `healthy${slots !== null ? ` · ${slots} slot${slots === 1 ? '' : 's'}` : ''}` : 'unreachable'}
            </span>
          </div>
          <div className="galileo-agent-row">
            <span className={`galileo-dot ${vision ? '' : 'opacity-40'}`} style={!vision ? { background: 'var(--galileo-text-muted)' } : undefined} />
            <span>
              <strong>Vision</strong> · 450M VL · {vision ? 'active' : 'idle'}
            </span>
          </div>
          <div className="galileo-agent-row">
            <span className="galileo-dot" style={{ background: 'var(--galileo-accent-soft)' }} />
            <span>
              <strong>Galileo Agent</strong> · {TOOLS.join(' / ')} · Alpha-managed
            </span>
          </div>
          {status?.queue && (
            <div className="mx-3 mt-1 rounded-lg border border-bolt-elements-borderColor px-3 py-2 text-xs text-bolt-elements-textSecondary">
              Queue {status.queue.queued_requests}/{status.queue.max_queue} · active {status.queue.active_requests}
            </div>
          )}
        </div>
      )}

      {view === 'memory' && (
        <div className="mt-2 flex-1 overflow-y-auto">
          <p className="mx-3 mb-2 mt-1 text-xs leading-relaxed text-bolt-elements-textTertiary">
            Workspace memory read from the live WebContainer filesystem. Enable tool access in a chat to let the agent refresh it.
          </p>
          <div className="galileo-memory-item">
            <strong>Project files</strong>
            <span>Live workspace synced through /api/projects snapshots</span>
          </div>
          <div className="galileo-memory-item">
            <strong>Conversation history</strong>
            <span>{list.length} saved conversation{list.length === 1 ? '' : 's'} on this device</span>
          </div>
          <div className="galileo-memory-item">
            <strong>Tool context</strong>
            <span>{TOOLS.join(', ')} — read-only workspace tools</span>
          </div>
        </div>
      )}

      {view === 'settings' && (
        <div className="mt-2 flex-1 overflow-y-auto px-3">
          <div className="galileo-memory-item">
            <strong>Theme</strong>
            <span className="mt-1 flex items-center gap-2">
              <ThemeSwitch />
              <span className="text-bolt-elements-textTertiary">Light / dark</span>
            </span>
          </div>
          <div className="galileo-memory-item">
            <strong>Auto-enhance prompts</strong>
            <AutoEnhanceToggle />
          </div>
          <div className="galileo-memory-item">
            <strong>Info rail</strong>
            <RailToggle />
          </div>
          <div className="galileo-memory-item">
            <strong>Account</strong>
            <span>
              {session.authenticated ? session.user?.username || 'signed in' : 'not signed in'}
              {session.authenticated && session.user?.githubLinked ? ' · GitHub linked' : ''}
            </span>
          </div>
        </div>
      )}

      <div className="galileo-sidebar-foot">
        <button
          className="galileo-user-chip"
          onClick={() => {
            railOpen.set(true);
            railTab.set('agent');
          }}
          type="button"
        >
          <span className="galileo-avatar">{session.authenticated ? (session.user?.username?.[0] || 'A').toUpperCase() : 'A'}</span>
          <span className="min-w-0">
            <span className="galileo-user-name block truncate">{session.authenticated ? session.user?.username || 'Signed in' : 'AGP Studios'}</span>
            <span className="galileo-user-plan block">{session.authenticated ? 'Galileo access' : 'Local session'}</span>
          </span>
        </button>
        <button
          aria-label="Toggle info rail"
          className="galileo-icon-btn"
          onClick={() => railOpen.set(!railVisible)}
          type="button"
        >
          <span className={railVisible ? 'i-ph:caret-right' : 'i-ph:caret-left'} />
        </button>
      </div>
    </aside>
  );
}

function AutoEnhanceToggle() {
  const enabled = useStore(autoEnhance).enabled;

  return (
    <button
      className={`galileo-composer-btn ${enabled ? 'is-on' : ''}`}
      onClick={() => autoEnhance.setKey('enabled', !enabled)}
      type="button"
    >
      {enabled ? 'On' : 'Off'}
    </button>
  );
}

function RailToggle() {
  const open = useStore(railOpen);

  return (
    <button className={`galileo-composer-btn ${open ? 'is-on' : ''}`} onClick={() => railOpen.set(!open)} type="button">
      {open ? 'Visible' : 'Hidden'}
    </button>
  );
}
