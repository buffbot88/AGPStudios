import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { API, clearShapeDrifts, request, safeList, safeNumber, safeText, safeTime } from './api';
import { confirmModal, confirmToast, toast } from './toast';
import { maybeCrash } from './devCrash';
import { PosterImage } from './home/PosterImage';
import { UnexpectedDataNotice, UnexpectedDataPanel } from './UnexpectedDataNotice';

type User = { id: string; username: string; email: string; display_name: string; role: string };
type AdminUser = { id: string; username: string; email: string; display_name: string; role: string; is_active: number; banned_at?: number | null; email_verified_at?: string | null; everenfell_s1?: number | boolean };
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
type AdminGame = { slug: string; title: string; summary: string; status: string; tags: string[]; url: string; poster: string; featured: boolean; sort_order?: number };
type ClientErrorStatus = 'new' | 'resolved' | 'muted';
type ClientError = {
  id: number; source?: string; message?: string; detail?: string; path?: string;
  count?: number; occurrences?: string; user_id?: string; created_at?: number;
  triage_status?: string; message_hash?: string; triaged_at?: number; triaged_by?: string;
  muted_at?: number; muted_by?: string;
};
const CLIENT_ERROR_FILTERS: { id: ClientErrorStatus | 'all'; label: string }[] = [
  { id: 'new', label: 'New' },
  { id: 'resolved', label: 'Resolved' },
  { id: 'muted', label: 'Muted' },
  { id: 'all', label: 'All' },
];
type AdminTab = 'overview' | 'users' | 'games' | 'payments' | 'system';
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
    return ['overview', 'users', 'games', 'payments', 'system'].includes(value) ? value : 'overview';
  });
  const [summary, setSummary] = useState<AdminSummary | null>(null);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [database, setDatabase] = useState<DatabaseStatus | null>(null);
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
  // True once the first load has answered, so "arrived but unusable" renders a
  // notice instead of the panels' Loading… placeholders forever.
  const [loaded, setLoaded] = useState(false);
  const [clientErrors, setClientErrors] = useState<ClientError[]>([]);
  const [clientErrorsError, setClientErrorsError] = useState<string | null>(null);
  const [clientErrorsBusy, setClientErrorsBusy] = useState(false);
  // Which triage state the panel is showing. `new` is the default so triaged
  // rows stop looking like fresh news.
  const [clientErrorsFilter, setClientErrorsFilter] = useState<ClientErrorStatus | 'all'>('new');
  // The big load() reads the filter through a ref: it must stay consistent with
  // what the panel shows without re-running the whole admin fetch on every chip.
  const clientErrorsFilterRef = useRef<ClientErrorStatus | 'all'>(clientErrorsFilter);
  clientErrorsFilterRef.current = clientErrorsFilter;

  const loadClientErrors = useCallback(async (status: ClientErrorStatus | 'all' = clientErrorsFilterRef.current) => {
    setClientErrorsBusy(true);
    try {
      const data = await request<{ errors: ClientError[] }>(`${API}/admin/client-errors?status=${status}`);
      setClientErrors(safeList<ClientError>(data.errors));
      setClientErrorsError(null);
    } catch (reason) {
      setClientErrors([]);
      setClientErrorsError(reason instanceof Error ? reason.message : 'Client errors unavailable');
    } finally {
      setClientErrorsBusy(false);
    }
  }, []);

  useEffect(() => { const timer = window.setTimeout(() => { void loadClientErrors(clientErrorsFilter); }, 0); return () => window.clearTimeout(timer); }, [clientErrorsFilter, loadClientErrors]);

  /** Row-level triage: 'resolved' records the judgement, 'new' reopens it. */
  async function triageClientError(id: number, status: ClientErrorStatus) {
    setClientErrorsBusy(true);
    try {
      await request(`${API}/admin/client-errors/status`, { method: 'POST', body: JSON.stringify({ id, status }) });
      toast(status === 'resolved' ? 'Report marked resolved.' : 'Report reopened.', 'success');
      await loadClientErrors();
    } catch (reason) {
      setClientErrorsError(reason instanceof Error ? reason.message : 'Unable to update the report');
    } finally { setClientErrorsBusy(false); }
  }

  /**
   * Message-level mute (migration 0006): covers every report of this message,
   * including future ones. The row only identifies which message to mute.
   */
  async function muteClientErrorMessage(id: number, muted: boolean) {
    setClientErrorsBusy(true);
    try {
      await request(`${API}/admin/client-errors/mute`, { method: 'POST', body: JSON.stringify({ id, muted }) });
      toast(muted ? 'Message muted. Reports of it stay out of New.' : 'Message unmuted.', 'success');
      await loadClientErrors();
    } catch (reason) {
      setClientErrorsError(reason instanceof Error ? reason.message : 'Unable to change the mute');
    } finally { setClientErrorsBusy(false); }
  }

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
      const [summaryResult, usersResult, databaseResult, settingsResult, repoResult, gamesResult, clientErrorsResult] = await Promise.allSettled([
        request<AdminSummary>(`${API}/admin/summary`),
        request<{ users: AdminUser[] }>(`${API}/admin/users${search}`),
        request<DatabaseStatus>(`${API}/admin/database/status`),
        request<Record<string, string | boolean>>(`${API}/admin/settings`),
        request<RepoStatus>(`${API}/admin/repo-status`),
        request<{ games: AdminGame[] }>(`${API}/admin/games`),
        request<{ errors: ClientError[] }>(`${API}/admin/client-errors?status=${clientErrorsFilterRef.current}`),
      ]);
      // Fresh payload: the notice describes the latest response, not history
      // (including placeholders the loading render legitimately coerced).
      clearShapeDrifts();
      if (summaryResult.status === 'fulfilled') setSummary(summaryResult.value);
      if (usersResult.status === 'fulfilled') setUsers(safeList(usersResult.value.users));
      if (databaseResult.status === 'fulfilled') setDatabase(databaseResult.value);
      if (settingsResult.status === 'fulfilled') setSettings(settingsResult.value);
      if (repoResult.status === 'fulfilled') setRepoStatus(repoResult.value);
      if (gamesResult.status === 'fulfilled') setGames(safeList<AdminGame>(gamesResult.value.games));
      if (clientErrorsResult.status === 'fulfilled') setClientErrors(safeList<ClientError>(clientErrorsResult.value.errors));
      const firstRejected = [summaryResult, usersResult, databaseResult, settingsResult, repoResult, gamesResult, clientErrorsResult].find(
        (outcome): outcome is PromiseRejectedResult => outcome.status === 'rejected',
      );
      if (firstRejected) {
        const reason = firstRejected.reason;
        setError(reason instanceof Error ? reason.message : 'Admin data unavailable');
      } else {
        setError(null);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Admin data unavailable'); }
    finally { setLoaded(true); }
  }, [query, user]);

  useEffect(() => { void load(); }, [load]);
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

  async function updateUser(userId: string, body: { role?: string; is_active?: boolean }) {
    setBusy(true); setError(null);
    try {
      const endpoint = body.role ? `${API}/admin/users/role` : `${API}/admin/users/status`;
      await request(endpoint, {
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

  async function setEverenfellS1(userId: string, enabled: boolean) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/everenfell/s1`, { method: 'POST', body: JSON.stringify({ user_id: userId, enabled }) });
      toast(enabled ? 'Everenfell S1 access granted.' : 'Everenfell S1 access revoked.', 'success');
      await load();
      if (selectedUser?.id === userId) {
        const result = await request<{ users: AdminUser[] }>(`${API}/admin/users?q=${encodeURIComponent(userId)}`);
        const fresh = safeList<AdminUser>(result.users).find((item) => item?.id === userId);
        if (fresh) setSelectedUser(fresh);
      }
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Everenfell access update failed'); }
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
    { id: 'payments', label: 'Payments' },
    { id: 'system', label: 'System' },
  ];

  return (
    <section className="member-section adm-surface" aria-label="AGP admin control panel">
      <div className="section-heading">
        <div><span className="eyebrow">AGP Studios</span><h2>Admin Control</h2></div>
        <a className="adm-back-link" href="/">← Back to AGP Studios</a>
      </div>
      {error && <p className="workspace-error" role="alert">{error}</p>}
      <UnexpectedDataNotice onRetry={() => { setLoaded(false); void load(); }} />

      <nav className="adm-tabs" aria-label="Admin navigation">
        {tabs.map((t) => (
          <button key={t.id} type="button" className={tab === t.id ? 'adm-tab selected' : 'adm-tab'} onClick={() => { setTab(t.id); window.history.replaceState({}, '', `/admin#${t.id}`); setSelectedUser(null); }}>{t.label}</button>
        ))}
      </nav>

      {tab === 'overview' && <OverviewTab summary={summary} onNavigate={setTab} />}
      {tab === 'users' && <UsersTab users={users} query={query} onQueryChange={setQuery} onSearch={load} onSelectUser={selectUser} selectedUser={selectedUser} busy={busy} currentUser={user} onUpdateUser={updateUser} onSetEverenfellS1={setEverenfellS1} onBanUser={banUser} onDeleteUser={deleteUser} moderation={moderation} moderationError={moderationError} onRefreshModeration={() => selectedUser && void loadModeration(selectedUser.id)} />}
      {tab === 'games' && <GamesTab games={games} busy={busy} onUpdatePoster={updateGamePoster} />}
      {tab === 'payments' && <PaymentsTab />}
      {tab === 'system' && <SystemTab loaded={loaded} database={database} settings={settings} onPush={pushGithub} onUpdate={systemUpdate} status={repoStatus} busy={busy} onApplyMigrations={applyMigrations} clientErrors={clientErrors} clientErrorsError={clientErrorsError} clientErrorsBusy={clientErrorsBusy} clientErrorsFilter={clientErrorsFilter} onFilterClientErrors={setClientErrorsFilter} onRefreshClientErrors={loadClientErrors} onTriageClientError={triageClientError} onMuteClientErrorMessage={muteClientErrorMessage} />}
    </section>
  );
}

