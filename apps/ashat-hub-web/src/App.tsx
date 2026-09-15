import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { AuthPanel } from './components/AuthPanel';
import { MemberSurfaces } from './components/MemberSurfaces';
import type { MemberTab } from './components/MemberSurfaces';
import { AdminSurface } from './components/AdminSurface';
import { csrfToken } from './components/api';
import './styles.css';

type User = { id: string; display_name: string; username: string; email: string; role: string };
interface ServerSnapshot { id: string; label: string; online: boolean; active_requests: number; requests_last_5m: number; generation_tokens_per_second: number; prompt_tokens_per_second: number; total_completion_tokens: number; avg_latency_ms: number; queue_depth: number; queue_limit: number }
interface TelemetryResponse { servers: ServerSnapshot[]; updated_at: number }
interface ShowcaseProject { id: string; name: string; description: string; category: string; status: string; updated: string }
interface ShowcaseResponse { projects: ShowcaseProject[] }type View = 'home' | 'projects' | 'games' | 'community' | 'support' | 'account' | 'telemetry' | 'admin' | 'terms' | 'privacy' | 'vesper-auth' | 'verify-email' | 'reset-password' | 'error';


function viewForPath(path: string): View {
  if (path === '/projects') return 'projects';
  if (path === '/games') return 'games';
  if (path.startsWith('/community')) return 'community';
  if (path.startsWith('/support')) return 'support';
  if (path.startsWith('/account')) return 'account';
  if (path.startsWith('/telemetry')) return 'telemetry';
  if (path.startsWith('/admin')) return 'admin';
  if (path.startsWith('/terms')) return 'terms';
  if (path.startsWith('/privacy')) return 'privacy';
  if (path.startsWith('/auth/vesper')) return 'vesper-auth';
  if (path.startsWith('/auth/verify-email')) return 'verify-email';
  if (path.startsWith('/auth/reset-password')) return 'reset-password';
  if (path.startsWith('/error')) return 'error';
  return 'home';
}

