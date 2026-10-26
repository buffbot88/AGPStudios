import { useEffect, useState } from 'react';
import { NAV_LINKS } from '../../content';
import { AuthPanel } from '../AuthPanel';
import type { User } from './primitives';

function Wordmark() {
  return (
    <a className="wordmark" href="#top">
      <b>AGP</b> STUDIOS
    </a>
  );
}

export function SiteHeader({ user, onUserChange }: { user: User | null; onUserChange: (user: User | null) => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [onlineUsers, setOnlineUsers] = useState<number | null>(null);
  const [active, setActive] = useState<string>(() => window.location.hash || '#top');

  useEffect(() => {
    const onHash = () => {
      setActive(window.location.hash || '#top');
      setMenuOpen(false);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    let visitorId: string = crypto.randomUUID();
    try {
      visitorId = localStorage.getItem('agp-presence-id') || visitorId;
      localStorage.setItem('agp-presence-id', visitorId);
    } catch {
      // Storage can be unavailable in private browsing; this tab still counts.
    }
    let mounted = true;
    const heartbeat = async () => {
      try {
        const response = await fetch('/api/online-presence', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ visitor_id: visitorId }),
        });
        const result = await response.json() as { online_users?: unknown };
        if (mounted && response.ok && typeof result.online_users === 'number') {
          setOnlineUsers(result.online_users);
        }
      } catch {
        // Leave the last known count visible during a brief network outage.
      }
    };
    void heartbeat();
    const timer = window.setInterval(() => void heartbeat(), 30_000);
    return () => { mounted = false; window.clearInterval(timer); };
  }, []);

  const isAdmin = user?.role?.toLowerCase() === 'admin';

  return (
    <header className="site-header">
      <div className="site-header-inner">
        <Wordmark />
        <nav className="main-nav" aria-label="Primary">
          {NAV_LINKS.map((link) => (
            <a key={link.href} href={link.href} className={active === link.href ? 'active' : undefined}>
              {link.label}
            </a>
          ))}
          {isAdmin && <a href="#admin">Admin</a>}
        </nav>
        <div className="header-tools">
          <button type="button" className="icon-button" aria-label="Search">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6">
              <circle cx="9" cy="9" r="5.5" />
              <path d="m13.5 13.5 3.5 3.5" strokeLinecap="round" />
            </svg>
          </button>
          <button type="button" className="icon-button" aria-label="Notifications">
            <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6">
              <path d="M10 3a4.5 4.5 0 0 0-4.5 4.5c0 3.5-1.5 5-1.5 5h12s-1.5-1.5-1.5-5A4.5 4.5 0 0 0 10 3Z" strokeLinejoin="round" />
              <path d="M8.5 15.5a1.5 1.5 0 0 0 3 0" strokeLinecap="round" />
            </svg>
          </button>
          <AuthPanel onChange={onUserChange} onNavigate={(hash) => { window.location.hash = hash; }} />
          <span className="online-count" role="status" aria-label={onlineUsers === null ? 'Online user count loading' : `${onlineUsers} people online`}>
            <span className="online-dot" aria-hidden="true" />
            <span>{onlineUsers ?? '—'}<span className="online-label"> online</span></span>
          </span>
          <button
            type="button"
            className="menu-toggle"
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((open) => !open)}
          >
            {menuOpen ? '×' : '☰'}
          </button>
        </div>
      </div>
      {menuOpen && (
        <nav className="mobile-nav" aria-label="Mobile">
          {NAV_LINKS.map((link) => (
            <a key={link.href} href={link.href} className={active === link.href ? 'active' : undefined}>
              {link.label}
            </a>
          ))}
          {isAdmin && <a href="#admin">Admin</a>}
          {!user && <a href="#top" onClick={(event) => { event.preventDefault(); setMenuOpen(false); window.dispatchEvent(new CustomEvent('agp:signin')); }}>Sign in</a>}
        </nav>
      )}
    </header>
  );
}
