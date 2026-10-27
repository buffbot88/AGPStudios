import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { API, clearShapeDrifts, request, safeList, safeNumber, safeText, safeTime } from './api';
import { confirmModal, confirmToast, toast } from './toast';
import { maybeCrash } from './devCrash';
import { burstConfetti } from './confetti';
import { PosterImage } from './home/PosterImage';
import { UnexpectedDataNotice, UnexpectedDataPanel } from './UnexpectedDataNotice';

type User = { id: string; username: string; email: string; display_name: string; role: string };
type AdminUser = { id: string; username: string; email: string; display_name: string; role: string; is_active: number; banned_at?: number | null; email_verified_at?: string | null; everenfell_s1?: number | boolean };
type AdminSummary = {
  users: number; active_users: number; disabled_users: number;
  open_tickets: number;
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
type AdminTab = 'overview' | 'users' | 'games' | 'website' | 'payments' | 'system';
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
    return ['overview', 'users', 'games', 'website', 'payments', 'system'].includes(value) ? value : 'overview';
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

  async function updateGame(slug: string, fields: Record<string, unknown>) {
    setBusy(true); setError(null);
    try {
      await request(`${API}/admin/games/${encodeURIComponent(slug)}`, { method: 'PUT', body: JSON.stringify(fields) });
      toast('Game updated.', 'success');
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Game update failed'); }
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
    { id: 'website', label: 'Website' },
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

      {tab === 'overview' && <OverviewTab summary={summary} games={games} onNavigate={setTab} />}
      {tab === 'users' && <UsersTab users={users} query={query} onQueryChange={setQuery} onSearch={load} onSelectUser={selectUser} selectedUser={selectedUser} busy={busy} currentUser={user} onUpdateUser={updateUser} onSetEverenfellS1={setEverenfellS1} onBanUser={banUser} onDeleteUser={deleteUser} moderation={moderation} moderationError={moderationError} onRefreshModeration={() => selectedUser && void loadModeration(selectedUser.id)} />}
      {tab === 'games' && <GamesTab games={games} busy={busy} onUpdateGame={updateGame} />}
      {tab === 'website' && <WebsiteTab />}
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

function GamesTab({ games, busy, onUpdateGame }: { games: AdminGame[]; busy: boolean; onUpdateGame: (slug: string, fields: Record<string, unknown>) => void }) {
  type GameDraft = { title: string; summary: string; status: string; url: string; featured: boolean; sort_order: string; tags: string; poster: string };
  const [drafts, setDrafts] = useState<Record<string, Partial<GameDraft>>>({});
  const blank: GameDraft = { title: '', summary: '', status: '', url: '', featured: false, sort_order: '', tags: '', poster: '' };
  const draft = (game: AdminGame): GameDraft => ({
    title: safeText(game.title), summary: safeText(game.summary), status: safeText(game.status),
    url: safeText(game.url), featured: Boolean(game.featured),
    sort_order: String(game.sort_order ?? ''),
    tags: safeList<string>(game.tags).join(', '), poster: safeText(game.poster),
    ...drafts[safeText(game.slug)],
  });
  const set = (slug: string, patch: Partial<GameDraft>) =>
    setDrafts((prev) => ({ ...prev, [slug]: { ...prev[slug], ...patch } }));
  function save(game: AdminGame) {
    const slug = safeText(game.slug);
    const current = draft(game);
    onUpdateGame(slug, {
      title: current.title.trim(), summary: current.summary.trim(), status: current.status,
      url: current.url.trim(), featured: current.featured,
      sort_order: current.sort_order.trim() === '' ? 0 : Number(current.sort_order),
      tags: current.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
      poster: current.poster.trim(),
    });
  }
  return (
    <div className="adm-panel-layout">
      <div className="adm-games-list">
        <div className="adm-toolbar"><span className="eyebrow">Games</span><span className="refresh-state">Edits apply live — no redeploy needed</span></div>
        {safeList<AdminGame>(games).filter(Boolean).map((game) => {
          const slug = safeText(game.slug);
          const current = draft(game);
          return (
            <form className="adm-game-row adm-game-edit" key={slug} onSubmit={(e) => { e.preventDefault(); save(game); }}>
              <div className="adm-game-thumb">
                <PosterImage
                  src={current.poster}
                  alt=""
                  fallbackClass="thumb-fallback fallback-in-development"
                  fallback={<span aria-hidden>◎</span>}
                />
              </div>
              <div className="adm-game-fields">
                <div className="adm-game-grid">
                  <label>Title<input value={current.title} onChange={(e) => set(slug, { title: e.target.value })} required maxLength={120} /></label>
                  <label>Slug<span className="adm-field-value mono">{slug}</span></label>
                  <label className="adm-span">Summary<textarea value={current.summary} onChange={(e) => set(slug, { summary: e.target.value })} required maxLength={500} rows={2} /></label>
                  <label>Status<select value={current.status} onChange={(e) => set(slug, { status: e.target.value })}><option value="live">live</option><option value="in-development">in-development</option><option value="coming-soon">coming-soon</option></select></label>
                  <label>Sort<input type="number" min={0} max={1000} value={current.sort_order} onChange={(e) => set(slug, { sort_order: e.target.value })} /></label>
                  <label className="adm-span">URL<input value={current.url} onChange={(e) => set(slug, { url: e.target.value })} placeholder="https://…" spellCheck={false} maxLength={500} /></label>
                  <label className="adm-span">Tags (comma separated)<input value={current.tags} onChange={(e) => set(slug, { tags: e.target.value })} placeholder="Cozy, Exploration" maxLength={200} /></label>
                  <label className="adm-span">Poster URL<input className="adm-game-poster-input" value={current.poster} onChange={(e) => set(slug, { poster: e.target.value })} placeholder="/poster.svg or https://…" spellCheck={false} /></label>
                  <label className="adm-check"><input type="checkbox" checked={current.featured} onChange={(e) => set(slug, { featured: e.target.checked })} /> Featured on homepage</label>
                </div>
                <div className="adm-game-actions"><button type="submit" className="secondary-button" disabled={busy}>Save game</button></div>
              </div>
            </form>
          );
        })}
        {games.length === 0 && <div className="adm-empty">No games configured.</div>}
      </div>
    </div>
  );
}

// ── Website Tab: homepage content, registration controls, news curation ──

type CuratedNews = { id: string; title: string; summary: string; url: string; published_at: number; created_at?: number };

const HOMEPAGE_FIELDS: { key: string; label: string; max: number }[] = [
  { key: 'featured_video.embed_url', label: 'Featured video embed URL', max: 500 },
  { key: 'featured_video.watch_url', label: 'Featured video watch URL', max: 500 },
  { key: 'footer.tagline', label: 'Footer tagline', max: 160 },
  { key: 'footer.discord_url', label: 'Discord invite URL', max: 500 },
  { key: 'footer.youtube_url', label: 'YouTube channel URL', max: 500 },
];

const REGISTER_FIELDS: { key: string; label: string; hint: string; min: number; max: number }[] = [
  { key: 'register.challenge_difficulty', label: 'Challenge difficulty (leading zero hex chars)', hint: 'Higher = slower for humans and bots alike. 4 is ~seconds in a browser.', min: 1, max: 8 },
  { key: 'register.budget', label: 'Registrations allowed per window, per IP', hint: 'Bot brake. 5 is strict; raise only if real users hit it.', min: 1, max: 100 },
  { key: 'register.window_seconds', label: 'Rate-limit window (seconds)', hint: 'How long the per-IP budget lasts.', min: 60, max: 3600 },
  { key: 'register.max_accounts_per_ip', label: 'Lifetime accounts allowed per IP', hint: 'Anti-farming: 2 means one person gets two accounts, ever, from one address.', min: 1, max: 100 },
  { key: 'referral.bonus_acoins', label: 'Referral bonus (ACoins each side)', hint: 'Paid to the referrer and the new account on signup. 0 disables payouts.', min: 0, max: 10000 },
  { key: 'referral.uses_per_day', label: 'Paid referral uses per link per day', hint: 'Each link pays this many bonuses per rolling 24h; extra signups still register.', min: 1, max: 1000 },
];

function WebsiteTab() {
  const [settings, setSettings] = useState<Record<string, string> | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [news, setNews] = useState<CuratedNews[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [adding, setAdding] = useState(false);
  const [newItem, setNewItem] = useState({ title: '', summary: '', url: '' });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editItem, setEditItem] = useState({ title: '', summary: '', url: '' });

  const loadAll = useCallback(async () => {
    try {
      const [siteResult, newsResult] = await Promise.all([
        request<{ settings: Record<string, string | number> }>(`${API}/admin/site-settings`),
        request<{ news: CuratedNews[] }>(`${API}/admin/news`),
      ]);
      clearShapeDrifts();
      const normalized: Record<string, string> = {};
      for (const [key, value] of Object.entries(siteResult.settings ?? {})) normalized[key] = String(value);
      setSettings(normalized);
      setNews(safeList<CuratedNews>(newsResult.news));
      setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Website settings unavailable'); }
  }, []);

  useEffect(() => { void loadAll(); }, [loadAll]);

  const value = (key: string) => drafts[key] ?? settings?.[key] ?? '';

  async function saveKeys(keys: string[], label: string) {
    const patch: Record<string, string> = {};
    for (const key of keys) patch[key] = (drafts[key] ?? settings?.[key] ?? '').trim();
    setBusy(true); setError(''); setMessage('');
    try {
      await request(`${API}/admin/site-settings`, { method: 'PUT', body: JSON.stringify({ settings: patch }) });
      setSettings((prev) => ({ ...prev, ...patch }));
      setDrafts((prev) => { const next = { ...prev }; for (const key of keys) delete next[key]; return next; });
      setMessage(`${label} saved — live on the homepage now.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Save failed'); }
    finally { setBusy(false); }
  }

  async function addNews(event: FormEvent) {
    event.preventDefault();
    if (!newItem.title.trim() || !newItem.summary.trim()) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await request<{ id: string }>(`${API}/admin/news`, {
        method: 'POST',
        body: JSON.stringify({ title: newItem.title.trim(), summary: newItem.summary.trim(), url: newItem.url.trim() || '#news' }),
      });
      setNewItem({ title: '', summary: '', url: '' });
      setAdding(false);
      setMessage('News item pinned.');
      void loadAll();
      void result;
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Add failed'); }
    finally { setBusy(false); }
  }

  function startEdit(item: CuratedNews) {
    setEditingId(safeText(item.id));
    setEditItem({ title: safeText(item.title), summary: safeText(item.summary), url: safeText(item.url) });
  }

  async function saveEdit(id: string) {
    setBusy(true); setError(''); setMessage('');
    try {
      await request(`${API}/admin/news/${encodeURIComponent(id)}`, {
        method: 'PUT',
        body: JSON.stringify({ title: editItem.title.trim(), summary: editItem.summary.trim(), url: editItem.url.trim() || '#news' }),
      });
      setEditingId(null);
      setMessage('News item updated.');
      void loadAll();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Update failed'); }
    finally { setBusy(false); }
  }

  async function deleteNews(id: string, title: string) {
    if (!(await confirmModal({ title: 'Remove this news item?', message: `"${title}" will no longer appear in Studio News.`, confirmLabel: 'Remove', requireText: 'DELETE' }))) return;
    setBusy(true); setError(''); setMessage('');
    try {
      await request(`${API}/admin/news/${encodeURIComponent(id)}`, { method: 'DELETE' });
      setMessage('News item removed.');
      void loadAll();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Delete failed'); }
    finally { setBusy(false); }
  }

  if (!settings) return <section className="adm-panel"><p>{error || 'Loading website settings…'}</p></section>;

  return (
    <div className="adm-website">
      {message && <p role="status" className="auth-success">{message}</p>}
      {error && <p className="workspace-error" role="alert">{error}</p>}
      <section className="adm-panel">
        <span className="eyebrow">Homepage</span>
        <h3>Featured video & footer</h3>
        <p className="muted">Edits apply live on the next homepage load — no redeploy needed.</p>
        <div className="adm-form-grid">
          {HOMEPAGE_FIELDS.map((field) => (
            <label key={field.key}>{field.label}
              <input value={value(field.key)} maxLength={field.max} spellCheck={false}
                onChange={(e) => setDrafts((prev) => ({ ...prev, [field.key]: e.target.value }))} />
            </label>
          ))}
        </div>
        <button type="button" className="secondary-button" disabled={busy} onClick={() => void saveKeys(HOMEPAGE_FIELDS.map((f) => f.key), 'Homepage content')}>Save homepage</button>
      </section>
      <section className="adm-panel">
        <span className="eyebrow">Registration</span>
        <h3>Bot defenses</h3>
        <p className="muted">Proof-of-work difficulty and the per-IP registration budget take effect immediately for new signups.</p>
        <div className="adm-form-grid">
          {REGISTER_FIELDS.map((field) => (
            <label key={field.key}>{field.label}<small>{field.hint}</small>
              <input type="number" min={field.min} max={field.max} value={value(field.key)}
                onChange={(e) => setDrafts((prev) => ({ ...prev, [field.key]: e.target.value }))} />
            </label>
          ))}
        </div>
        <button type="button" className="secondary-button" disabled={busy} onClick={() => void saveKeys(REGISTER_FIELDS.map((f) => f.key), 'Registration controls')}>Save registration controls</button>
      </section>
      <section className="adm-panel">
        <div className="adm-toolbar"><div><span className="eyebrow">Studio news</span><h3>Pinned items</h3></div><button type="button" className="secondary-button" onClick={() => setAdding((open) => !open)}>{adding ? 'Close' : 'Pin news item'}</button></div>
        <p className="muted">Pinned rows merge with recent GitHub commits on the homepage, newest first.</p>
        {adding && (
          <form className="adm-form-grid" onSubmit={(e) => void addNews(e)}>
            <label>Title<input value={newItem.title} maxLength={140} required onChange={(e) => setNewItem({ ...newItem, title: e.target.value })} /></label>
            <label>Link URL<input value={newItem.url} maxLength={500} spellCheck={false} placeholder="#news" onChange={(e) => setNewItem({ ...newItem, url: e.target.value })} /></label>
            <label className="adm-span">Summary<textarea value={newItem.summary} maxLength={500} required rows={2} onChange={(e) => setNewItem({ ...newItem, summary: e.target.value })} /></label>
            <div><button type="submit" className="secondary-button" disabled={busy}>Pin item</button></div>
          </form>
        )}
        <ol className="adm-news-list">
          {news.map((item) => (
            <li key={safeText(item.id)}>
              {editingId === safeText(item.id) ? (
                <div className="adm-form-grid">
                  <label>Title<input value={editItem.title} maxLength={140} required onChange={(e) => setEditItem({ ...editItem, title: e.target.value })} /></label>
                  <label>Link URL<input value={editItem.url} maxLength={500} spellCheck={false} onChange={(e) => setEditItem({ ...editItem, url: e.target.value })} /></label>
                  <label className="adm-span">Summary<textarea value={editItem.summary} maxLength={500} required rows={2} onChange={(e) => setEditItem({ ...editItem, summary: e.target.value })} /></label>
                  <div className="adm-row-actions">
                    <button type="button" className="secondary-button" disabled={busy} onClick={() => void saveEdit(safeText(item.id))}>Save</button>
                    <button type="button" className="secondary-button" disabled={busy} onClick={() => setEditingId(null)}>Cancel</button>
                  </div>
                </div>
              ) : (
                <><div className="adm-news-text"><strong>{safeText(item.title)}</strong><small>{safeText(item.summary)}</small></div>
                <div className="adm-row-actions">
                  <button type="button" className="secondary-button" disabled={busy} onClick={() => startEdit(item)}>Edit</button>
                  <button type="button" className="secondary-button" disabled={busy} onClick={() => void deleteNews(safeText(item.id), safeText(item.title))}>Remove</button>
                </div></>
              )}
            </li>
          ))}
        </ol>
        {news.length === 0 && <div className="adm-empty">No pinned items — the homepage shows GitHub commits only.</div>}
      </section>
    </div>
  );
}

// ── Live Wall Stats ──

type WallStats = {
  signups: { today: number; week: number; month: number; total: number };
  profits_cents: { day: number; week: number; month: number; year: number };
  online: { website: number; paws: number | null };
  games: { slug: string; title: string; status: string; online: number | null; entitlements: number | null; acoins_spent: number; transactions: number }[];
};

const money = (cents: number): string =>
  `$${(safeNumber(cents) / 100).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** One wall card: fires a confetti burst inside itself whenever its headline number grows. */
function WallStatCard({ label, value, display, sub }: { label: string; value: number; display: string; sub: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const prev = useRef<number | null>(null);
  useEffect(() => {
    if (prev.current !== null && value > prev.current && ref.current) burstConfetti(ref.current);
    prev.current = value;
  }, [value]);
  return (
    <div ref={ref} className="adm-metric-card wall-card">
      <span className="adm-metric-label">{label}</span>
      <strong className="adm-metric-value">{display}</strong>
      <small className="adm-metric-sub">{sub}</small>
    </div>
  );
}

/** Live wall: signups, profits, presence, and per-game activity, repolled on a
 *  cadence so a long-open dashboard stays live. Quiet days honestly read zero. */
function WallStats() {
  const [stats, setStats] = useState<WallStats | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const data = await request<WallStats>(`${API}/admin/stats`);
      clearShapeDrifts();
      setStats(data);
      setError('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Wall stats unavailable'); }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void load();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, [load]);

  if (!stats) {
    return <p className="muted">{error || 'Loading live stats…'}</p>;
  }
  const signups = stats.signups;
  const profits = stats.profits_cents;
  return (
    <div className="wall-stats" aria-label="Live studio stats">
      <WallStatCard
        label="Member signups · today"
        value={safeNumber(signups.today)}
        display={safeNumber(signups.today).toLocaleString()}
        sub={`${safeNumber(signups.week)} this week · ${safeNumber(signups.month)} this month · ${safeNumber(signups.total)} total`}
      />
      <WallStatCard
        label="Profits · today"
        value={safeNumber(profits.day)}
        display={money(profits.day)}
        sub={`${money(profits.week)} week · ${money(profits.month)} month · ${money(profits.year)} year`}
      />
      <WallStatCard
        label="Online now"
        value={safeNumber(stats.online.website)}
        display={safeNumber(stats.online.website).toLocaleString()}
        sub={stats.online.paws === null || stats.online.paws === undefined
          ? 'site presence · Paws status unknown'
          : `site presence · ${safeNumber(stats.online.paws)} in Paws & Parcels`}
      />
      {stats.games.map((game) => {
        return (
          <WallStatCard
            key={safeText(game.slug)}
            label={`${safeText(game.title)} · ${safeText(game.status)}`}
            value={safeNumber(game.acoins_spent)}
            display={`${safeNumber(game.acoins_spent).toLocaleString()} ACoins`}
            sub={
              (game.online === null || game.online === undefined
                ? (game.entitlements === null || game.entitlements === undefined ? 'no live probe' : `${safeNumber(game.entitlements)} Season 1 players`)
                : `${safeNumber(game.online)} online now`) +
              ` · ${safeNumber(game.transactions)} ledger rows`
            }
          />
        );
      })}
    </div>
  );
}

// ── Overview Tab ──

function OverviewTab({ summary, games, onNavigate }: { summary: AdminSummary | null; games: AdminGame[]; onNavigate: (tab: AdminTab) => void }) {
  const liveGames = games.filter((game) => game.status === 'live');
  return (
    <div className="adm-overview">
      <div className="section-heading wall-heading">
        <div><span className="eyebrow">Live</span><h3>Studio Wall</h3></div>
      </div>
      <WallStats />
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
          <span className="adm-metric-label">Live Games</span>
          <strong className="adm-metric-value">{liveGames.length.toLocaleString()}</strong>
          <small className="adm-metric-sub">
            {games.length ? liveGames.map((game) => safeText(game.title)).filter(Boolean).join(' · ') || 'No live titles' : '—'}
          </small>
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
        <button type="button" className="adm-action-card" onClick={() => onNavigate('games')}><span className="adm-action-icon">♞</span><span><strong>Manage Games</strong><small>Titles, posters, and featured order</small></span><b>→</b></button>
        <button type="button" className="adm-action-card" onClick={() => onNavigate('website')}><span className="adm-action-icon">✎</span><span><strong>Website Settings</strong><small>Homepage copy, news, and referrals</small></span><b>→</b></button>
        <button type="button" className="adm-action-card" onClick={() => onNavigate('payments')}><span className="adm-action-icon">◈</span><span><strong>Payments</strong><small>ACoins checkout and PayPal</small></span><b>→</b></button>
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