function ServerCard({ server }: { server: ServerSnapshot }) {
  return (
    <article className="server-card">
      <div className="server-card-header">
        <div><span className="eyebrow">{server.label}</span><strong>{server.online ? 'Telemetry connected' : 'Unavailable'}</strong></div>
        <span className={`server-status ${server.online ? 'online' : 'offline'}`}><span className="status-dot" /> {server.online ? 'online' : 'offline'}</span>
      </div>
      <div className="server-model">{server.online ? 'Telemetry gateway connected' : 'Unavailable'}</div>
      <div className="metrics-grid">
        <Metric label="Active requests" value={server.active_requests.toLocaleString()} />
        <Metric label="Requests · 5 min" value={server.requests_last_5m.toLocaleString()} />
        <Metric label="Generation tok/s" value={server.generation_tokens_per_second.toFixed(1)} />
        <Metric label="Prompt tok/s" value={server.prompt_tokens_per_second.toFixed(1)} />
        <Metric label="Completion tokens" value={server.total_completion_tokens.toLocaleString()} />
        <Metric label="Queue" value={`${server.queue_depth}/${server.queue_limit}`} />
      </div>
    </article>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="metric"><span>{label}</span><strong>{value}</strong></div>;
}

function ProjectCard({ project }: { project: ShowcaseProject }) {
  const statusClass = project.status === 'live' ? 'live' : project.status === 'in-development' ? 'in-development' : 'archived';
  const statusLabel = project.status === 'live' ? 'Live' : project.status === 'in-development' ? 'In Development' : 'Archived';
  const url = project.id === 'galileo' ? 'https://galileo.agpstudios.org/' : project.id === 'paws-and-parcels' ? 'https://pawsandparcels.agpstudios.org/' : undefined;
  const card = (
    <article className="project-card">
      <div className="project-card-thumb">
        <span className="project-glyph" aria-hidden="true">{project.category === 'game' ? '◈' : project.category === 'studio' ? '✦' : '⌘'}</span>
      </div>
      <div className="project-card-body">
        <h3>{project.name}</h3>
        <p>{project.description}</p>
        <div className="project-card-footer">
          <span className={`project-status ${statusClass}`}>{statusLabel}</span>
          <span className="project-card-action">{url ? 'Open' : 'View'} →</span>
        </div>
      </div>
    </article>
  );
  return url ? <a className="project-card-link" href={url}>{card}</a> : card;
}

function LegalPage({ kind }: { kind: 'terms' | 'privacy' }) {
  if (kind === 'privacy') return <section className="legal-panel"><span className="eyebrow">AGP Studios</span><h1>Privacy Policy</h1><p className="legal-lead">AGP Studios builds software, games, and tools. This policy explains what information AshatHub handles and why.</p><h2>Information we handle</h2><p>We may store account details such as your username, email address, display name, authentication records, projects, files, conversations, deployments, support requests, and activity needed to operate the service.</p><h2>How we use it</h2><p>We use this information to provide authentication, save and run your projects, respond to requests, maintain security, provide support, and improve reliability. We do not sell personal information.</p><h2>Project and AI data</h2><p>Project files and messages are used to provide the requested workspace and coding assistance. Do not submit secrets or information you are not authorized to share.</p><h2>Choices and deletion</h2><p>You may request account and associated project deletion through an administrator. Deletion is intended to remove the account, sessions, linked accounts, project data, conversations, deployments, and related records, subject to necessary security or legal retention.</p><h2>Contact</h2><p>For privacy questions or requests, use the support channel on this site.</p></section>;
  return <section className="legal-panel"><span className="eyebrow">AGP Studios</span><h1>Terms of Service</h1><p className="legal-lead">By using AGP Studios or AshatHub, you agree to use the services lawfully and responsibly.</p><h2>Using the services</h2><p>You are responsible for your account, credentials, submitted content, and activity. Do not use the services to abuse infrastructure, bypass access controls, distribute malware, infringe rights, or interfere with other users.</p><h2>Your content</h2><p>You retain rights to content you submit. You grant AGP Studios the limited permission needed to store, process, display, preview, deploy, and transmit that content as requested by you.</p><h2>AI-assisted output</h2><p>Ashat may produce incomplete or incorrect output. Review generated code and commands before relying on them. You are responsible for the software you run or deploy.</p><h2>Availability</h2><p>Services may change, pause, or be unavailable for maintenance, security, or operational reasons. We do not guarantee that every generated result or deployment will be error-free.</p><h2>Termination</h2><p>We may suspend access for abuse, security risk, or material violations. You may stop using the services and request deletion of your account.</p><h2>Contact</h2><p>For questions about these terms, use the support channel on this site.</p></section>;
}

export default function App() {
  const [telemetry, setTelemetry] = useState<TelemetryResponse | null>(null);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [view, setView] = useState<View>(() => viewForPath(window.location.pathname));
  const [showcase, setShowcase] = useState<ShowcaseProject[]>([]);

  const navigate = useCallback((path: string) => {
    window.history.pushState({}, '', path);
    setView(viewForPath(path));
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }, []);

  useEffect(() => {
    const onPopState = () => setView(viewForPath(window.location.pathname));
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  useEffect(() => {
    fetch('/api/showcase', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then((data: ShowcaseResponse | null) => { if (data?.projects) setShowcase(data.projects); })
      .catch(() => {});
  }, []);

  const loadTelemetry = useCallback(async () => {
    if (!user || view !== 'telemetry') { setTelemetry(null); setTelemetryError(null); return; }
    try {
      const response = await fetch('/api/telemetry', { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error(`Telemetry request failed (${response.status})`);
      setTelemetry(await response.json() as TelemetryResponse);
      setUpdatedAt(new Date());
      setTelemetryError(null);
    } catch (requestError) {
      setTelemetryError(requestError instanceof Error ? requestError.message : 'Telemetry unavailable');
    }
  }, [user, view]);

  useEffect(() => {
    void loadTelemetry();
    if (!user || view !== 'telemetry') return undefined;
    const timer = window.setInterval(() => void loadTelemetry(), 8000);
    return () => window.clearInterval(timer);
  }, [loadTelemetry]);

  const handleAuthChange = useCallback((nextUser: User | null) => setUser(nextUser), []);
  const showHome = view === 'home';
  if (view === 'vesper-auth') return <VesperAuthPage />;
  if (view === 'verify-email') return <EmailVerificationPage />;
  if (view === 'reset-password') return <PasswordResetPage />;
  const memberTab: MemberTab | undefined = ['community', 'support', 'account'].includes(view) ? view as MemberTab : undefined;
  const showMemberSurface = ['community', 'support', 'account'].includes(view);

  const studioProjects = showcase.filter(p => (p.category === 'studio' || p.category === 'project') && !['ashat', 'ashat-ai', 'ashat-hub'].includes(p.id));
  const gameProjects = showcase.filter(p => p.category === 'game');

  return (
    <main className="hub-shell">
      <header className="hub-header">
        <button type="button" className="brand-mark brand-button" onClick={() => navigate('/')}><img className="brand-logo" src="/agp-logo.png" alt="" width="32" height="32" /><span>AGP<span className="brand-accent">Studios</span></span></button>
        <nav className="site-nav" aria-label="Primary navigation">
          <button type="button" className={view === 'projects' ? 'site-nav-link selected' : 'site-nav-link'} onClick={() => navigate('/projects')}>Projects</button>
          <button type="button" className={view === 'games' ? 'site-nav-link selected' : 'site-nav-link'} onClick={() => navigate('/games')}>Games</button>
        </nav>
        <div className="header-actions"><AuthPanel onChange={handleAuthChange} onNavigate={navigate} /></div>
      </header>

      {showHome && <>
        <section className="home-hero"><div className="hero-copy"><span className="eyebrow">Independent software studio</span><h1>Software, games, and tools built with purpose.</h1><p>AGP Studios builds practical software, games, and tools.</p><div className="hero-actions"><button type="button" className="primary-button" onClick={() => navigate('/projects')}>Explore Projects</button></div></div><div className="hero-ornament" aria-hidden="true"><span>AGP</span><i /><i /><i /></div></section>

      </>}

      {view === 'projects' && <>
        <section className="hero-copy compact-hero">
          <span className="eyebrow">Projects</span>
          <h1>Tools and platforms.</h1>
          <p>Development environments, infrastructure, and internal tooling.</p>
        </section>
        <div className="project-grid">
          {studioProjects.map(project => <ProjectCard key={project.id} project={project} />)}
        </div>
      </>}

      {view === 'games' && <>
        <section className="hero-copy compact-hero">
          <span className="eyebrow">Games</span>
          <h1>Game worlds.</h1>
          <p>Interactive experiences built with care.</p>
        </section>
        <div className="project-grid">
          {gameProjects.length > 0 ? gameProjects.map(project => <ProjectCard key={project.id} project={project} />) : <p className="muted">No games listed yet.</p>}
        </div>
      </>}


      {showMemberSurface && <MemberSurfaces user={user} initialTab={memberTab} />}
      {view === 'admin' && <AdminSurface user={user} />}
      {view === 'telemetry' && <TelemetrySection user={user} telemetry={telemetry} telemetryError={telemetryError} updatedAt={updatedAt} />}
      {view === 'terms' && <LegalPage kind="terms" />}
      {view === 'privacy' && <LegalPage kind="privacy" />}
      {view === 'error' && <section className="member-panel legal-panel"><span className="eyebrow">Ashat platform</span><h2>Page unavailable</h2><p>The requested page could not be found.</p></section>}

      {view !== 'admin' && <StudioFooter navigate={navigate} />}
    </main>
  );
}

function EmailVerificationPage() {
  const token = new URLSearchParams(window.location.search).get('token') || '';
  const [status, setStatus] = useState<'loading' | 'success' | 'error'>('loading');
  const [message, setMessage] = useState('Verifying your email address…');

  useEffect(() => {
    if (!token) {
      setStatus('error');
      setMessage('This verification link is missing its token.');
      return;
    }
    fetch(`/api/auth/verify-email?token=${encodeURIComponent(token)}`, { credentials: 'same-origin' })
      .then(async (response) => {
        const data = await response.json() as { error?: { message?: string; code?: string } };
        if (!response.ok) throw new Error(data.error?.message || data.error?.code || 'This verification link is invalid or expired.');
        setStatus('success');
        setMessage('Your email is verified. You can now sign in to AGP Studios.');
      })
      .catch((reason) => {
        setStatus('error');
        setMessage(reason instanceof Error ? reason.message : 'This verification link is invalid or expired.');
      });
  }, [token]);

  return <main className="vesper-auth-page auth-action-page"><section className="vesper-auth-card"><span className="eyebrow">AGP Studios</span><h1>{status === 'loading' ? 'Verifying email' : status === 'success' ? 'Email verified' : 'Verification failed'}</h1><p className={status === 'error' ? 'auth-error' : 'muted'}>{message}</p><a className="primary-button auth-action-link" href="/">Return to AGP Studios</a></section></main>;
}

function PasswordResetPage() {
  const token = new URLSearchParams(window.location.search).get('token') || '';
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    setMessage('');
    if (!token) { setError('This password reset link is missing its token.'); return; }
    if (password.length < 8) { setError('Password must be at least 8 characters.'); return; }
    if (password !== confirmation) { setError('Passwords do not match.'); return; }
    setBusy(true);
    try {
      const response = await fetch('/api/auth/password-reset/confirm', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, password }),
      });
      const data = await response.json() as { error?: { message?: string; code?: string } };
      if (!response.ok) throw new Error(data.error?.message || data.error?.code || 'This reset link is invalid or expired.');
      setComplete(true);
      setMessage('Your password has been changed. You can now sign in.');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Password reset failed.');
    } finally {
      setBusy(false);
    }
  }

  return <main className="vesper-auth-page auth-action-page"><section className="vesper-auth-card"><span className="eyebrow">AGP Studios</span><h1>Reset your password</h1>{complete ? <><p className="auth-success">{message}</p><a className="primary-button auth-action-link" href="/">Return to sign in</a></> : <form onSubmit={(event) => void submit(event)}><input value={password} onChange={(event) => setPassword(event.target.value)} placeholder="New password" type="password" autoComplete="new-password" required /><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="Confirm password" type="password" autoComplete="new-password" required /><button type="submit" className="primary-button" disabled={busy}>{busy ? 'Saving…' : 'Set new password'}</button>{error && <small className="auth-error">{error}</small>}</form>}</section></main>;
}

function VesperAuthPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const redirect = new URLSearchParams(window.location.search).get('redirect') || '';

  function callbackUrl() {
    try {
      const url = new URL(redirect);
      if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.pathname !== '/callback' || !url.port) return null;
      return url;
    } catch { return null; }
  }

  // If the user is already signed in (cookie session), mint a Vesper bearer
  // session and hand the token to the desktop app's localhost callback.
  useEffect(() => {
    const callback = callbackUrl();
    if (!callback) return;
    let cancelled = false;
    fetch('/api/auth/session', { credentials: 'same-origin' })
      .then((response) => response.json())
      .then(async (data: { authenticated?: boolean }) => {
        if (cancelled || !data.authenticated) return;
        const mint = await fetch('/api/v1/auth/token-from-session', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'X-CSRF-Token': csrfToken() },
        });
        const minted = await mint.json() as { session_token?: string; user_id?: string; username?: string; role?: string };
        if (cancelled || !mint.ok || !minted.session_token || !minted.user_id || !minted.username || !minted.role) return;
        callback.search = new URLSearchParams({ token: minted.session_token, user_id: minted.user_id, username: minted.username, role: minted.role }).toString();
        window.location.assign(callback.toString());
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault(); setError('');
    const callback = callbackUrl();
    if (!callback) { setError('Invalid Vesper callback. Start the sign-in flow again from the desktop app.'); return; }
    setBusy(true);
    try {
      const response = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
      const data = await response.json() as { session_token?: string; user_id?: string; username?: string; role?: string; error?: string | { code?: string; message?: string } };
      if (!response.ok || !data.session_token || !data.user_id || !data.username || !data.role) {
        const detail = typeof data.error === 'string' ? data.error : data.error?.message || data.error?.code;
        throw new Error(detail || 'Invalid credentials');
      }
      callback.search = new URLSearchParams({ token: data.session_token, user_id: data.user_id, username: data.username, role: data.role }).toString();
      window.location.assign(callback.toString());
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Sign-in failed'); setBusy(false); }
  }

  return <main className="vesper-auth-page"><section className="vesper-auth-card"><span className="eyebrow">Vesper Studios</span><h1>Sign in to Vesper</h1><p className="muted">Authorize the Vesper desktop app through AGP Studios.</p><form onSubmit={(event) => void submit(event)}><input value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Username or email" autoComplete="username" required /><input value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" type="password" autoComplete="current-password" required /><button type="submit" className="primary-button" disabled={busy}>{busy ? 'Signing in…' : 'Continue to Vesper'}</button>{error && <small className="auth-error">{error}</small>}</form></section></main>;
}

function StudioFooter({ navigate }: { navigate: (path: string) => void }) {
  return <footer className="studio-footer"><div><strong className="footer-brand">AGP<span>Studios</span></strong><p>Independent software, games, and tools built with purpose.</p></div><div><span className="footer-label">Explore</span><button onClick={() => navigate('/projects')}>Projects</button><button onClick={() => navigate('/games')}>Games</button><button onClick={() => navigate('/community')}>Community</button></div>      <div><span className="footer-label">Resources</span><button onClick={() => navigate('/support')}>Support</button></div><div><span className="footer-label">AGP Studios</span><button onClick={() => navigate('/privacy')}>Privacy</button><button onClick={() => navigate('/terms')}>Terms</button></div><div className="footer-bottom">© {new Date().getFullYear()} AGP Studios</div></footer>;
}

function TelemetrySection({ user, telemetry, telemetryError, updatedAt }: { user: User | null; telemetry: TelemetryResponse | null; telemetryError: string | null; updatedAt: Date | null }) {
  if (!user) return null;
  return <section className="telemetry-section"><div className="section-heading"><div><span className="eyebrow">Live ecosystem</span><h2>Agent telemetry</h2></div><div className="refresh-state">{telemetryError ? telemetryError : updatedAt ? `Updated ${updatedAt.toLocaleTimeString()}` : 'Connecting...'}</div></div>{telemetry && <div className="telemetry-summary"><Metric label="Active requests" value={telemetry.servers.reduce((total, server) => total + server.active_requests, 0).toLocaleString()} /><Metric label="Requests · 5 min" value={telemetry.servers.reduce((total, server) => total + server.requests_last_5m, 0).toLocaleString()} /><Metric label="Updated" value={new Date(telemetry.updated_at * 1000).toLocaleTimeString()} /></div>}<div className="server-grid">{telemetry?.servers.map((server) => <ServerCard key={server.id} server={server} />)}{!telemetry && !telemetryError && <div className="loading-card">Connecting to AshatHub...</div>}{telemetryError && <div className="loading-card error-card">{telemetryError}</div>}</div></section>;
}
