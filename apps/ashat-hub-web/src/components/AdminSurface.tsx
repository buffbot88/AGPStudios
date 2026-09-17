import { useCallback, useEffect, useState } from 'react';
import { API, request } from './api';

type User = { id: string; username: string; email: string; display_name: string; role: string };
type AdminUser = { id: string; username: string; email: string; display_name: string; role: string; is_active: number; banned_at?: number | null; email_verified_at?: string | null };
type AdminSummary = {
  users: number; active_users: number; disabled_users: number;
  open_tickets: number; active_deploys: number; active_projects: number;
  database_manager: string;
};
type DatabaseStatus = { version: string; migrations: [number, string, boolean][]; maintenance: string; arbitrary_sql: string };
type AdminTelemetry = { servers: { id: string; label: string; online: boolean; active_requests: number; requests_last_5m: number; generation_tokens_per_second: number; prompt_tokens_per_second: number; total_completion_tokens: number; queue_depth: number; queue_limit: number }[]; updated_at: number };
type OmegaServer = { id: string; label: string; online: boolean; ready: boolean; workers?: number; max_workers: number };
type AdminTab = 'overview' | 'users' | 'telemetry' | 'system';
type RepoPair = { live: string; repo: string; state: string };
type RepoStatus = { ashathub: RepoPair };
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
  const [query, setQuery] = useState('');
  const [deployFilter, setDeployFilter] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Side panels
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null);

  const load = useCallback(async () => {
    if (!user || user.role.toLowerCase() !== 'admin') return;
    try {
      const search = query.trim() ? `?q=${encodeURIComponent(query.trim())}` : '';
      const [nextSummary, nextUsers, nextDatabase, nextSettings, nextRepoStatus] = await Promise.all([
        request<AdminSummary>(`${API}/admin/summary`),
        request<{ users: AdminUser[] }>(`${API}/admin/users${search}`),
        request<DatabaseStatus>(`${API}/admin/database/status`),
        request<Record<string, string | boolean>>(`${API}/admin/settings`),
        request<RepoStatus>(`${API}/admin/repo-status`),
      ]);
      setSummary(nextSummary);
      setUsers(nextUsers.users);
      setDatabase(nextDatabase);
      setSettings(nextSettings);
      setRepoStatus(nextRepoStatus);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Admin data unavailable'); }
  }, [query, user]);

  useEffect(() => { void load(); }, [load]);
  const loadTelemetry = useCallback(async () => { try { setTelemetry(await request<AdminTelemetry>(`${API}/admin/telemetry`)); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Telemetry unavailable'); } }, []);
  const loadOmegaServers = useCallback(async () => { try { const data = await request<{ servers: OmegaServer[] }>(`${API}/admin/omega/servers`); setOmegaServers(data.servers); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Omega status unavailable'); } }, []);
  useEffect(() => { if (tab === 'telemetry') { void loadTelemetry(); void loadOmegaServers(); } }, [tab, loadTelemetry, loadOmegaServers]);
  async function telemetryAction(action: 'refresh' | 'clear') { setBusy(true); setError(null); try { await request(`${API}/admin/telemetry/${action}`, { method: 'POST' }); await loadTelemetry(); await loadOmegaServers(); } catch (reason) { setError(reason instanceof Error ? reason.message : `Telemetry ${action} failed`); } finally { setBusy(false); } }
  async function pushGithub() { if (!window.confirm('Push committed Ashat Hub changes to GitHub main?')) return; setBusy(true); setError(null); try { await request(`${API}/admin/github/push`, { method: 'POST' }); } catch (reason) { setError(reason instanceof Error ? reason.message : 'GitHub push failed'); } finally { setBusy(false); } }
  async function systemUpdate() { if (!window.confirm('Pull main, rebuild Ashat Hub, and restart the live service?')) return; setBusy(true); setError(null); try { const result = await request<{ commit: string }>(`${API}/admin/system/update`, { method: 'POST' }); window.alert(`System updated to ${result.commit}. Reloading Admin CP…`); window.setTimeout(() => window.location.reload(), 2500); } catch (reason) { setError(reason instanceof Error ? reason.message : 'System update failed'); } finally { setBusy(false); } }
  async function codingAgentsUpdate() { if (!window.confirm('Launch the Omega coding agent?')) return; setBusy(true); setError(null); try { await request(`${API}/admin/omega/launch`, { method: 'POST' }); window.alert('Omega coding agent launched.'); await loadTelemetry(); await loadOmegaServers(); } catch (reason) { setError(reason instanceof Error ? reason.message : 'Omega launch failed'); } finally { setBusy(false); } }

  async function updateUser(userId: string, body: { role?: string; is_active?: boolean }) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/users/${body.role ? 'role' : 'status'}`, { method: 'POST', body: JSON.stringify(body.role ? { user_id: userId, role: body.role } : { user_id: userId, is_active: body.is_active }) });
      await load();
      if (selectedUser && selectedUser.id === userId) {
        const updated = await request<{ users: AdminUser[] }>(`${API}/admin/users?q=${encodeURIComponent(userId)}`);
        const fresh = updated.users.find(u => u.id === userId);
        if (fresh) setSelectedUser(fresh);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'User update failed'); }
    finally { setBusy(false); }
  }

  async function banUser(userId: string, banned: boolean) {
    const action = banned ? 'Ban' : 'Unban';
    if (!window.confirm(`${action} this user? ${banned ? 'They will be logged out immediately.' : ''}`)) return;
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/users/ban`, { method: 'POST', body: JSON.stringify({ user_id: userId, banned }) });
      await load();
      if (selectedUser && selectedUser.id === userId) {
        const updated = await request<{ users: AdminUser[] }>(`${API}/admin/users?q=${encodeURIComponent(userId)}`);
        const fresh = updated.users.find(u => u.id === userId);
        if (fresh) setSelectedUser(fresh);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : `${action} failed`); }
    finally { setBusy(false); }
  }

  async function deleteUser(userId: string) {
    if (!window.confirm('PERMANENTLY DELETE this user? This cannot be undone. All their projects, deployments, and data will be removed.')) return;
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/users/${userId}`, { method: 'DELETE' });
      setSelectedUser(null);
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Delete failed'); }
    finally { setBusy(false); }
  }

  if (!user || user.role.toLowerCase() !== 'admin') return <section className="member-panel workspace-locked"><span className="eyebrow">AGP Admin</span><h2>Administrator access required.</h2></section>;

  const tabs: { id: AdminTab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'users', label: 'Users' },
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
      {tab === 'users' && <UsersTab users={users} query={query} onQueryChange={setQuery} onSearch={load} onSelectUser={setSelectedUser} selectedUser={selectedUser} busy={busy} currentUser={user} onUpdateUser={updateUser} onBanUser={banUser} onDeleteUser={deleteUser} />}
      {tab === 'telemetry' && <TelemetryTab telemetry={telemetry} servers={omegaServers} busy={busy} onAction={telemetryAction} onUpdate={codingAgentsUpdate} />}
      {tab === 'system' && <SystemTab database={database} settings={settings} onPush={pushGithub} onUpdate={systemUpdate} status={repoStatus} />}
    </section>
  );
}

function TelemetryTab({ telemetry, servers, busy, onAction, onUpdate }: { telemetry: AdminTelemetry | null; servers: OmegaServer[]; busy: boolean; onAction: (action: 'refresh' | 'clear') => void; onUpdate: () => void }) {
  return <div className="adm-telemetry-page">
    <section className="adm-update-card"><div className="adm-update-card-copy"><span className="eyebrow">Omega Inference</span><h3>Qwen + Vision</h3><p>Omega hosts Qwen for coding and the 450M VL worker for image understanding. Alpha runs the agent runtime only.</p><div className="adm-agent-flow"><span>Qwen · coding</span><b>→</b><span>Vision · 450M VL</span><b>→</b><span>Alpha · tools</span></div></div><div className="adm-update-card-actions"><button type="button" className="primary-button" onClick={onUpdate} disabled={busy}>{busy ? 'Starting…' : 'Ensure Omega is running'}</button></div></section>
    
    <div className="adm-toolbar adm-telemetry-toolbar"><button type="button" className="secondary-button" onClick={() => onAction('refresh')} disabled={busy}>Refresh telemetry</button><button type="button" className="secondary-button" onClick={() => onAction('clear')} disabled={busy}>Clear server cache</button><span className="refresh-state">{telemetry ? `Updated ${new Date(telemetry.updated_at * 1000).toLocaleTimeString()}` : 'Loading…'}</span></div>
    <div className="admin-telemetry-grid">{servers.map((server) => <div className={`admin-server ${server.online ? 'online' : 'offline'}`} key={server.id}><div className="admin-server-heading"><strong>{server.label}</strong><span className="admin-server-status">{server.online ? '● Online' : '○ Offline'}</span></div><small>{server.ready ? 'Ready' : 'Not ready'} · {server.workers ?? 0}/{server.max_workers} workers</small><small>Omega managed by Ashat Hub SSH provisioning</small></div>)}</div>
  </div>;
}

// ── Overview Tab ──

function OverviewTab({ summary, onNavigate }: { summary: AdminSummary | null; onNavigate: (tab: AdminTab) => void }) {
  return (
    <div className="adm-overview">
      <div className="adm-metric-cards">
        <div className="adm-metric-card">
          <span className="adm-metric-label">Total Users</span>
          <strong className="adm-metric-value">{summary?.users ?? '—'}</strong>
          <small className="adm-metric-sub">{summary?.active_users ?? 0} active · {summary?.disabled_users ?? 0} disabled</small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Live Deployments</span>
          <strong className="adm-metric-value">{summary?.active_deploys ?? '—'}</strong>
          <small className="adm-metric-sub">{summary?.active_projects ?? 0} projects</small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Open Tickets</span>
          <strong className="adm-metric-value">{summary?.open_tickets ?? '—'}</strong>
          <small className="adm-metric-sub">{summary?.open_tickets === 0 ? 'All clear' : 'Needs attention'}</small>
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

function UsersTab({ users, query, onQueryChange, onSearch, onSelectUser, selectedUser, busy, currentUser, onUpdateUser, onBanUser, onDeleteUser }: {
  users: AdminUser[]; query: string; onQueryChange: (q: string) => void; onSearch: () => void;
  onSelectUser: (u: AdminUser | null) => void; selectedUser: AdminUser | null;
  busy: boolean; currentUser: User; onUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onBanUser: (id: string, banned: boolean) => void; onDeleteUser: (id: string) => void;
}) {
  return (
    <div className="adm-panel-layout">
      <div className="adm-panel-list">
        <div className="adm-toolbar">
          <input value={query} onChange={(e) => onQueryChange(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }} placeholder="Search users..." />
          <button type="button" className="secondary-button" onClick={onSearch}>Search</button>
        </div>
        <div className="adm-user-list">
          {users.map((u) => (
            <button key={u.id} type="button" className={selectedUser?.id === u.id ? 'adm-user-row selected' : 'adm-user-row'} onClick={() => onSelectUser(u)}>
              <div className="adm-user-row-info">
                <strong>{u.display_name || u.username}</strong>
                <small>@{u.username} · {u.email}</small>
              </div>
              <span className="adm-user-role">{u.role}</span>
              <span className={u.is_active ? 'adm-status-dot active' : 'adm-status-dot disabled'} />
            </button>
          ))}
          {users.length === 0 && <div className="adm-empty">No users found.</div>}
        </div>
      </div>
      {selectedUser && <UserDetailPanel user={selectedUser} busy={busy} currentUser={currentUser} onUpdate={onUpdateUser} onBan={onBanUser} onDelete={onDeleteUser} onClose={() => onSelectUser(null)} />}
    </div>
  );
}

function UserDetailPanel({ user: u, busy, currentUser, onUpdate, onBan, onDelete, onClose }: {
  user: AdminUser; busy: boolean; currentUser: User; onUpdate: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onBan: (id: string, banned: boolean) => void; onDelete: (id: string) => void; onClose: () => void;
}) {
  const isBanned = Boolean(u.banned_at);
  return (
    <div className="adm-detail-panel">
      <div className="adm-detail-header">
        <h3>{u.display_name || u.username}</h3>
        <button type="button" className="adm-close-btn" onClick={onClose}>×</button>
      </div>
      <p className="adm-detail-email">{u.email}</p>
      <div className="adm-detail-fields">
        <div className="adm-field"><span className="adm-field-label">Status</span><span className={!u.is_active || isBanned ? 'adm-badge err' : 'adm-badge ok'}>{isBanned ? '⊘ Banned' : u.is_active ? '● Active' : '○ Disabled'}</span></div>
        <div className="adm-field"><span className="adm-field-label">Role</span>
          <select value={u.role} disabled={busy || u.id === currentUser.id} onChange={(e) => void onUpdate(u.id, { role: e.target.value })}>
            <option>Member</option><option>Pro</option><option>Admin</option>
          </select>
        </div>
        <div className="adm-field"><span className="adm-field-label">Username</span><span className="adm-field-value mono">@{u.username}</span></div>
        <div className="adm-field"><span className="adm-field-label">User ID</span><span className="adm-field-value mono">{u.id}</span></div>
        {u.email_verified_at && <div className="adm-field"><span className="adm-field-label">Email Verified</span><span className="adm-field-value">{u.email_verified_at}</span></div>}
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
    </div>
  );
}

// ── System Tab ──

function SystemTab({ database, settings, onPush, onUpdate, status }: { database: DatabaseStatus | null; settings: Record<string, string | boolean> | null; onPush: () => void; onUpdate: () => void; status: RepoStatus | null }) {
  return (
    <div className="adm-system">
      <div className="adm-system-grid">
        <section className="adm-panel">
          <span className="eyebrow">Database</span>
          <h3>{database?.version || 'Loading...'}</h3><p className="system-status-ok">● Connected</p>
          <span className="eyebrow">Database Management</span>
          <div className="adm-field"><span className="adm-field-label">Schema Revision</span><span className="adm-field-value">v{database?.migrations?.at(-1)?.[0] ?? '—'}</span></div>
          <div className="adm-field"><span className="adm-field-label">Schema Updates</span><span className="adm-field-value">{database?.migrations?.length ?? 0} applied</span></div>
          <div className="adm-field"><span className="adm-field-label">Update Method</span><span className="adm-field-value">Versioned</span></div>
          <div className="adm-field"><span className="adm-field-label">Direct SQL Console</span><span className="adm-field-value">Disabled</span></div>
          {database?.migrations && database.migrations.length > 0 && (<>
            <span className="eyebrow">Recent Schema Updates</span><div className="adm-migration-list">
              {database.migrations.slice(-5).reverse().map((m) => (
                <span key={m[0]} className={m[2] ? 'adm-migration-ok' : 'adm-migration-fail'}>v{m[0]} · {m[1]}</span>
              ))}
            </div>
          </>)}
        </section>
        <section className="adm-panel">
          <span className="eyebrow">Application Runtime</span>
          <h3>Services</h3><div className="adm-field"><span className="adm-field-label">Backend</span><span className="adm-field-value">Rust · <b className="system-status-ok">● Running</b></span></div><div className="adm-field"><span className="adm-field-label">Frontend</span><span className="adm-field-value">Vite · <b className="system-status-ok">● Available</b></span></div><span className="eyebrow">Security & Configuration</span>
          <div className={`adm-system-updates adm-repo-${status?.ashathub?.state || 'unknown'}`}><span className="adm-field-label">System Updates</span><div className="adm-commit-comparison"><div><span>Live commit</span><code>{status?.ashathub?.live?.slice(0, 8) || '—'}</code></div><div><span>Repo commit</span><code>{status?.ashathub?.repo?.slice(0, 8) || '—'}</code></div></div><div className="adm-system-update-actions"><button type="button" className="secondary-button" onClick={onPush}>Push Changes to GitHub</button><button type="button" className="secondary-button" onClick={onUpdate}>Pull, Build & Restart</button></div></div>
          {settings && Object.entries(settings).map(([key, value]) => (
            <div className="adm-setting-row" key={key}><span>{key.replace(/_/g, ' ')}</span><strong>{typeof value === 'boolean' ? (value ? '✓ Enabled' : '○ Disabled') : String(value)}</strong></div>
          ))}
        </section>
      </div>
    </div>
  );
}

