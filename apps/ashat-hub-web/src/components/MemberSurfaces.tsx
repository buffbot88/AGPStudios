import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { API, clearShapeDrifts, request, safeList, safeNumber, safeText, safeTime } from './api';
import { confirmModal, toast } from './toast';
import { maybeCrash } from './devCrash';
import { UnexpectedDataNotice, UnexpectedDataPanel } from './UnexpectedDataNotice';

type User = { id: string; username: string; email: string; display_name: string; tag_name?: string | null; discord_tag?: string | null; location?: string | null; interests?: string | null; role: string; acoins_balance?: number };
export type MemberTab = 'support' | 'account';

type Project = { id: string; name: string };
type CommunityProject = {
  id: string;
  slug: string;
  title: string;
  description: string;
  category: string;
  tags: string;
  stack: string;
  status: string;
  created_at: string;
  user_id?: string | null;
  publisher_username?: string | null;
  publisher_display_name?: string | null;
  deployed_url?: string | null;
};
type TicketSummary = { id: string; subject: string; status: string; priority: string; category: string; preview: string; created_at: string; updated_at: string };
type TicketReply = { id: string; ticket_id: string; user_id: string; message: string; is_staff: number; created_at: string; username?: string; display_name?: string; role?: string };
type Ticket = { id: string; user_id: string; subject: string; status: string; priority: string; category: string; message: string; created_at: string; updated_at: string };
/** Shape of GET /api/account/summary per the Python API: {user, stats:{support_tickets}}. */
type AccountSummary = { user: User; github_linked?: boolean; stats: { support_tickets: number } };


const categories = ['all', 'tools', 'ai', 'pipeline', 'games', 'general'];

// formatDate replaced by safeTime from ./api (drift-proof)

