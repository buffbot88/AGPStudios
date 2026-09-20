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
  const [active, setActive] = useState<string>(() => window.location.hash || '#top');

  useEffect(() => {
    const onHash = () => {
      setActive(window.location.hash || '#top');
      setMenuOpen(false);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
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
          <a
            className="play-button"
            href={user ? 'https://pawsandparcels.agpstudios.org/?auth=1' : undefined}
            onClick={(event) => {
              if (!user) {
                event.preventDefault();
                window.dispatchEvent(new CustomEvent('agp:signin'));
              }
            }}
          >
            Play <span aria-hidden>→</span>
          </a>
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