type PaymentSettings = { paypal_email: string; paypal_client_id: string; client_secret_configured: boolean; environment: 'sandbox' | 'live'; encryption_key_configured: boolean };

function PaymentsTab() {
  const [settings, setSettings] = useState<PaymentSettings | null>(null);
  const [secret, setSecret] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  async function loadSettings() {
    try {
      const data = await request<PaymentSettings>(`${API}/admin/payments/settings`);
      setSettings({ ...data, paypal_email: data.paypal_email || 'buffbot88@gmail.com' });
      setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Payment settings unavailable'); }
  }
  useEffect(() => { void loadSettings(); }, []);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings) return;
    setBusy(true); setError(''); setMessage('');
    try {
      await request(`${API}/admin/payments/settings`, {
        method: 'POST',
        body: JSON.stringify({ ...settings, paypal_client_secret: secret }),
      });
      setSecret(''); setMessage('PayPal settings saved.'); await loadSettings();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to save payment settings'); }
    finally { setBusy(false); }
  }

  if (!settings) return <section className="adm-panel"><p>{error || 'Loading payment settings…'}</p></section>;
  return <section className="adm-panel adm-payments"><span className="eyebrow">ACoins checkout</span><h3>PayPal settings</h3><p>Choose Sandbox while testing. Live purchases credit ACoins only after PayPal confirms capture.</p>{!settings.encryption_key_configured && <p className="workspace-error">Set payment_encryption_key in the API's config.json before saving the client secret.</p>}<form className="adm-payment-form" onSubmit={(event) => void save(event)}><label>PayPal merchant email<input type="email" required value={settings.paypal_email} onChange={(event) => setSettings({ ...settings, paypal_email: event.target.value })} /></label><label>PayPal app Client ID<input required value={settings.paypal_client_id} onChange={(event) => setSettings({ ...settings, paypal_client_id: event.target.value })} /></label><label>PayPal app Secret<input type="password" autoComplete="new-password" required={!settings.client_secret_configured} placeholder={settings.client_secret_configured ? 'Saved — leave blank to keep current secret' : 'Enter app secret'} value={secret} onChange={(event) => setSecret(event.target.value)} /></label><label>Environment<select value={settings.environment} onChange={(event) => setSettings({ ...settings, environment: event.target.value as 'sandbox' | 'live' })}><option value="sandbox">Sandbox</option><option value="live">Live</option></select></label><button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save PayPal settings'}</button></form>{message && <p role="status">{message}</p>}{error && <p className="workspace-error" role="alert">{error}</p>}</section>;
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
          {/* Guarded like the value above: coercing a not-yet-loaded summary is
              a placeholder, and a placeholder is not shape drift. */}
          <small className="adm-metric-sub">
            {summary ? `${safeNumber(summary.active_users)} active · ${safeNumber(summary.disabled_users)} disabled` : '—'}
          </small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Live Deployments</span>
          <strong className="adm-metric-value">{summary ? safeNumber(summary.active_deploys).toLocaleString() : '—'}</strong>
          <small className="adm-metric-sub">{summary ? `${safeNumber(summary.active_projects)} projects` : '—'}</small>
        </div>
        <div className="adm-metric-card">
          <span className="adm-metric-label">Open Tickets</span>
          <strong className="adm-metric-value">{summary ? safeNumber(summary.open_tickets).toLocaleString() : '—'}</strong>
          <small className="adm-metric-sub">
            {summary ? (safeNumber(summary.open_tickets) === 0 ? 'All clear' : 'Needs attention') : '—'}
          </small>
        </div>
      </div>
      <div className="adm-overview-actions">
        <button type="button" className="adm-action-card" onClick={() => onNavigate('users')}><span className="adm-action-icon">♙</span><span><strong>Manage Users</strong><small>Accounts, roles, and access</small></span><b>→</b></button>
        <button type="button" className="adm-action-card" onClick={() => onNavigate('system')}><span className="adm-action-icon">⚙</span><span><strong>System Health</strong><small>Database and configuration</small></span><b>→</b></button>
      </div>

    </div>
  );
}

