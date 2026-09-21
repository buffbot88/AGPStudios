import { useCallback, useEffect, useState } from 'react';
import { API, request, safeList, safeNumber, safeText, safeTime } from './api';
import { confirmModal, confirmToast, toast } from './toast';
import { maybeCrash } from './devCrash';
import { PosterImage } from './home/PosterImage';

type User = { id: string; username: string; email: string; display_name: string; role: string };
type AdminUser = { id: string; username: string; email: string; display_name: string; role: string; is_active: number; banned_at?: number | null; email_verified_at?: string | null };
type AdminSummary = {
  users: number; active_users: number; disabled_users: number;
  open_tickets: number; active_deploys: number; active_projects: number;
  database_manager: string;
};
type MigrationEntry = { filename: string; applied_at?: number | null; statements?: number | null; applied_by?: string };
type MigrationSummary = {
  tracked: boolean; tracking_table: string; current_revision: string | null;
  applied: MigrationEntry[]; pending: { filename: string; statements?: number }[];
  drifted: string[]; missing: string[]; error?: string;
};
type DatabaseStatus = { database: string; version: string; paws?: { status?: string }; migrations?: MigrationSummary };
type AdminTelemetry = { servers: { id: string; label: string; online: boolean; active_requests: number; requests_last_5m: number; generation_tokens_per_second: number; prompt_tokens_per_second: number; total_completion_tokens: number; queue_depth: number; queue_limit: number }[]; updated_at: number };
type OmegaServer = { id: string; label: string; online: boolean; ready: boolean; workers?: number; max_workers: number };
type AdminGame = { slug: string; title: string; summary: string; status: string; tags: string[]; url: string; poster: string; featured: boolean; sort_order?: number };
type AdminTab = 'overview' | 'users' | 'games' | 'telemetry' | 'system';
type RepoPair = { live: string; repo: string; state: string };
type RepoStatus = { ashathub: RepoPair };
type ModerationEvent = { id: number; user_id: string; action: string; actor_id: string; actor_name: string; detail: string; created_at: number };
function repoCommit(pair?: RepoPair) { return pair ? `${pair.live.slice(0, 8)} / ${pair.repo.slice(0, 8)}` : 'Loading…'; }

// ── Helpers ──

function timeAgo(ts: number): string {
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleString();
}

function shortId(id: string): string {
  return id.replace(/^dep_/, '').slice(0, 8);
}

function deployDot(status: string): { dot: string; cls: string } {
  switch (status) {
    case 'deployed': return { dot: '●', cls: 'adm-ready' };
    case 'building': return { dot: '◐', cls: 'adm-building' };
    case 'failed': return { dot: '×', cls: 'adm-failed' };
    case 'undeployed': return { dot: '○', cls: 'adm-removed' };
    default: return { dot: '○', cls: 'adm-removed' };
  }
}

// ── Main Component ──

