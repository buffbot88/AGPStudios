import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { API, request } from './api';
import { confirmModal, toast } from './toast';

type User = { id: string; username: string; email: string; display_name: string; tag_name?: string | null; discord_tag?: string | null; location?: string | null; interests?: string | null; role: string };
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

function formatDate(value: string | number): string {
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? 'Unknown time' : date.toLocaleString();
}

export function MemberSurfaces({ user, initialTab = 'support' }: { user: User | null; initialTab?: MemberTab }) {
  const [tab, setTab] = useState<MemberTab>(initialTab);
  const [error, setError] = useState<string | null>(null);
  const [tickets, setTickets] = useState<TicketSummary[]>([]);
  const [selectedTicket, setSelectedTicket] = useState<{ ticket: Ticket; replies: TicketReply[] } | null>(null);
  const [account, setAccount] = useState<AccountSummary | null>(null);
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
      setTickets(data.tickets);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Support unavailable'); }
  }, [user]);

  const loadAccount = useCallback(async () => {
    if (!user) return;
    try {
      const data = await request<AccountSummary>(`${API}/account/summary`);
      setAccount(data);
      setError(null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Account summary unavailable'); }
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

  return (
    <section className="member-section" aria-label="Ashat member surfaces">
      <div className="section-heading"><div><span className="eyebrow">Ashat member surfaces</span><h2>Explore the platform</h2></div><span className="refresh-state">{user ? `Signed in as ${user.username}` : 'Community and support'}</span></div>
      <nav className="member-tabs" aria-label="Member navigation">
        {tabs.map((item) => <button type="button" key={item.id} className={tab === item.id ? 'member-tab selected' : 'member-tab'} onClick={() => { setTab(item.id); setSelectedTicket(null); }}>{item.label}</button>)}
      </nav>
      {error && <p className="workspace-error" role="alert">{error}</p>}

      {tab === 'support' && <div className="member-panel support-layout">{!user ? <div className="tool-empty"><p>Sign in to create and view support tickets.</p></div> : <><div className="support-sidebar"><div className="member-toolbar"><span className="eyebrow">Your tickets</span><button type="button" className="secondary-button" onClick={() => setSelectedTicket(null)}>New</button></div>{tickets.map((ticket) => <button type="button" className={selectedTicket?.ticket.id === ticket.id ? 'ticket-row selected' : 'ticket-row'} key={ticket.id} onClick={() => void showTicket(ticket.id)}><strong>{ticket.subject}</strong><small>{ticket.status} · {ticket.priority}</small></button>)}{!tickets.length && <p className="muted">No tickets yet.</p>}</div><div className="support-detail">{selectedTicket ? <><button type="button" className="back-button" onClick={() => setSelectedTicket(null)}>← New ticket</button><span className="eyebrow">{selectedTicket.ticket.status} · {selectedTicket.ticket.priority}</span><h3>{selectedTicket.ticket.subject}</h3><p className="ticket-message">{selectedTicket.ticket.message}</p><div className="ticket-replies">{selectedTicket.replies.map((item) => <article className={item.is_staff ? 'ticket-reply staff' : 'ticket-reply'} key={item.id}><small>{item.display_name || item.username || 'Member'} · {formatDate(item.created_at)}</small><p>{item.message}</p></article>)}</div><form className="member-form" onSubmit={(event) => void sendReply(event)}><textarea value={reply} onChange={(event) => setReply(event.target.value)} placeholder="Reply to this ticket" rows={3} required /><button type="submit" disabled={busy}>Reply</button></form></> : <form className="member-form" onSubmit={(event) => void createTicket(event)}><span className="eyebrow">New support ticket</span><input value={supportSubject} onChange={(event) => setSupportSubject(event.target.value)} placeholder="Subject" required /><div className="form-row"><select value={supportCategory} onChange={(event) => setSupportCategory(event.target.value)}><option value="bug">Bug</option><option value="feature">Feature</option><option value="account">Account</option><option value="billing">Billing</option><option value="other">Other</option></select><select value={supportPriority} onChange={(event) => setSupportPriority(event.target.value)}><option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option><option value="urgent">Urgent</option></select></div><textarea value={supportMessage} onChange={(event) => setSupportMessage(event.target.value)} placeholder="Describe the issue or request" rows={7} required /><button type="submit" disabled={busy}>Create ticket</button></form>}</div></>}</div>}

      {tab === 'account' && <div className="member-panel account-grid">{!user ? <div className="tool-empty"><p>Sign in to view your account.</p></div> : account ? <><AccountForm user={account.user} githubLinked={!!account.github_linked} onSaved={(next) => setAccount({ ...account, user: next })} onGithubChange={(linked) => setAccount({ ...account, github_linked: linked })} onDeleted={() => window.location.reload()} /><div className="account-stats"><Metric label="Support tickets" value={account.stats?.support_tickets} /></div></> : <div className="tool-empty"><p>Loading account...</p></div>}</div>}


    </section>
  );
}

function AccountForm({ user, githubLinked, onSaved, onGithubChange, onDeleted }: { user: User; githubLinked: boolean; onSaved: (user: User) => void; onGithubChange: (linked: boolean) => void; onDeleted: () => void }) {
  // tag_name is the UI label for the API's display_name (PUT /api/account requires
  // {username, email, display_name}; discord_tag/location/interests are accepted extras).
  const [form, setForm] = useState({ username: user.username, tag_name: user.display_name, email: user.email });
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

function Metric({ label, value }: { label: string; value: number | undefined }) {
  return <div className="account-stat"><span>{label}</span><strong>{(value ?? 0).toLocaleString()}</strong></div>;
}