export function MemberSurfaces({ user, initialTab = 'support' }: { user: User | null; initialTab?: MemberTab }) {
  const [tab, setTab] = useState<MemberTab>(initialTab);
  const [error, setError] = useState<string | null>(null);
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [selectedTicket, setSelectedTicket] = useState<{ ticket: Ticket; replies: TicketReply[] } | null>(null);
  const [account, setAccount] = useState<AccountSummary | null>(null);
  // Distinguishes "still loading" from "arrived but unusable": only the former
  // may keep a spinner up, or a wrong-shaped payload loads forever.
  const [accountLoaded, setAccountLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showSubmit, setShowSubmit] = useState(false);
  const [submitProjectId, setSubmitProjectId] = useState('');
  const [submitTitle, setSubmitTitle] = useState('');
  const [submitDescription, setSubmitDescription] = useState('');
  const [submitCategory, setSubmitCategory] = useState('general');
  const [submitTags, setSubmitTags] = useState('');
  const [submitStack, setSubmitStack] = useState('');
  const [supportSubject, setSupportSubject] = useState('');
  const [supportMessage, setSupportMessage] = useState('');
  const [supportCategory, setSupportCategory] = useState('other');
  const [supportPriority, setSupportPriority] = useState('normal');
  const [reply, setReply] = useState('');

  useEffect(() => { setTab(initialTab); }, [initialTab]);

  const loadTickets = useCallback(async () => {
    if (!user) return;
    try {
      const data = await request<{ tickets: TicketSummary[] }>(`${API}/support`);
      // Fresh payload: the notice describes the latest response, not history
      // (including placeholders the loading render legitimately coerced).
      clearShapeDrifts();
      setTickets(safeList<TicketSummary>(data.tickets));
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Support unavailable'); }
  }, [user]);

  const loadAccount = useCallback(async () => {
    if (!user) return;
    try {
      const data = await request<AccountSummary>(`${API}/account/summary`);
      clearShapeDrifts();
      setAccount(data);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Account summary unavailable'); }
    finally { setAccountLoaded(true); }
  }, [user]);

  useEffect(() => {
    
    if (tab === 'support') void loadTickets();
    if (tab === 'account') void loadAccount();
  }, [tab, loadAccount, loadTickets]);

  async function showTicket(id: string) {
    try {
      const data = await request<{ ticket: Ticket; replies: TicketReply[] }>(`${API}/support/${encodeURIComponent(id)}`);
      setSelectedTicket(data);
      setReply('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Ticket unavailable'); }
  }

  async function createTicket(event: FormEvent) {
    event.preventDefault();
    if (!supportSubject.trim() || !supportMessage.trim()) return;
    setBusy(true); setError(null);
    try {
      const data = await request<{ id: string }>(`${API}/support`, { method: 'POST', body: JSON.stringify({ subject: supportSubject, message: supportMessage, category: supportCategory, priority: supportPriority }) });
      setSupportSubject(''); setSupportMessage('');
      await loadTickets();
      await showTicket(data.id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Ticket creation failed'); }
    finally { setBusy(false); }
  }

  async function sendReply(event: FormEvent) {
    event.preventDefault();
    if (!selectedTicket || !reply.trim()) return;
    setBusy(true); setError(null);
    try {
      await request(`${API}/support/${encodeURIComponent(selectedTicket.ticket.id)}/reply`, { method: 'POST', body: JSON.stringify({ message: reply }) });
      setReply(''); await showTicket(selectedTicket.ticket.id); await loadTickets();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Reply failed'); }
    finally { setBusy(false); }
  }

  const tabs: { id: MemberTab; label: string }[] = [
    { id: 'support', label: 'Support' },
    { id: 'account', label: 'Account' },
  ];

  maybeCrash('member');

  return (
    <section className="member-section" aria-label="Ashat member surfaces">
      <div className="section-heading"><div><h2>Explore the platform</h2></div></div>
      <nav className="member-tabs" aria-label="Member navigation">
        {tabs.map((item) => <button type="button" key={item.id} className={tab === item.id ? 'member-tab selected' : 'member-tab'} onClick={() => { setTab(item.id); setSelectedTicket(null); }}>{item.label}</button>)}
      </nav>
      {/* When the account panel is already explaining a rejected payload, the
          surface-wide banner would repeat the same message. */}
      {error && !(tab === 'account' && !account?.user) && <p className="workspace-error" role="alert">{error}</p>}
      <UnexpectedDataNotice onRetry={() => { setAccountLoaded(false); void loadTickets(); void loadAccount(); }} />

      {tab === 'support' && <div className="member-panel support-layout">{!user ? <div className="tool-empty"><p>Sign in to create and view support tickets.</p></div> : <><div className="support-sidebar"><div className="member-toolbar"><span className="eyebrow">Your tickets</span><button type="button" className="secondary-button" onClick={() => setSelectedTicket(null)}>New</button></div>{tickets.map((ticket) => <button type="button" className={selectedTicket?.ticket.id === ticket.id ? 'ticket-row selected' : 'ticket-row'} key={safeText(ticket.id)} onClick={() => void showTicket(safeText(ticket.id))}><strong>{safeText(ticket.subject)}</strong><small>{safeText(ticket.status)} · {safeText(ticket.priority)}</small></button>)}{!tickets.length && <p className="muted">No tickets yet.</p>}</div><div className="support-detail">{selectedTicket ? <><button type="button" className="back-button" onClick={() => setSelectedTicket(null)}>← New ticket</button><span className="eyebrow">{safeText(selectedTicket.ticket.status)} · {safeText(selectedTicket.ticket.priority)}</span><h3>{safeText(selectedTicket.ticket.subject)}</h3><p className="ticket-message">{safeText(selectedTicket.ticket.message)}</p><div className="ticket-replies">{safeList<TicketReply>(selectedTicket.replies).map((item) => <article className={item.is_staff ? 'ticket-reply staff' : 'ticket-reply'} key={safeText(item.id)}><small>{safeText(item.display_name) || safeText(item.username) || 'Member'} · {safeTime(item.created_at)}</small><p>{safeText(item.message)}</p></article>)}</div><form className="member-form" onSubmit={(event) => void sendReply(event)}><textarea value={reply} onChange={(event) => setReply(event.target.value)} placeholder="Reply to this ticket" rows={3} required /><button type="submit" disabled={busy}>Reply</button></form></> : <form className="member-form" onSubmit={(event) => void createTicket(event)}><span className="eyebrow">New support ticket</span><input value={supportSubject} onChange={(event) => setSupportSubject(event.target.value)} placeholder="Subject" required /><div className="form-row"><select value={supportCategory} onChange={(event) => setSupportCategory(event.target.value)}><option value="bug">Bug</option><option value="feature">Feature</option><option value="account">Account</option><option value="billing">Billing</option><option value="other">Other</option></select><select value={supportPriority} onChange={(event) => setSupportPriority(event.target.value)}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option></select></div><textarea value={supportMessage} onChange={(event) => setSupportMessage(event.target.value)} placeholder="Describe the issue or request" rows={7} required /><button type="submit" disabled={busy}>Create ticket</button></form>}</div></>}</div>}

      {tab === 'account' && <div className="member-panel account-grid">{!user ? <div className="tool-empty"><p>Sign in to view your account.</p></div> : !accountLoaded ? <div className="tool-empty"><p>Loading account...</p></div> : !account?.user ? <UnexpectedDataPanel what="Your account details" reason={error ?? undefined} onRetry={() => { setAccountLoaded(false); void loadAccount(); }} /> : <><AccountForm user={account.user} githubLinked={!!account.github_linked} onSaved={(next) => setAccount({ ...account, user: next })} onGithubChange={(linked) => setAccount({ ...account, github_linked: linked })} onDeleted={() => window.location.reload()} /><div className="account-stats"><Metric label="Support tickets" value={safeNumber(account.stats?.support_tickets)} /><PurchaseHistoryPanel /></div></>}</div>}


    </section>
  );
}

function AccountForm({ user, githubLinked, onSaved, onGithubChange, onDeleted }: { user: User; githubLinked: boolean; onSaved: (user: User) => void; onGithubChange: (linked: boolean) => void; onDeleted: () => void }) {
  // tag_name is the UI label for the API's display_name (PUT /api/account requires
  // {username, email, display_name}; discord_tag/location/interests are accepted extras).
  // safeText everywhere: a null/wrong-shape user object degrades to blank fields.
  const [form, setForm] = useState({
    username: safeText(user?.username),
    tag_name: safeText(user?.display_name),
    email: safeText(user?.email),
  });
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function unlinkGithub() {
    setBusy(true); setError('');
    try {
      await request(`${API}/account/github`, { method: 'POST' });
      onGithubChange(false);
      setMessage('GitHub account unlinked.');
    }
    catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Unable to unlink GitHub';
      // The API only implements GitHub *linking*; unlinking is not a real endpoint yet.
      setError(message === 'Request failed (404)' ? 'GitHub unlinking is not available yet.' : message);
    }
    finally { setBusy(false); }
  }
  async function deleteAccount() {
    if (!(await confirmModal({
      title: 'Delete your account?',
      message: 'This permanently removes your account and all associated data. This cannot be undone.',
      confirmLabel: 'Delete forever',
      requireText: 'DELETE',
    })))
      return;
    setBusy(true); setError('');
    try { await request(`${API}/account`, { method: 'DELETE' }); toast('Your account has been deleted.'); onDeleted(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to delete account'); }
    finally { setBusy(false); }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(''); setError('');
    try {
      const next = await request<{ user: User }>(`${API}/account`, {
        method: 'PUT',
        body: JSON.stringify({ username: form.username, email: form.email, display_name: form.tag_name }),
      });
      onSaved(next.user); setMessage('Account updated.');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Account update failed'); }
    finally { setBusy(false); }
  }
  return <form className="member-form account-form" onSubmit={(event) => void save(event)}><span className="eyebrow">Account details</span><div className="form-row"><input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="Username" required /><input value={form.tag_name} onChange={(e) => setForm({ ...form, tag_name: e.target.value })} placeholder="Display name" required /></div><input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} placeholder="Email" type="email" required /><button type="submit" disabled={busy}>{busy ? 'Saving…' : 'Save account'}</button><div className="account-danger"><strong>GitHub connection</strong><span>{githubLinked ? 'GitHub account linked.' : 'No GitHub account linked.'}</span>{githubLinked
              ? <button type="button" className="secondary-button" onClick={() => void unlinkGithub()} disabled={busy} title="Unlinking is not available on the API yet">Unlink GitHub</button>
              : <a className="secondary-button" href={`${API}/auth/github`}>Link GitHub account</a>}</div><button type="button" className="delete-account-button" onClick={() => void deleteAccount()} disabled={busy}>Delete account and all data</button>{message && <small className="auth-success">{message}</small>}{error && <small className="auth-error">{error}</small>}</form>;
}

type PurchaseTransaction = {
  id: string;
  kind: 'acoins_purchase' | 'acoins_spend' | 'game_purchase';
  title: string;
  detail: string;
  amount_cents: number | null;
  acoins_delta: number;
  game_slug: string | null;
  status: string;
  created_at: number;
  receipt: Record<string, unknown>;
};

/** Purchase history: past ACoins buys, ACoins spending, and per-game
 *  purchases (some games do not use ACoins). Newest first, searchable,
 *  with an expandable receipt per row. No refunds are issued for the
 *  purchase of digital content or currency. */
function PurchaseHistoryPanel() {
  const [transactions, setTransactions] = useState<PurchaseTransaction[] | null>(null);
  const [query, setQuery] = useState('');
  const [openId, setOpenId] = useState<string | null>(null);
  const [message, setMessage] = useState('');

  const load = async () => {
    try {
      const data = await request<{ transactions: PurchaseTransaction[] }>(`${API}/account/transactions`);
      setTransactions(Array.isArray(data.transactions) ? data.transactions : []);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : 'Purchase history unavailable');
      setTransactions([]);
    }
  };

  // In-flight PayPal returns still land here (?acoins=return&token=):
  // confirm the capture so the purchase posts to history, then reload.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const token = params.get('token');
    if (params.get('acoins') === 'return' && token) {
      request(`${API}/acoins/capture`, { method: 'POST', body: JSON.stringify({ order_id: token }) })
        .then(() => setMessage('Payment confirmed. Your purchase is listed below.'))
        .catch((reason: unknown) => setMessage(reason instanceof Error ? reason.message : 'Payment confirmation failed.'))
        .finally(() => {
          const url = new URL(window.location.href);
          url.search = '';
          window.history.replaceState({}, '', `${url.pathname}${url.hash}`);
          void load();
        });
    } else if (params.get('acoins') === 'cancel') {
      setMessage('Payment canceled. No charges were made.');
      window.history.replaceState({}, '', `${window.location.pathname}${window.location.hash}`);
    }
    void load();
  }, []);

  const needle = query.trim().toLowerCase();
  const visible = (transactions ?? []).filter((item) =>
    !needle ||
    [item.title, item.detail, item.game_slug ?? '', item.kind].some((field) => field.toLowerCase().includes(needle)),
  );

  const money = (cents: number | null) =>
    cents == null ? '—' : `$${(cents / 100).toFixed(2)}`;
  const coins = (delta: number) =>
    delta === 0 ? '—' : `${delta > 0 ? '+' : ''}${delta.toLocaleString()} ACoins`;

  return (
    <section className="purchase-history" aria-labelledby="purchase-history-title">
      <div>
        <span className="eyebrow">Purchases</span>
        <h3 id="purchase-history-title">Purchase history</h3>
      </div>
      <input
        value={query}
        onChange={(event) => setQuery(event.target.value)}
        placeholder="Search purchases"
        aria-label="Search purchase history"
      />
      {message && <p role="status">{message}</p>}
      {transactions === null ? (
        <p className="muted">Loading purchase history...</p>
      ) : visible.length === 0 ? (
        <p className="muted">{transactions.length === 0 ? 'No purchases yet.' : 'No purchases match your search.'}</p>
      ) : (
        <ol className="purchase-rows">
          {visible.map((item) => {
            const open = openId === item.id;
            return (
              <li key={safeText(item.id)}>
                <button
                  type="button"
                  className={`purchase-row${open ? ' selected' : ''}`}
                  onClick={() => setOpenId(open ? null : safeText(item.id))}
                  aria-expanded={open}
                >
                  <span className="purchase-row-main">
                    <strong>{safeText(item.title)}</strong>
                    <small>{safeText(item.detail)} · {safeTime(item.created_at)}</small>
                  </span>
                  <span className="purchase-row-amounts">
                    <span>{money(item.amount_cents)}</span>
                    <span>{coins(safeNumber(item.acoins_delta))}</span>
                  </span>
                </button>
                {open && (
                  <div className="purchase-receipt">
                    <dl>
                      <div><dt>Transaction</dt><dd>{safeText(item.id)}</dd></div>
                      <div><dt>Type</dt><dd>{safeText(item.kind)}</dd></div>
                      <div><dt>Date</dt><dd>{safeTime(item.created_at)}</dd></div>
                      <div><dt>Amount</dt><dd>{money(item.amount_cents)}</dd></div>
                      <div><dt>ACoins</dt><dd>{coins(safeNumber(item.acoins_delta))}</dd></div>
                      {item.game_slug && <div><dt>Game</dt><dd>{safeText(item.game_slug)}</dd></div>}
                      {Object.entries(item.receipt ?? {}).map(([key, value]) => (
                        <div key={key}><dt>{safeText(key)}</dt><dd>{safeText(String(value))}</dd></div>
                      ))}
                    </dl>
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      )}
      <p className="purchase-policy">No refunds will be issued for the purchase of digital content or currency.</p>
    </section>
  );
}

function Metric({ label, value }: { label: string; value: number | undefined }) {
  // value is already normalized via safeNumber at the call site; safeNumber
  // again so a drift in the prop pipeline still can't throw.
  return <div className="account-stat"><span>{label}</span><strong>{safeNumber(value).toLocaleString()}</strong></div>;
}