export function AdminSurface({ user }: { user: User | null }) {
  const [tab, setTab] = useState<AdminTab>(() => {
    const value = window.location.hash.slice(1) as AdminTab;
    return ['overview', 'users', 'telemetry', 'system'].includes(value) ? value : 'overview';
  });
  const [summary, setSummary] = useState<AdminSummary | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [database, setDatabase] = useState<DatabaseStatus | null>(null);
  const [telemetry, setTelemetry] = useState<AdminTelemetry | null>(null);
  const [omegaServers, setOmegaServers] = useState<OmegaServer[]>([]);
  const [settings, setSettings] = useState<Record<string, string | boolean> | null>(null);
  const [repoStatus, setRepoStatus] = useState<RepoStatus | null>(null);
  const [games, setGames] = useState<AdminGame[]>([]);
  const [query, setQuery] = useState('');
  const [deployFilter, setDeployFilter] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Side panels
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null);
  const [moderation, setModeration] = useState<ModerationEvent[]>([]);
  const [moderationError, setModerationError] = useState<string | null>(null);

  const loadModeration = useCallback(async (userId: string) => {
    try {
      const data = await request<{ user_id: string; events: ModerationEvent[] }>(`${API}/admin/users/${encodeURIComponent(userId)}/moderation`);
      setModeration(safeList<ModerationEvent>(data.events));
      setModerationError(null);
    } catch (reason) {
      setModeration([]);
      setModerationError(reason instanceof Error ? reason.message : 'Moderation history unavailable');
    }
  }, []);

  const selectUser = useCallback((u: AdminUser | null) => {
    setSelectedUser(u);
    setModeration([]);
    setModerationError(null);
    if (u) void loadModeration(u.id);
  }, [loadModeration]);

  const load = useCallback(async () => {
    if (!user || user.role.toLowerCase() !== 'admin') return;
    try {
      const search = query.trim() ? `?q=${encodeURIComponent(query.trim())}` : '';
      const [summaryResult, usersResult, databaseResult, settingsResult, repoResult, gamesResult] = await Promise.allSettled([
        request<AdminSummary>(`${API}/admin/summary`),
        request<{ users: AdminUser[] }>(`${API}/admin/users${search}`),
        request<DatabaseStatus>(`${API}/admin/database/status`),
        request<Record<string, string | boolean>>(`${API}/admin/settings`),
        request<RepoStatus>(`${API}/admin/repo-status`),
        request<{ games: AdminGame[] }>(`${API}/admin/games`),
      ]);
      if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
      if (usersResult.status === 'fulfilled') setUsers(safeList(usersResult.value.users));
      if (databaseResult.status === 'fulfilled') setDatabase(databaseResult.value);
      if (settingsResult.status === 'fulfilled') setSettings(settingsResult.value);
      if (repoResult.status === 'fulfilled') setRepoStatus(repoResult.value);
      if (gamesResult.status === 'fulfilled') setGames(safeList<AdminGame>(gamesResult.value.games));
      const firstRejected = [summaryResult, usersResult, databaseResult, settingsResult, repoResult, gamesResult].find(
        (outcome): outcome is PromiseRejectedResult => outcome.status === 'rejected',
      );
      if (firstRejected) {
        const reason = firstRejected.reason;
        setError(reason instanceof Error ? reason.message : 'Admin data unavailable');
      } else {
        setError(null);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Admin data unavailable'); }
  }, [query, user]);

  useEffect(() => { void load(); }, [load]);
  const loadTelemetry = useCallback(async () => { try { setTelemetry(await request<AdminTelemetry>(`${API}/admin/telemetry`)); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Telemetry unavailable'); } }, []);
  const loadOmegaServers = useCallback(async () => { try { const data = await request<{ servers: OmegaServer[] }>(`${API}/admin/omega/servers`); setOmegaServers(safeList<OmegaServer>(data.servers)); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Omega status unavailable'); } }, []);
  useEffect(() => { if (tab === 'telemetry') { void loadTelemetry(); void loadOmegaServers(); } }, [tab, loadTelemetry, loadOmegaServers]);
  async function telemetryAction(action: 'refresh' | 'clear') { setBusy(true); setError(null); try { await request(`${API}/admin/telemetry/${action}`, { method: 'POST' }); await loadTelemetry(); await loadOmegaServers(); toast(action === 'refresh' ? 'Telemetry refreshed.' : 'Server cache cleared.', 'success'); } catch (reason) { setError(reason instanceof Error ? reason.message : `Telemetry ${action} failed`); } finally { setBusy(false); } }
  async function pushGithub() { if (!(await confirmToast({ message: 'Push committed Ashat Hub changes to GitHub main?', confirmLabel: 'Push' }))) return; setBusy(true); setError(null); try { await request(`${API}/admin/github/push`, { method: 'POST' }); toast('Changes pushed to GitHub.', 'success'); } catch (reason) { setError(reason instanceof Error ? reason.message : 'GitHub push failed'); } finally { setBusy(false); } }
  async function applyMigrations(baseline: boolean) {
    if (!(await confirmToast({
      message: baseline
        ? 'Record the current migration files as applied WITHOUT running them? Use this only for a database migrated by hand before the runner existed.'
        : 'Run all pending database migrations now? Schema changes apply immediately.',
      confirmLabel: baseline ? 'Baseline' : 'Run migrations',
      danger: !baseline,
    }))) return;
    setBusy(true); setError(null);
    try {
      const result = await request<{ ok: boolean; applied: string[] }>(`${API}/admin/database/migrations/apply`, {
        method: 'POST',
        body: JSON.stringify({ baseline }),
      });
      const count = safeList<string>(result.applied).length;
      toast(count ? `${count} migration(s) ${baseline ? 'baselined' : 'applied'}.` : 'No migrations were pending.', count ? 'success' : 'info');
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Migration run failed'); }
    finally { setBusy(false); }
  }

  async function systemUpdate() { if (!(await confirmToast({ message: 'Pull main, rebuild Ashat Hub, and restart the live service?', confirmLabel: 'Update & restart', danger: true }))) return; setBusy(true); setError(null); try { const result = await request<{ commit: string }>(`${API}/admin/system/update`, { method: 'POST' }); toast(`System updated to ${result.commit}. Reloading Admin CP…`); window.setTimeout(() => window.location.reload(), 2500); } catch (reason) { setError(reason instanceof Error ? reason.message : 'System update failed'); } finally { setBusy(false); } }
  async function codingAgentsUpdate() { if (!(await confirmToast({ message: 'Launch the Omega coding agent?', confirmLabel: 'Launch' }))) return; setBusy(true); setError(null); try { await request(`${API}/admin/omega/launch`, { method: 'POST' }); toast('Omega coding agent launched.', 'success'); await loadTelemetry(); await loadOmegaServers(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Omega launch failed'); } finally { setBusy(false); } }

  async function updateUser(userId: string, body: { role?: string; is_active?: boolean }) {
    setBusy(true); setError(null);
    try {
      // API shape: {user_id, status: 'active'|'disabled'} — a bare is_active boolean
      // is read as `form.get('status','active')` by the API, so disable never sticks.
      await request(`${API}/admin/users/status`, {
        method: 'POST',
        body: JSON.stringify(
          body.role
            ? { user_id: userId, role: body.role }
            : { user_id: userId, status: body.is_active ? 'active' : 'disabled' },
        ),
      });
      toast(body.role ? `Role updated to ${body.role}.` : body.is_active ? 'User enabled.' : 'User disabled.', 'success');
      await load();
      if (selectedUser && selectedUser.id === userId) {
        const updated = await request<{ users: AdminUser[] }>(`${API}/admin/users?q=${encodeURIComponent(userId)}`);
        const fresh = safeList<AdminUser>(updated.users).find(u => u?.id === userId);
        if (fresh) setSelectedUser(fresh);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'User update failed'); }
    finally { setBusy(false); }
  }

  async function banUser(userId: string, banned: boolean) {
    const action = banned ? 'Ban' : 'Unban';
    if (!(await confirmToast({ message: `${action} this user?${banned ? ' They will be logged out immediately.' : ''}`, confirmLabel: action, danger: banned }))) return;
    setBusy(true); setError(null);
    try {
      // API now accepts {user_id, banned: bool} on POST /users/ban for both
      // ban and unban — no need to route unban through /users/status.
      await request(`${API}/admin/users/ban`, { method: 'POST', body: JSON.stringify({ user_id: userId, banned }) });
      toast(banned ? 'User banned.' : 'User unbanned.', banned ? 'error' : 'success');
      await load();
      if (selectedUser && selectedUser.id === userId) {
        const updated = await request<{ users: AdminUser[] }>(`${API}/admin/users?q=${encodeURIComponent(userId)}`);
        const fresh = safeList<AdminUser>(updated.users).find(u => u?.id === userId);
        if (fresh) setSelectedUser(fresh);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : `${action} failed`); }
    finally { setBusy(false); }
  }

  async function updateGamePoster(slug: string, poster: string) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/games/${encodeURIComponent(slug)}`, { method: 'PUT', body: JSON.stringify({ poster }) });
      toast('Poster updated.', 'success');
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Poster update failed'); }
    finally { setBusy(false); }
  }

  async function deleteUser(userId: string) {
    if (!(await confirmModal({
      title: 'Delete user permanently?',
      message: 'This cannot be undone. All their projects, deployments, and data will be removed.',
      confirmLabel: 'Delete user',
      requireText: 'DELETE',
    }))) return;
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/users/${userId}`, { method: 'DELETE' });
      toast('User deleted permanently.', 'success');
      setSelectedUser(null);
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Delete failed'); }
    finally { setBusy(false); }
  }

  if (!user || user.role.toLowerCase() !== 'admin') return <section className="member-panel workspace-locked"><span className="eyebrow">AGP Admin</span><h2>Administrator access required.</h2></section>;

  maybeCrash('admin');

  const tabs: { id: AdminTab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'users', label: 'Users' },
    { id: 'games', label: 'Games' },
    { id: 'telemetry', label: 'Omega' },
    { id: 'system', label: 'System' },
  ];

  return (
    <section className="member-section adm-surface" aria-label="AGP admin control panel">
      <div className="section-heading">
        <div><span className="eyebrow">AGP Studios</span><h2>Admin Control</h2></div>
        <a className="adm-back-link" href="/">← Back to AGP Studios</a>
      </div>
      {error && <p className="workspace-error" role="alert">{error}</p>}

      <nav className="adm-tabs" aria-label="Admin navigation">
        {tabs.map((t) => (
          <button key={t.id} type="button" className={tab === t.id ? 'adm-tab selected' : 'adm-tab'} onClick={() => { setTab(t.id); window.history.replaceState({}, '', `/admin#${t.id}`); setSelectedUser(null); }}>{t.label}</button>
        ))}
      </nav>

      {tab === 'overview' && <OverviewTab summary={summary} onNavigate={setTab} />}
      {tab === 'users' && <UsersTab users={users} query={query} onQueryChange={setQuery} onSearch={load} onSelectUser={selectUser} selectedUser={selectedUser} busy={busy} currentUser={user} onUpdateUser={updateUser} onBanUser={banUser} onDeleteUser={deleteUser} moderation={moderation} moderationError={moderationError} onRefreshModeration={() => selectedUser && void loadModeration(selectedUser.id)} />}
      {tab === 'games' && <GamesTab games={games} busy={busy} onUpdatePoster={updateGamePoster} />}
      {tab === 'telemetry' && <TelemetryTab telemetry={telemetry} servers={omegaServers} busy={busy} onAction={telemetryAction} onUpdate={codingAgentsUpdate} />}
      {tab === 'system' && <SystemTab database={database} settings={settings} onPush={pushGithub} onUpdate={systemUpdate} status={repoStatus} busy={busy} onApplyMigrations={applyMigrations} />}
    </section>
  );
}

function TelemetryTab({ telemetry, servers, busy, onAction, onUpdate }: { telemetry: AdminTelemetry | null; servers: OmegaServer[]; busy: boolean; onAction: (action: 'refresh' | 'clear') => void; onUpdate: () => void }) {
  return <div className="adm-telemetry-page">
    <section className="adm-update-card"><div className="adm-update-card-copy"><span className="eyebrow">Omega Inference</span><h3>Qwen + Vision</h3><p>Omega hosts Qwen for coding and the 450M VL worker for image understanding. Alpha runs the agent runtime only.</p><div className="adm-agent-flow"><span>Qwen · coding</span><b>→</b><span>Vision · 450M VL</span><b>→</b><span>Alpha · tools</span></div></div><div className="adm-update-card-actions"><button type="button" className="primary-button" onClick={onUpdate} disabled={busy}>{busy ? 'Starting…' : 'Ensure Omega is running'}</button></div></section>
    
    <div className="adm-toolbar adm-telemetry-toolbar"><button type="button" className="secondary-button" onClick={() => onAction('refresh')} disabled={busy}>Refresh telemetry</button><button type="button" className="secondary-button" onClick={() => onAction('clear')} disabled={busy}>Clear server cache</button><span className="refresh-state">{telemetry ? `Updated ${safeTime(telemetry.updated_at * 1000).split(', ').pop() ?? ''}` : 'Loading…'}</span></div>
    <div className="admin-telemetry-grid">{safeList<OmegaServer>(servers).map((server) => <div className={`admin-server ${server.online ? 'online' : 'offline'}`} key={safeText(server.id)}><div className="admin-server-heading"><strong>{safeText(server.label)}</strong><span className="admin-server-status">{server.online ? '● Online' : '○ Offline'}</span></div><small>{server.ready ? 'Ready' : 'Not ready'} · {safeNumber(server.workers)}/{safeNumber(server.max_workers)} workers</small><small>Omega managed by Ashat Hub SSH provisioning</small></div>)}</div>
  </div>;
}

// ── Games Tab ──

function GamesTab({ games, busy, onUpdatePoster }: { games: AdminGame[]; busy: boolean; onUpdatePoster: (slug: string, poster: string) => void }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const draft = (slug: string, current: string) => drafts[slug] ?? current;
  return (
    <div className="adm-panel-layout">
      <div className="adm-games-list">
        <div className="adm-toolbar"><span className="eyebrow">Game art</span><span className="refresh-state">Poster URLs update live — no redeploy needed</span></div>
        {safeList<AdminGame>(games).filter(Boolean).map((game) => (
          <form className="adm-game-row" key={safeText(game.slug)} onSubmit={(e) => { e.preventDefault(); onUpdatePoster(safeText(game.slug), draft(safeText(game.slug), safeText(game.poster)).trim()); }}>
            <div className="adm-game-thumb">
              <PosterImage
                src={draft(safeText(game.slug), safeText(game.poster))}
                alt=""
                fallbackClass="thumb-fallback fallback-in-development"
                fallback={<span aria-hidden>◎</span>}
              />
            </div>
            <div className="adm-game-info">
              <strong>{safeText(game.title)}</strong>
              <small>{safeText(game.slug)} · {safeText(game.status)}{game.featured ? ' · featured' : ''}</small>
              <input className="adm-game-poster-input" value={draft(safeText(game.slug), safeText(game.poster))}
                onChange={(e) => setDrafts({ ...drafts, [safeText(game.slug)]: e.target.value })}
                placeholder="/poster.svg or https://…" spellCheck={false} />
            </div>
            <button type="submit" className="secondary-button" disabled={busy}>Save poster</button>
          </form>
        ))}
        {games.length === 0 && <div className="adm-empty">No games configured.</div>}
      </div>
    </div>
  );
}

// ── Overview Tab ──

function OverviewTab({ summary, onNavigate }: { summary: AdminSummary | null; onNavigate: (tab: AdminTab) => void }) {
  return (
    <div className="adm-overview">
      <div className="adm-metric-cards">
        <div className="adm-metric-card">
          <span className="adm-metric-label">Total Users</span>
          <strong className="adm-metric-value">{summary ? safeNumber(summary.users).toLocaleString() : '—'}</strong>
          <small className="adm-metric-sub">{safeNumber(summary?.active_users)} active · {safeNumber(summary?.disabled_users)} disabled</small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Live Deployments</span>
          <strong className="adm-metric-value">{summary ? safeNumber(summary.active_deploys).toLocaleString() : '—'}</strong>
          <small className="adm-metric-sub">{safeNumber(summary?.active_projects)} projects</small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Open Tickets</span>
          <strong className="adm-metric-value">{summary ? safeNumber(summary.open_tickets).toLocaleString() : '—'}</strong>
          <small className="adm-metric-sub">{safeNumber(summary?.open_tickets) === 0 ? 'All clear' : 'Needs attention'}</small>
        </div>
      </div>
      <div className="adm-overview-actions">
        <button type="button" className="adm-action-card" onClick={() => onNavigate('users')}><span className="adm-action-icon">♙</span><span><strong>Manage Users</strong><small>Accounts, roles, and access</small></span><b>→</b></button>
        <button type="button" className="adm-action-card" onClick={() => onNavigate('telemetry')}><span className="adm-action-icon">⌁</span><span><strong>Omega</strong><small>Qwen and Vision status</small></span><b>→</b></button>
        <button type="button" className="adm-action-card" onClick={() => onNavigate('system')}><span className="adm-action-icon">⚙</span><span><strong>System Health</strong><small>Database and configuration</small></span><b>→</b></button>
      </div>

    </div>
  );
}

// ── Users Tab ──

function UsersTab({ users, query, onQueryChange, onSearch, onSelectUser, selectedUser, busy, currentUser, onUpdateUser, onBanUser, onDeleteUser, moderation, moderationError, onRefreshModeration }: {
  users: AdminUser[]; query: string; onQueryChange: (q: string) => void; onSearch: () => void;
  onSelectUser: (u: AdminUser | null) => void; selectedUser: AdminUser | null;
  busy: boolean; currentUser: User; onUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onBanUser: (id: string, banned: boolean) => void; onDeleteUser: (id: string) => void;
  moderation: ModerationEvent[]; moderationError: string | null; onRefreshModeration: () => void;
}) {
  return (
    <div className="adm-panel-layout">
      <div className="adm-panel-list">
        <div className="adm-toolbar">
          <input value={query} onChange={(e) => onQueryChange(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }} placeholder="Search users..." />
          <button type="button" className="secondary-button" onClick={onSearch}>Search</button>
        </div>
        <div className="adm-user-list">
          {safeList<AdminUser>(users).filter(Boolean).map((u) => (
            <button key={safeText(u.id)} type="button" className={selectedUser?.id === u?.id ? 'adm-user-row selected' : 'adm-user-row'} onClick={() => onSelectUser(u)}>
              <div className="adm-user-row-info">
                <strong>{safeText(u?.display_name) || safeText(u?.username)}</strong>
                <small>@{safeText(u?.username)} · {safeText(u?.email)}</small>
              </div>
              <span className="adm-user-role">{safeText(u?.role)}</span>
              <span className={u?.is_active ? 'adm-status-dot active' : 'adm-status-dot disabled'} />
            </button>
          ))}
          {users.length === 0 && <div className="adm-empty">No users found.</div>}
        </div>
      </div>
      {selectedUser && <UserDetailPanel user={selectedUser} busy={busy} currentUser={currentUser} onUpdate={onUpdateUser} onBan={onBanUser} onDelete={onDeleteUser} onClose={() => onSelectUser(null)} moderation={moderation} moderationError={moderationError} onRefreshModeration={onRefreshModeration} />}
    </div>
  );
}

function UserDetailPanel({ user: u, busy, currentUser, onUpdate, onBan, onDelete, onClose, moderation, moderationError, onRefreshModeration }: {
  user: AdminUser; busy: boolean; currentUser: User; onUpdate: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onBan: (id: string, banned: boolean) => void; onDelete: (id: string) => void; onClose: () => void;
  moderation: ModerationEvent[]; moderationError: string | null; onRefreshModeration: () => void;
}) {
  const isBanned = Boolean(u.banned_at);
  return (
    <div className="adm-detail-panel">
      <div className="adm-detail-header">
        <h3>{safeText(u.display_name) || safeText(u.username)}</h3>
        <button type="button" className="adm-close-btn" onClick={onClose}>×</button>
      </div>
      <p className="adm-detail-email">{safeText(u.email)}</p>
      <div className="adm-detail-fields">
        <div className="adm-field"><span className="adm-field-label">Status</span><span className={!u.is_active || isBanned ? 'adm-badge err' : 'adm-badge ok'}>{isBanned ? '⊘ Banned' : u.is_active ? '● Active' : '○ Disabled'}</span></div>
        <div className="adm-field"><span className="adm-field-label">Role</span>
          <select value={safeText(u.role)} disabled={busy || u.id === currentUser.id} onChange={(e) => void onUpdate(u.id, { role: e.target.value })}>
            <option>Member</option><option>Pro</option><option>Admin</option>
          </select>
        </div>
        <div className="adm-field"><span className="adm-field-label">Username</span><span className="adm-field-value mono">@{safeText(u.username)}</span></div>
        <div className="adm-field"><span className="adm-field-label">User ID</span><span className="adm-field-value mono">{safeText(u.id)}</span></div>
        {u.email_verified_at && <div className="adm-field"><span className="adm-field-label">Email Verified</span><span className="adm-field-value">{safeTime(u.email_verified_at)}</span></div>}
      </div>
      <div className="adm-detail-actions">
        <button type="button" className={u.is_active ? 'adm-btn-danger' : 'adm-btn-ok'} disabled={busy || u.id === currentUser.id} onClick={() => void onUpdate(u.id, { is_active: !Boolean(u.is_active) })}>
          {u.is_active ? 'Disable User' : 'Enable User'}
        </button>
        <button type="button" className={isBanned ? 'adm-btn-ok' : 'adm-btn-danger'} disabled={busy || u.id === currentUser.id} onClick={() => void onBan(u.id, !isBanned)}>
          {isBanned ? 'Unban User' : 'Ban User'}
        </button>
      </div>
      <div className="adm-detail-actions adm-danger-zone">
        <button type="button" className="adm-btn-delete" disabled={busy || u.id === currentUser.id} onClick={() => void onDelete(u.id)}>
          Delete User Permanently
        </button>
      </div>
      <div className="adm-moderation-history">
        <div className="adm-moderation-heading">
          <span className="eyebrow">Moderation history</span>
          <button type="button" className="adm-close-btn" onClick={onRefreshModeration} aria-label="Refresh moderation history">↻</button>
        </div>
        {moderationError ? <p className="workspace-error" role="alert">{moderationError}</p>
          : moderation.length === 0 ? <p className="muted">No moderation actions recorded.</p>
          : <ul className="adm-moderation-list">
              {moderation.map((event) => <li key={safeText(event.id)} className="adm-moderation-event">
                <span className={`adm-badge ${String(event.action).includes('banned') && !String(event.action).includes('un') ? 'err' : 'ok'}`}>
                  {safeText(event.action).replace('user.', '').replace('.', ' ')}
                </span>
                <span className="adm-moderation-detail">{safeText(event.detail)}</span>
                <small className="adm-moderation-meta">by {safeText(event.actor_name)} · {safeTime(event.created_at)}</small>
              </li>)}
            </ul>}
      </div>
    </div>
  );
}

// ── Migrations panel (System tab) ──

/**
 * Applied/pending migration panel. Reads the `migrations` object carried by
 * the existing /api/admin/database/status response (no extra fetch) and runs
 * pending files through POST /api/admin/database/migrations/apply.
 */
function MigrationsPanel({ migrations, busy, onApplyMigrations }: {
  migrations: MigrationSummary | undefined; busy: boolean;
  onApplyMigrations: (baseline: boolean) => void;
}) {
  if (!migrations) {
    return (
      <section className="adm-panel adm-migrations">
        <span className="eyebrow">Schema migrations</span>
        <h3>Loading…</h3>
        <p className="muted">Migration state arrives with the database status.</p>
      </section>
    );
  }

  const applied = safeList<MigrationEntry>(migrations.applied);
  const pending = safeList<{ filename: string; statements?: number }>(migrations.pending);
  const drifted = safeList<string>(migrations.drifted);
  const missing = safeList<string>(migrations.missing);

  return (
    <section className="adm-panel adm-migrations">
      <div className="adm-migrations-heading">
        <div>
          <span className="eyebrow">Schema migrations</span>
          <h3>{pending.length ? `${pending.length} pending` : 'Up to date'}</h3>
        </div>
        <span className={pending.length ? 'adm-badge err' : 'adm-badge ok'}>
          {pending.length ? '◐ Applying needed' : '● Current'}
        </span>
      </div>

      <div className="adm-detail-fields">
        <div className="adm-field">
          <span className="adm-field-label">Current revision</span>
          <span className="adm-field-value mono">{safeText(migrations.current_revision) || '—'}</span>
        </div>
        <div className="adm-field">
          <span className="adm-field-label">Tracking</span>
          <span className="adm-field-value mono">
            {migrations.tracked ? safeText(migrations.tracking_table) || 'schema_migrations' : 'not tracked'}
          </span>
        </div>
      </div>

      {!migrations.tracked && <p className="adm-migrations-note">
        This database has no migration history yet. If it was migrated by hand, baseline it once so
        existing files are recorded without being re-run (migration 0001 is not idempotent).
      </p>}
      {migrations.error && <p className="workspace-error" role="alert">{migrations.error}</p>}

      {drifted.length > 0 && <p className="adm-migrations-note">
        Changed on disk since they were applied (not re-run): {drifted.map(safeText).join(', ')}.
      </p>}
      {missing.length > 0 && <p className="adm-migrations-note">
        Recorded but no longer in the repo: {missing.map(safeText).join(', ')}.
      </p>}

      <div className="adm-migrations-actions">
        <button type="button" className="primary-button" disabled={busy || pending.length === 0}
          onClick={() => onApplyMigrations(false)}>
          {pending.length ? `Run ${pending.length} pending` : 'Nothing to run'}
        </button>
        {!migrations.tracked && <button type="button" className="secondary-button" disabled={busy}
          onClick={() => onApplyMigrations(true)}>Baseline existing</button>}
      </div>

      <div className="adm-migration-lists">
        <div>
          <span className="eyebrow">Applied ({applied.length})</span>
          <ul className="adm-migration-list">
            {applied.map((entry) => <li key={safeText(entry.filename)}>
              <code>{safeText(entry.filename)}</code>
              <small>{safeNumber(entry.statements)} stmt · {safeTime(entry.applied_at)}</small>
            </li>)}
            {applied.length === 0 && <li><small className="muted">None recorded.</small></li>}
          </ul>
        </div>
        <div>
          <span className="eyebrow">Pending ({pending.length})</span>
          <ul className="adm-migration-list">
            {pending.map((entry) => <li key={safeText(entry.filename)}>
              <code>{safeText(entry.filename)}</code>
              <small>{safeNumber(entry.statements)} stmt</small>
            </li>)}
            {pending.length === 0 && <li><small className="muted">Nothing pending.</small></li>}
          </ul>
        </div>
      </div>
    </section>
  );
}

// ── System Tab ──

function SystemTab({ database, settings, onPush, onUpdate, status, busy, onApplyMigrations }: {
  database: DatabaseStatus | null; settings: Record<string, string | boolean> | null;
  onPush: () => void; onUpdate: () => void; status: RepoStatus | null;
  busy: boolean; onApplyMigrations: (baseline: boolean) => void;
}) {
  return (
    <div className="adm-system">
      <MigrationsPanel migrations={database?.migrations} busy={busy} onApplyMigrations={onApplyMigrations} />
      <div className="adm-system-grid">
        <section className="adm-panel">
          <span className="eyebrow">Database</span>
          <h3>{safeText(database?.version) || 'Loading...'}</h3><p className="system-status-ok">● Connected</p>
          <span className="eyebrow">Database Management</span>
          <div className="adm-field"><span className="adm-field-label">Backend</span><span className="adm-field-value">{safeText(database?.database) || 'Unknown'}</span></div>
          <div className="adm-field"><span className="adm-field-label">Update Method</span><span className="adm-field-value">Versioned</span></div>
          <div className="adm-field"><span className="adm-field-label">Direct SQL Console</span><span className="adm-field-value">Disabled</span></div>
          {database?.paws && <div className="adm-field"><span className="adm-field-label">Paws & Parcels</span><span className="adm-field-value">{safeText(database.paws.status) || 'unknown'}</span></div>}
        </section>
        <section className="adm-panel">
          <span className="eyebrow">Application Runtime</span>
          <h3>Services</h3><div className="adm-field"><span className="adm-field-label">Backend</span><span className="adm-field-value">Rust · <b className="system-status-ok">● Running</b></span></div><div className="adm-field"><span className="adm-field-label">Frontend</span><span className="adm-field-value">Vite · <b className="system-status-ok">● Available</b></span></div><span className="eyebrow">Security & Configuration</span>
          <div className={`adm-system-updates adm-repo-${safeText(status?.ashathub?.state) || 'unknown'}`}><span className="adm-field-label">System Updates</span><div className="adm-commit-comparison"><div><span>Live commit</span><code>{safeText(status?.ashathub?.live).slice(0, 8) || '—'}</code></div><div><span>Repo commit</span><code>{safeText(status?.ashathub?.repo).slice(0, 8) || '—'}</code></div></div><div className="adm-system-update-actions"><button type="button" className="secondary-button" onClick={onPush}>Push Changes to GitHub</button><button type="button" className="secondary-button" onClick={onUpdate}>Pull, Build & Restart</button></div></div>
          {settings && Object.entries(settings)
            .filter(([key]) => key !== 'backend') // internal value, not a user-facing setting
            .map(([key, value]) => (
              <div className="adm-setting-row" key={key}><span>{key.replace(/_/g, ' ')}</span><strong>{typeof value === 'boolean' ? (value ? '✓ Enabled' : '○ Disabled') : String(value)}</strong></div>
            ))}
        </section>
      </div>
    </div>
  );
}

