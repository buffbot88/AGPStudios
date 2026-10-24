import { useEffect, useState } from 'react';
import { request } from './api';

type Ticket = { ticket: string; expires_in: number; user: { id: string; username: string } };

const errors: Record<string, string> = {
  suspended: "This account can't access World of Ra.",
  alpha_not_granted: "You're not in the World of Ra alpha yet. Contact support if you think this is a mistake.",
  mint_unavailable: 'The sign-in service is temporarily unavailable. Please try again shortly.',
  ticket_unavailable: 'World of Ra sign-in is temporarily unavailable. Please try again shortly.',
  ticket_rate_limited: 'Too many codes requested. Wait a few minutes and try again.',
  authentication_required: 'Sign in to AGP Studios to continue.',
};

export function WorldOfRaHandoff({ signedIn }: { signedIn: boolean }) {
  const [ticket, setTicket] = useState('');
  const [expiresAt, setExpiresAt] = useState(0);
  const [remaining, setRemaining] = useState(0);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!expiresAt) return;
    const update = () => {
      const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
      setRemaining(seconds);
      if (!seconds) {
        setTicket('');
        setExpiresAt(0);
        setExpired(true);
      }
    };
    update();
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [expiresAt]);

  async function issueTicket() {
    if (!signedIn) {
      window.dispatchEvent(new CustomEvent('agp:signin'));
      return;
    }
    setBusy(true);
    setError('');
    setExpired(false);
    setCopied(false);
    try {
      const result = await request<Ticket>('/api/game/ticket', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      setTicket(result.ticket);
      setRemaining(result.expires_in);
      setExpiresAt(Date.now() + result.expires_in * 1000);
    } catch (reason) {
      const code = reason instanceof Error ? reason.message : '';
      setError(errors[code] ?? 'Could not create a sign-in code. Please try again.');
    } finally {
      setBusy(false);
    }
  }

  async function copyTicket() {
    try {
      await navigator.clipboard.writeText(ticket);
      setCopied(true);
    } catch {
      setError('Clipboard access was blocked. Select and copy the code instead.');
    }
  }

  const minutes = Math.floor(remaining / 60).toString().padStart(2, '0');
  const seconds = (remaining % 60).toString().padStart(2, '0');

  return (
    <section className="member-section world-ra-handoff" aria-labelledby="world-ra-title">
      <span className="eyebrow">World of Ra · Alpha</span>
      <h1 id="world-ra-title">Sign in to World of Ra</h1>
      <p>Generate a one-time code, then enter it in the game to link your AGP Studios account.</p>
      {!ticket ? (
        <>
        {expired && <p role="status">Code expired — get a new one.</p>}
        <button type="button" className="primary-button" onClick={() => void issueTicket()} disabled={busy}>
          {busy ? 'Creating code…' : signedIn ? 'Get sign-in code' : 'Sign in to continue'}
        </button>
        </>
      ) : (
        <div className="world-ra-ticket" aria-live="polite">
          <code>{ticket}</code>
          <p>Expires in <strong>{minutes}:{seconds}</strong></p>
          <button type="button" className="primary-button" onClick={() => void copyTicket()}>
            {copied ? 'Copied' : 'Copy code'}
          </button>
        </div>
      )}
      {error && <p className="auth-error" role="alert">{error}</p>}
      {!signedIn && <p className="muted">Sign in with your AGP Studios account first. You’ll return here after signing in.</p>}
    </section>
  );
}