// ── Users Tab ──

function UsersTab({ users, query, onQueryChange, onSearch, onSelectUser, selectedUser, busy, currentUser, onUpdateUser, onSetEverenfellS1, onBanUser, onDeleteUser, moderation, moderationError, onRefreshModeration }: {
  users: AdminUser[]; query: string; onQueryChange: (q: string) => void; onSearch: () => void;
  onSelectUser: (u: AdminUser | null) => void; selectedUser: AdminUser | null;
  busy: boolean; currentUser: User; onUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onSetEverenfellS1: (id: string, enabled: boolean) => void;
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
      {selectedUser && <UserDetailPanel user={selectedUser} busy={busy} currentUser={currentUser} onUpdate={onUpdateUser} onSetEverenfellS1={onSetEverenfellS1} onBan={onBanUser} onDelete={onDeleteUser} onClose={() => onSelectUser(null)} moderation={moderation} moderationError={moderationError} onRefreshModeration={onRefreshModeration} />}
    </div>
  );
}

function UserDetailPanel({ user: u, busy, currentUser, onUpdate, onSetEverenfellS1, onBan, onDelete, onClose, moderation, moderationError, onRefreshModeration }: {
  user: AdminUser; busy: boolean; currentUser: User; onUpdate: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onSetEverenfellS1: (id: string, enabled: boolean) => void;
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
        <div className="adm-field"><span className="adm-field-label">Everenfell S1</span><span className="adm-badge">{u.everenfell_s1 ? 'Entitled' : 'Not entitled'}</span></div>
      </div>
      <div className="adm-detail-actions">
        <button type="button" className={u.everenfell_s1 ? 'adm-btn-danger' : 'adm-btn-ok'} disabled={busy || !u.is_active || u.role.trim().toLowerCase() !== 'admin'} onClick={() => onSetEverenfellS1(u.id, !Boolean(u.everenfell_s1))}>
          {u.everenfell_s1 ? 'Revoke Everenfell S1' : 'Grant Everenfell S1'}
        </button>
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
 * Applied/pending migrations, read from the database/status response the System tab already loads.
 */
function MigrationsPanel({ loaded, migrations, busy, onApplyMigrations }: {
  loaded: boolean; migrations: MigrationSummary | undefined; busy: boolean;
  onApplyMigrations: (baseline: boolean) => void;
}) {
  if (!migrations) {
    return (
      <section className="adm-panel adm-migrations">
        <span className="eyebrow">Schema migrations</span>
        <h3>{loaded ? 'Not reported' : 'Loading…'}</h3>
        {/* A status response that arrived without a migrations object is drift,
            not a pending request — saying "Loading…" there never resolves. */}
        <p className="muted">
          {loaded
            ? 'The database status response carried no migration state, so pending files can\u2019t be listed. Use the migration runner on the host.'
            : 'Migration state arrives with the database status.'}
        </p>
        {loaded && <UnexpectedDataPanel what="The migration summary" />}
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
        existing files are recorded as applied instead of executed.
      </p>}
      {migrations.error && <p className="workspace-error" role="alert">{migrations.error}</p>}

      {drifted.length > 0 && <p className="adm-migrations-note">
        Changed on disk since they were applied (not re-run): {drifted.map((name) => safeText(name)).join(', ')}.
      </p>}
      {missing.length > 0 && <p className="adm-migrations-note">
        Recorded but no longer in the repo: {missing.map((name) => safeText(name)).join(', ')}.
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
              <small>{safeNumber(entry.statements)} stmt · {entry.applied_at ? safeTime(entry.applied_at) : 'unknown'}</small>
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

// ── Client errors panel (System tab) ──

/**
 * Uncaught browser failures reported by visitors. Reported by the client, so it is a signal to investigate, never a fact to trust.
 */
function ClientErrorsPanel({ errors, error, busy, status, onStatusChange, onRefresh, onTriage, onMute }: {
  errors: ClientError[]; error: string | null; busy: boolean;
  status: ClientErrorStatus | 'all'; onStatusChange: (status: ClientErrorStatus | 'all') => void;
  onRefresh: () => void;
  onTriage: (id: number, status: ClientErrorStatus) => void;
  onMute: (id: number, muted: boolean) => void;
}) {
  const heading = status === 'all' ? `${errors.length} reported` : `${errors.length} ${status}`;
  const empty = status === 'new' ? 'No new client errors reported.' : `Nothing ${status}.`;
  return (
    <section className="adm-panel adm-client-errors">
      <div className="adm-migrations-heading">
        <div>
          <span className="eyebrow">Client errors</span>
          <h3>{errors.length ? heading : empty.replace('.', '')}</h3>
        </div>
        <button type="button" className="secondary-button" onClick={onRefresh} disabled={busy}>
          {busy ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>
      <div className="adm-client-error-filters" role="tablist" aria-label="Client error triage state">
        {CLIENT_ERROR_FILTERS.map((filter) => (
          <button
            key={filter.id}
            type="button"
            role="tab"
            aria-selected={status === filter.id}
            className={`adm-client-error-filter${status === filter.id ? ' selected' : ''}`}
            onClick={() => onStatusChange(filter.id)}
          >
            {filter.label}
          </button>
        ))}
      </div>
      <p className="adm-client-errors-note">
        Uncaught failures from visitors&apos; browsers, newest first. Entries are client-reported, so they may repeat
        or be junk — the count shows how many occurrences arrived in one burst. Resolving covers this report;
        muting covers every report of the same message, now and in future.
      </p>
      {error && <p className="workspace-error" role="alert">{error}</p>}
      <ul className="adm-client-error-list">
        {errors.map((entry) => {
          const id = safeNumber(entry.id);
          const triage = safeText(entry.triage_status) || 'new';
          const muted = triage === 'muted';
          return (
            <li key={id}>
              <div className="adm-client-error-head">
                <span className={`adm-badge${safeText(entry.source) === 'boundary' ? ' err' : ''}`}>
                  {safeText(entry.source) || 'unknown'}
                </span>
                <strong>{safeText(entry.message)}</strong>
                {safeNumber(entry.count) > 1 && <span className="adm-client-error-count">×{safeNumber(entry.count)}</span>}
                <span className={`adm-badge adm-triage-${triage}`}>{triage}</span>
              </div>
              {safeText(entry.detail) && <small className="adm-client-error-detail">{safeText(entry.detail)}</small>}
              <small className="adm-moderation-meta">
                {safeText(entry.path) || '/'} · {entry.created_at ? safeTime(entry.created_at) : 'unknown'}
                {safeText(entry.user_id) ? ` · user ${safeText(entry.user_id)}` : ' · signed out'}
                {safeNumber(entry.triaged_at) > 0 && ` · resolved by ${safeText(entry.triaged_by) || 'admin'} ${safeTime(safeNumber(entry.triaged_at))}`}
                {muted && ` · message muted by ${safeText(entry.muted_by) || 'admin'}${safeNumber(entry.muted_at) > 0 ? ` ${safeTime(safeNumber(entry.muted_at))}` : ''}`}
              </small>
              <div className="adm-client-error-actions">
                {triage === 'resolved'
                  ? <button type="button" className="secondary-button" onClick={() => onTriage(id, 'new')} disabled={busy}>Reopen</button>
                  : <button type="button" className="secondary-button" onClick={() => onTriage(id, 'resolved')} disabled={busy}>Mark resolved</button>}
                {muted
                  ? <button type="button" className="secondary-button" onClick={() => onMute(id, false)} disabled={busy}>Unmute message</button>
                  : <button type="button" className="secondary-button" onClick={() => onMute(id, true)} disabled={busy}>Mute this message</button>}
              </div>
            </li>
          );
        })}
        {errors.length === 0 && !error && <li className="adm-empty">{empty}</li>}
      </ul>
    </section>
  );
}

// ── System Tab ──

function SystemTab({ loaded, database, settings, onPush, onUpdate, status, busy, onApplyMigrations, clientErrors, clientErrorsError, clientErrorsBusy, clientErrorsFilter, onFilterClientErrors, onRefreshClientErrors, onTriageClientError, onMuteClientErrorMessage }: {
  loaded: boolean;
  database: DatabaseStatus | null; settings: Record<string, string | boolean> | null;
  onPush: () => void; onUpdate: () => void; status: RepoStatus | null;
  busy: boolean; onApplyMigrations: (baseline: boolean) => void;
  clientErrors: ClientError[]; clientErrorsError: string | null; clientErrorsBusy: boolean;
  clientErrorsFilter: ClientErrorStatus | 'all';
  onFilterClientErrors: (status: ClientErrorStatus | 'all') => void;
  onRefreshClientErrors: (status?: ClientErrorStatus | 'all') => void;
  onTriageClientError: (id: number, status: ClientErrorStatus) => void;
  onMuteClientErrorMessage: (id: number, muted: boolean) => void;
}) {
  return (
    <div className="adm-system">
      <MigrationsPanel loaded={loaded} migrations={database?.migrations} busy={busy} onApplyMigrations={onApplyMigrations} />
      <ClientErrorsPanel errors={clientErrors} error={clientErrorsError} busy={clientErrorsBusy} status={clientErrorsFilter} onStatusChange={onFilterClientErrors} onRefresh={() => onRefreshClientErrors()} onTriage={onTriageClientError} onMute={onMuteClientErrorMessage} />
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
          <h3>Services</h3><div className="adm-field"><span className="adm-field-label">Backend</span><span className="adm-field-value">Python · <b className="system-status-ok">● Running</b></span></div><div className="adm-field"><span className="adm-field-label">Frontend</span><span className="adm-field-value">Vite · <b className="system-status-ok">● Available</b></span></div><span className="eyebrow">Security & Configuration</span>
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

