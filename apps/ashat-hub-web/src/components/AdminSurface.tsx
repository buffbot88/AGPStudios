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

  async function createGame(fields: Record<string, unknown>): Promise<string | null> {
    setBusy(true); setError(null);
    try {
      const result = await request<{ ok: boolean; slug: string }>(`${API}/admin/games`, { method: 'POST', body: JSON.stringify(fields) });
      toast('Game added.', 'success');
      await load();
      return safeText(result.slug) || null;
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Game creation failed'); return null; }
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
      {tab === 'games' && <GamesTab games={games} busy={busy} onUpdateGame={updateGame} onCreateGame={createGame} />}
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

function GamesTab({ games, busy, onUpdateGame, onCreateGame }: { games: AdminGame[]; busy: boolean; onUpdateGame: (slug: string, fields: Record<string, unknown>) => void; onCreateGame: (fields: Record<string, unknown>) => Promise<string | null> }) {
  type GameDraft = { title: string; summary: string; status: string; url: string; featured: boolean; sort_order: string; tags: string; poster: string };
  const [drafts, setDrafts] = useState<Record<string, Partial<GameDraft>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [created, setCreated] = useState({ slug: '', title: '', summary: '', status: 'in-development', url: '', featured: false, sort_order: '0', tags: '', poster: '' });
  const list = safeList<AdminGame>(games).filter(Boolean);
  // A reloaded list drops edits for games that no longer exist; the open
  // editor follows suit instead of editing a ghost row.
  useEffect(() => {
    if (selected && !list.some((game) => safeText(game?.slug) === selected)) setSelected(null);
  }, [games, selected]);
  const openGame = list.find((game) => safeText(game?.slug) === selected) ?? null;
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
  async function saveNew(event: FormEvent) {
    event.preventDefault();
    const slug = await onCreateGame({
      slug: created.slug.trim().toLowerCase(), title: created.title.trim(), summary: created.summary.trim(),
      status: created.status, url: created.url.trim(), featured: created.featured,
      sort_order: created.sort_order.trim() === '' ? 0 : Number(created.sort_order),
      tags: created.tags.split(',').map((tag) => tag.trim()).filter(Boolean),
      poster: created.poster.trim(),
    });
    if (slug) {
      setCreated({ slug: '', title: '', summary: '', status: 'in-development', url: '', featured: false, sort_order: '0', tags: '', poster: '' });
      setAdding(false);
      setSelected(slug);
    }
  }
  // Click a card to edit that game; the editor replaces the list so only
  // one full form is ever on screen.
  if (openGame && !adding) {
    const slug = safeText(openGame.slug);
    const current = draft(openGame);
    return (
      <div className="adm-games">
        <div className="adm-toolbar">
          <button type="button" className="secondary-button" onClick={() => setSelected(null)}>← All games</button>
          <span className="eyebrow">Editing {safeText(openGame.title) || slug}</span>
        </div>
        <form className="adm-game-row adm-game-edit" onSubmit={(e) => { e.preventDefault(); save(openGame); }}>
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
      </div>
    );
  }
  return (
    <div className="adm-games">
      <div className="adm-toolbar">
        <span className="eyebrow">Games</span>
        <span className="refresh-state">Edits apply live — no redeploy needed</span>
        <span className="adm-toolbar-spacer" />
        <button type="button" className="secondary-button" onClick={() => { setAdding((open) => !open); setSelected(null); }}>{adding ? 'Cancel' : '+ Add game'}</button>
      </div>
      {adding && (
        <form className="adm-game-row adm-game-add" onSubmit={(e) => void saveNew(e)}>
          <div className="adm-game-fields">
            <div className="adm-game-grid">
              <label>Slug<input value={created.slug} onChange={(e) => setCreated({ ...created, slug: e.target.value })} required maxLength={60} placeholder="starlight-drift" spellCheck={false} /></label>
              <label>Title<input value={created.title} onChange={(e) => setCreated({ ...created, title: e.target.value })} required maxLength={120} /></label>
              <label className="adm-span">Summary<textarea value={created.summary} onChange={(e) => setCreated({ ...created, summary: e.target.value })} required maxLength={500} rows={2} /></label>
              <label>Status<select value={created.status} onChange={(e) => setCreated({ ...created, status: e.target.value })}><option value="live">live</option><option value="in-development">in-development</option><option value="coming-soon">coming-soon</option></select></label>
              <label>Sort<input type="number" min={0} max={1000} value={created.sort_order} onChange={(e) => setCreated({ ...created, sort_order: e.target.value })} /></label>
              <label className="adm-span">URL<input value={created.url} onChange={(e) => setCreated({ ...created, url: e.target.value })} placeholder="https://…" spellCheck={false} maxLength={500} /></label>
              <label className="adm-span">Tags (comma separated)<input value={created.tags} onChange={(e) => setCreated({ ...created, tags: e.target.value })} placeholder="Cozy, Exploration" maxLength={200} /></label>
              <label className="adm-span">Poster URL<input className="adm-game-poster-input" value={created.poster} onChange={(e) => setCreated({ ...created, poster: e.target.value })} placeholder="/poster.svg or https://…" spellCheck={false} /></label>
              <label className="adm-check"><input type="checkbox" checked={created.featured} onChange={(e) => setCreated({ ...created, featured: e.target.checked })} /> Featured on homepage</label>
            </div>
            <div className="adm-game-actions"><button type="submit" className="secondary-button" disabled={busy}>Add game</button></div>
          </div>
        </form>
      )}
      <div className="adm-games-grid">
        {list.map((game) => {
          const slug = safeText(game?.slug);
          return (
            <button key={slug} type="button" className={selected === slug ? 'adm-game-card selected' : 'adm-game-card'} onClick={() => { setSelected(slug); setAdding(false); }}>
              <span className="adm-game-card-thumb">
                <PosterImage
                  src={safeText(game?.poster)}
                  alt=""
                  fallbackClass="thumb-fallback fallback-in-development"
                  fallback={<span aria-hidden>◎</span>}
                />
              </span>
              <span className="adm-game-card-info">
                <strong>{safeText(game?.title) || slug}</strong>
                <small>{safeText(game?.status)} · {safeText(game?.url) || 'no URL yet'}</small>
              </span>
              <b aria-hidden>→</b>
            </button>
          );
        })}
      </div>
      {list.length === 0 && !adding && <div className="adm-empty">No games configured.</div>}
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

type WebsiteSection = 'homepage' | 'registration' | 'news';

const WEBSITE_SECTIONS: { id: WebsiteSection; label: string; hint: string }[] = [
  { id: 'homepage', label: 'Homepage', hint: 'Video & footer' },
  { id: 'registration', label: 'Registration', hint: 'Bot defenses' },
  { id: 'news', label: 'Studio News', hint: 'Pinned items' },
];

function WebsiteTab() {
  const [settings, setSettings] = useState<Record<string, string> | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [section, setSection] = useState<WebsiteSection>('homepage');
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

  async function refreshNews() {
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await request<{ ok: boolean; commits: number }>(`${API}/admin/news/refresh`, { method: 'POST', body: JSON.stringify({}) });
      setMessage(`GitHub news refreshed — ${safeNumber(result.commits)} commit items live on the homepage.`);
      void loadAll();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'News refresh failed'); }
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
    <div className="adm-side-layout">
      <nav className="adm-side-nav" aria-label="Website settings sections">
        {WEBSITE_SECTIONS.map((item) => (
          <button key={item.id} type="button" className={section === item.id ? 'adm-side-nav-item selected' : 'adm-side-nav-item'} onClick={() => setSection(item.id)}>
            <strong>{item.label}</strong><small>{item.hint}</small>
          </button>
        ))}
      </nav>
      <div className="adm-side-panel">
      {message && <p role="status" className="auth-success">{message}</p>}
      {error && <p className="workspace-error" role="alert">{error}</p>}
      {section === 'homepage' && (
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
      )}
      {section === 'registration' && (
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
      )}
      {section === 'news' && (
      <section className="adm-panel">
        <div className="adm-toolbar"><div><span className="eyebrow">Studio news</span><h3>Pinned items</h3></div><span className="adm-toolbar-spacer" /><button type="button" className="secondary-button" disabled={busy} onClick={() => void refreshNews()}>↻ Refresh GitHub news</button><button type="button" className="secondary-button" onClick={() => setAdding((open) => !open)}>{adding ? 'Close' : 'Pin news item'}</button></div>
        <p className="muted">Pinned rows merge with recent GitHub commits on the homepage, newest first. Refresh pulls the latest commits now instead of waiting for the 30-minute cache.</p>
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
                <><div className="adm-news-text"><strong>{safeText(item.title)}</strong><small>{safeText(item.summary)} · {safeTime(item.published_at)}</small></div>
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
      )}
      </div>
    </div>
  );
}

// ── Live Wall Stats ──

type WallStats = {
  signups: { today: number; week: number; month: number; total: number };
  profits_cents: { day: number; week: number; month: number; year: number };
  online: { website: number; paws: number | null; everenfell: number | null };
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
        value={safeNumber(stats.online.website) + safeNumber(stats.online.paws) + safeNumber(stats.online.everenfell)}
        display={(safeNumber(stats.online.website) + safeNumber(stats.online.paws) + safeNumber(stats.online.everenfell)).toLocaleString()}
        sub={`AGP Studios ${safeNumber(stats.online.website)} · Paws & Parcels ${stats.online.paws === null || stats.online.paws === undefined ? 'unknown' : safeNumber(stats.online.paws)} · Everenfell ${stats.online.everenfell === null || stats.online.everenfell === undefined ? 'unknown' : safeNumber(stats.online.everenfell)}`}
      />
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

const USERS_PER_PAGE = 5;

function UsersTab({ users, query, onQueryChange, onSearch, onSelectUser, selectedUser, busy, currentUser, onUpdateUser, onSetEverenfellS1, onBanUser, onDeleteUser, moderation, moderationError, onRefreshModeration }: {
  users: AdminUser[]; query: string; onQueryChange: (q: string) => void; onSearch: () => void;
  onSelectUser: (u: AdminUser | null) => void; selectedUser: AdminUser | null;
  busy: boolean; currentUser: User; onUpdateUser: (id: string, body: { role?: string; is_active?: boolean }) => void;
  onSetEverenfellS1: (id: string, enabled: boolean) => void;
  onBanUser: (id: string, banned: boolean) => void; onDeleteUser: (id: string) => void;
  moderation: ModerationEvent[]; moderationError: string | null; onRefreshModeration: () => void;
}) {
  const [page, setPage] = useState(0);
  const rows = safeList<AdminUser>(users).filter(Boolean);
  const pageCount = Math.max(1, Math.ceil(rows.length / USERS_PER_PAGE));
  // A new search (or a reloaded list) starts back on the first page.
  useEffect(() => { setPage(0); }, [users, query]);
  const safePage = Math.min(page, pageCount - 1);
  const visible = rows.slice(safePage * USERS_PER_PAGE, safePage * USERS_PER_PAGE + USERS_PER_PAGE);
  return (
    <div className="adm-panel-layout">
      <div className="adm-panel-list">
        <div className="adm-toolbar">
          <input value={query} onChange={(e) => onQueryChange(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') onSearch(); }} placeholder="Search users..." />
          <button type="button" className="secondary-button" onClick={onSearch}>Search</button>
        </div>
        <div className="adm-user-list">
          <div className="adm-user-list-head" aria-hidden>
            <span>Member</span><span>Role</span><span>Status</span>
          </div>
          {visible.map((u) => (
            <button key={safeText(u.id)} type="button" className={selectedUser?.id === u?.id ? 'adm-user-row selected' : 'adm-user-row'} onClick={() => onSelectUser(u)}>
              <div className="adm-user-row-info">
                <strong>{safeText(u?.display_name) || safeText(u?.username)}</strong>
                <small>@{safeText(u?.username)} · {safeText(u?.email)}</small>
              </div>
              <span className="adm-user-role">{safeText(u?.role)}</span>
              <span className={u?.is_active ? 'adm-status-dot active' : 'adm-status-dot disabled'} />
            </button>
          ))}
          {rows.length === 0 && <div className="adm-empty">No users found.</div>}
        </div>
        <div className="adm-pagination">
          <button type="button" className="secondary-button" disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>← Prev</button>
          <span>Page {safePage + 1} of {pageCount} · {rows.length} member{rows.length === 1 ? '' : 's'}</span>
          <button type="button" className="secondary-button" disabled={safePage >= pageCount - 1} onClick={() => setPage(safePage + 1)}>Next →</button>
        </div>
      </div>
      {selectedUser ? <UserDetailPanel user={selectedUser} busy={busy} currentUser={currentUser} onUpdate={onUpdateUser} onSetEverenfellS1={onSetEverenfellS1} onBan={onBanUser} onDelete={onDeleteUser} onClose={() => onSelectUser(null)} moderation={moderation} moderationError={moderationError} onRefreshModeration={onRefreshModeration} /> : (
        <div className="adm-detail-panel adm-detail-empty">
          <div className="adm-detail-header"><h3>Member profile</h3></div>
          <p className="muted">Select a member to view their profile.</p>
        </div>
      )}
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

type SystemSection = 'services' | 'updates' | 'migrations' | 'errors' | 'config';

const SYSTEM_SECTIONS: { id: SystemSection; label: string; hint: string }[] = [
  { id: 'services', label: 'Services', hint: 'Runtime & probes' },
  { id: 'updates', label: 'Updates', hint: 'Git & deploy' },
  { id: 'migrations', label: 'Migrations', hint: 'Schema state' },
  { id: 'errors', label: 'Client Errors', hint: 'Browser reports' },
  { id: 'config', label: 'Configuration', hint: 'Runtime flags' },
];

type RuntimeInfo = {
  port: number; database_path: string; oidc_issuer: string;
  probes: { paws_health_url: string; paws_online_url: string; everenfell_online_url: string };
  news: { commits: number; age_seconds: number | null; ttl_seconds: number };
  uptime_seconds: number;
};

function formatDuration(totalSeconds: number): string {
  let rest = Math.max(0, Math.floor(safeNumber(totalSeconds)));
  const days = Math.floor(rest / 86400); rest -= days * 86400;
  const hours = Math.floor(rest / 3600); rest -= hours * 3600;
  const minutes = Math.floor(rest / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m`;
  return 'just started';
}

function formatCacheAge(ageSeconds: number | null | undefined): string {
  if (ageSeconds === null || ageSeconds === undefined) return 'never refreshed';
  const age = Math.max(0, Math.floor(safeNumber(ageSeconds)));
  if (age < 60) return `${age}s ago`;
  if (age < 3600) return `${Math.floor(age / 60)}m ago`;
  return `${Math.floor(age / 3600)}h ago`;
}

function ProbeRow({ label, url }: { label: string; url: string }) {
  const target = safeText(url);
  return (
    <div className="adm-field">
      <span className="adm-field-label">{label}</span>
      {target
        ? <span className="adm-field-value mono">{target}</span>
        : <span className="adm-badge">not configured</span>}
    </div>
  );
}

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
  const [section, setSection] = useState<SystemSection>('services');
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null);
  const [runtimeFailed, setRuntimeFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void request<RuntimeInfo>(`${API}/admin/runtime`)
      .then((data) => { if (!cancelled) { clearShapeDrifts(); setRuntime(data); } })
      .catch(() => { if (!cancelled) setRuntimeFailed(true); });
    return () => { cancelled = true; };
  }, []);

  return (
    <div className="adm-side-layout">
      <nav className="adm-side-nav" aria-label="System settings sections">
        {SYSTEM_SECTIONS.map((item) => (
          <button key={item.id} type="button" className={section === item.id ? 'adm-side-nav-item selected' : 'adm-side-nav-item'} onClick={() => setSection(item.id)}>
            <strong>{item.label}</strong><small>{item.hint}</small>
          </button>
        ))}
      </nav>
      <div className="adm-side-panel">
      {section === 'services' && (
        <>
        <section className="adm-panel">
          <div className="adm-migrations-heading">
            <div><span className="eyebrow">Database</span><h3>{safeText(database?.version) || 'Loading…'}</h3></div>
            <span className="adm-badge ok">● Connected</span>
          </div>
          <div className="adm-detail-fields">
            <div className="adm-field"><span className="adm-field-label">Backend</span><span className="adm-field-value">{safeText(database?.database) || 'Unknown'}</span></div>
            <div className="adm-field"><span className="adm-field-label">Schema revision</span><span className="adm-field-value mono">{safeText(database?.migrations?.current_revision) || '—'}</span></div>
            <div className="adm-field"><span className="adm-field-label">Schema updates</span><span className="adm-field-value">Versioned</span></div>
            <div className="adm-field"><span className="adm-field-label">Paws & Parcels probe</span>{database?.paws ? <span className="adm-badge ok">{safeText(database.paws.status) || 'ok'}</span> : <span className="adm-badge">unknown</span>}</div>
          </div>
        </section>
        <section className="adm-panel">
          <div className="adm-migrations-heading">
            <div><span className="eyebrow">Advanced</span><h3>Server runtime</h3></div>
            <span className="adm-badge ok">● Running</span>
          </div>
          <p className="muted">Non-secret host configuration and live process state. Secrets are never exposed here.</p>
          {runtime ? (
            <div className="adm-detail-fields">
              <div className="adm-field"><span className="adm-field-label">API port</span><span className="adm-field-value mono">{safeNumber(runtime.port)}</span></div>
              <div className="adm-field"><span className="adm-field-label">Database path</span><span className="adm-field-value mono">{safeText(runtime.database_path) || '—'}</span></div>
              <div className="adm-field"><span className="adm-field-label">OIDC issuer</span><span className="adm-field-value mono">{safeText(runtime.oidc_issuer) || '—'}</span></div>
              <div className="adm-field"><span className="adm-field-label">Uptime</span><span className="adm-field-value">{formatDuration(runtime.uptime_seconds)}</span></div>
              <div className="adm-field"><span className="adm-field-label">News cache</span><span className="adm-field-value">{safeNumber(runtime.news?.commits)} commits · {formatCacheAge(runtime.news?.age_seconds ?? null)}</span></div>
              <ProbeRow label="Paws health" url={safeText(runtime.probes?.paws_health_url)} />
              <ProbeRow label="Paws online" url={safeText(runtime.probes?.paws_online_url)} />
              <ProbeRow label="Everenfell online" url={safeText(runtime.probes?.everenfell_online_url)} />
            </div>
          ) : (
            <p className="muted">{runtimeFailed ? 'Runtime facts unavailable.' : 'Loading runtime facts…'}</p>
          )}
        </section>
        </>
      )}
      {section === 'updates' && (
        <section className="adm-panel">
          <span className="eyebrow">Deployment</span>
          <h3>System updates</h3>
          <p className="muted">Live checkout versus the repository. Push committed work to GitHub, or pull, rebuild, and restart the live service.</p>
          <div className={`adm-system-updates adm-repo-${safeText(status?.ashathub?.state) || 'unknown'}`}><div className="adm-commit-comparison"><div><span>Live commit</span><code>{safeText(status?.ashathub?.live).slice(0, 8) || '—'}</code></div><div><span>Repo commit</span><code>{safeText(status?.ashathub?.repo).slice(0, 8) || '—'}</code></div></div><div className="adm-system-update-actions"><button type="button" className="secondary-button" onClick={onPush}>Push Changes to GitHub</button><button type="button" className="secondary-button" onClick={onUpdate}>Pull, Build & Restart</button></div></div>
        </section>
      )}
      {section === 'migrations' && (
        <MigrationsPanel loaded={loaded} migrations={database?.migrations} busy={busy} onApplyMigrations={onApplyMigrations} />
      )}
      {section === 'errors' && (
        <ClientErrorsPanel errors={clientErrors} error={clientErrorsError} busy={clientErrorsBusy} status={clientErrorsFilter} onStatusChange={onFilterClientErrors} onRefresh={() => onRefreshClientErrors()} onTriage={onTriageClientError} onMute={onMuteClientErrorMessage} />
      )}
      {section === 'config' && (
        <section className="adm-panel">
          <span className="eyebrow">Configuration</span>
          <h3>Runtime flags</h3>
          <p className="muted">Live flags reported by the API. Content and registration settings live under Website; PayPal under Payments.</p>
          {settings && Object.entries(settings)
            .filter(([key]) => key !== 'backend') // internal value, not a user-facing setting
            .map(([key, value]) => (
              <div className="adm-setting-row" key={key}><span>{key.replace(/_/g, ' ')}</span><strong>{typeof value === 'boolean' ? (value ? '✓ Enabled' : '○ Disabled') : String(value)}</strong></div>
            ))}
        </section>
      )}
      </div>
    </div>
  );
}

