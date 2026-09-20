import { FormEvent, useEffect, useRef, useState } from 'react';
import { API, csrfToken, errorMessage, safeText, type ApiError } from './api';
import { getSession, onSessionChange, setSessionUser, type SessionUser } from './session';

type User = SessionUser;
type AuthMode = 'login' | 'register' | 'reset-request';

export type AuthUser = User;

export function AuthPanel({ onChange, onNavigate, embedded = false }: { onChange: (user: User | null) => void; onNavigate?: (path: string) => void; embedded?: boolean }) {
  const [user, setUser] = useState<User | null>(null);
  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [mode, setMode] = useState<AuthMode>('login');
  const [email, setEmail] = useState('');
  const [verificationEmail, setVerificationEmail] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDetailsElement>(null);

  // Subscribe to the shared session store: one network fetch app-wide, and
  // local state follows any login/logout/expiry that happens anywhere. The
  // store is the source of truth — onChange is only notified of changes.
  const lastReported = useRef<User | null | undefined>(undefined);
  useEffect(() => {
    let mounted = true;
    const sync = (next: User | null) => {
      if (!mounted) return;
      setUser(next);
      if (lastReported.current !== next) {
        lastReported.current = next;
        onChange(next);
      }
    };
    void getSession().then(sync);
    return onSessionChange(sync);
  }, [onChange]);

  useEffect(() => {
    if (!isOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setIsOpen(false); };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isOpen]);

  useEffect(() => {
    const open = () => {
      openMode('login');
      setIsOpen(true);
    };
    window.addEventListener('agp:signin', open);
    return () => window.removeEventListener('agp:signin', open);
  }, []);

  function openMode(nextMode: AuthMode) {
    setMode(nextMode);
    setError('');
    setMessage('');
    setPassword('');
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError('');
    setMessage('');
    setBusy(true);
    try {
      if (mode === 'reset-request') {
        const response = await fetch(`${API}/auth/password-reset/request`, {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ identifier }),
        });
        const data = await response.json() as { error?: ApiError };
        if (!response.ok) {
          setError(errorMessage(data.error, 'Unable to request a password reset'));
          return;
        }
        setMessage('If that account exists, a password reset link will arrive by email.');
        return;
      }

      const response = await fetch(`${API}/auth/${mode}`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(mode === 'login'
          ? { identifier, password }
          : { username: identifier, email, password, display_name: identifier }),
      });
      const data = await response.json() as {
        user?: User;
        registered?: boolean;
        verification_required?: boolean;
        email?: string;
        error?: ApiError;
      };
      if (!response.ok) {
        const errorCode = typeof data.error === 'string' ? data.error : data.error?.code;
        if (mode === 'login' && errorCode === 'email_verification_required') {
          setVerificationEmail(identifier);
          setMessage('Verify your email address before signing in, then request a new link if needed.');
          setError('');
        } else {
          setError(errorMessage(data.error, mode === 'login' ? 'Login failed' : 'Registration failed'));
        }
        return;
      }
      if (mode === 'register') {
        if (data.verification_required) {
          const nextEmail = data.email || email;
          setVerificationEmail(nextEmail);
          setIdentifier(nextEmail);
          setMode('login');
          setPassword('');
          setMessage(`Account created. Check ${nextEmail} for a verification link.`);
          return;
        }
        setMode('login');
        setPassword('');
        setMessage('Account created. Sign in to continue.');
        return;
      }
      if (!data.user) {
        setError('Login failed');
        return;
      }
      setUser(data.user);
      setSessionUser(data.user); // store notifies subscribers; onChange fires via sync
      setIsOpen(false);
    } catch {
      setError('Authentication service unavailable');
    } finally {
      setBusy(false);
    }
  }

  async function resendVerification() {
    const target = verificationEmail || identifier;
    if (!target) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      const response = await fetch(`${API}/auth/verify-email/resend`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier: target }),
      });
      if (!response.ok) throw new Error('Unable to resend verification email');
      setMessage('If the account needs verification, a new link will arrive by email.');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to resend verification email');
    } finally {
      setBusy(false);
    }
  }

  /** Navigate the studio SPA. Paths map to hash routes; account is the menu itself. */
  function navigateFromMenu(path: string) {
    const target = path === '/community' ? '#community' : path === '/admin' ? '#admin' : '#account';
    onNavigate?.(target);
    if (window.location.hash === target) {
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    } else {
      window.location.hash = target;
    }
    menuRef.current?.removeAttribute('open');
  }

  async function logout() {
    setBusy(true); setError('');
    try {
      const response = await fetch(`${API}/auth/logout`, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': csrfToken() } });
      if (!response.ok) throw new Error('Logout failed');
      setUser(null);
      setSessionUser(null); // store notifies subscribers; onChange fires via sync
    } catch { setError('Logout failed'); }
    finally { setBusy(false); }
  }

  if (user) {
    return (
      <details ref={menuRef} className="auth-menu">
        <summary className="auth-menu-trigger"><span>{safeText(user.display_name) || safeText(user.username)}</span>{safeText(user.role).toLowerCase() === 'admin' && <span className="auth-role-badge">Admin</span>}<span className="auth-menu-chevron">⌄</span></summary>
        <div className="auth-menu-panel">
          <span className="auth-menu-email">{safeText(user.email)}</span>
          {onNavigate && <nav className="auth-menu-links" aria-label="Account navigation">
            <button type="button" className="auth-menu-link" onClick={() => navigateFromMenu('/community')}>Community</button>
            {user.role.toLowerCase() === 'admin' && <button type="button" className="auth-menu-link" onClick={() => navigateFromMenu('/admin')}>Admin</button>}
          </nav>}
          <button type="button" className="auth-logout" onClick={() => void logout()} disabled={busy}>Sign out</button>
        </div>
      </details>
    );
  }

  const title = mode === 'login' ? 'Welcome back' : mode === 'register' ? 'Create your account' : 'Reset your password';
  const sub = mode === 'login'
    ? 'Sign in to access your AGP Studios projects.'
    : mode === 'register'
      ? 'A few details and you can start building.'
      : 'Enter your username or email and we will send a reset link if the account exists.';

  const form = (
    <>
      <div className="auth-tabs" role="tablist" aria-label="Authentication mode">
        <button type="button" role="tab" aria-selected={mode === 'login'} className={`auth-tab ${mode === 'login' ? 'active' : ''}`} onClick={() => openMode('login')}>Sign in</button>
        <button type="button" role="tab" aria-selected={mode === 'register'} className={`auth-tab ${mode === 'register' ? 'active' : ''}`} onClick={() => openMode('register')}>Register</button>
      </div>
      <form className="auth-modal-form" onSubmit={(event) => void submit(event)} aria-label="Authentication">
        <div className="auth-field">
          <span className="field-icon" aria-hidden>◍</span>
          <input value={identifier} onChange={(event) => setIdentifier(event.target.value)} placeholder={mode === 'register' ? 'Username' : 'Username or email'} autoComplete="username" required />
        </div>
        {mode === 'register' && <div className="auth-field">
          <span className="field-icon" aria-hidden>✉</span>
          <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" type="email" autoComplete="email" required />
        </div>}
        {mode !== 'reset-request' && <div className="auth-field">
          <span className="field-icon" aria-hidden>⚿</span>
          <input value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} required />
        </div>}
        {mode !== 'reset-request' && <button type="button" className="github-auth-button" onClick={() => { window.location.href = `${API}/auth/github`; }}>Continue with GitHub</button>}
        <button type="submit" className="auth-submit" disabled={busy}>{mode === 'login' ? 'Sign in' : mode === 'register' ? 'Create account' : 'Send reset link'}</button>
        <div className="auth-links">
          {mode === 'login' && <button type="button" className="auth-switch" onClick={() => openMode('reset-request')}>Forgot password?</button>}
          {mode === 'login' && verificationEmail && <button type="button" className="auth-switch" onClick={() => void resendVerification()} disabled={busy}>Resend verification email</button>}
          {mode !== 'reset-request' && <button type="button" className="auth-switch primary" onClick={() => openMode(mode === 'login' ? 'register' : 'login')}>{mode === 'login' ? 'New here? Create an account' : 'Already have an account? Sign in'}</button>}
          {mode === 'reset-request' && <button type="button" className="auth-switch" onClick={() => openMode('login')}>Back to sign in</button>}
        </div>
        {message && <small className="auth-success">{message}</small>}
        {error && <small className="auth-error">{error}</small>}
      </form>
    </>
  );

  if (embedded) {
    return (
      <div className="auth-card">
        <span className="eyebrow">AGP Studios</span>
        <h2>{title}</h2>
        <p className="auth-card-sub">{sub}</p>
        {form}
      </div>
    );
  }

  return (
    <>
      <button type="button" className="auth-sign-in" onClick={() => { openMode('login'); setIsOpen(true); }}>Sign in</button>
      {isOpen && <div className="auth-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setIsOpen(false); }}>
        <section className="auth-modal" role="dialog" aria-modal="true" aria-labelledby="auth-modal-title" onMouseDown={(event) => event.stopPropagation()}>
          <div className="auth-modal-header"><span className="eyebrow">AGP Studios</span><button type="button" className="auth-modal-close" aria-label="Close sign in dialog" onClick={() => setIsOpen(false)}>×</button></div>
          <h2 id="auth-modal-title">{title}</h2>
          <p className="auth-card-sub">{sub}</p>
          {form}
        </section>
      </div>}
    </>
  );
}
