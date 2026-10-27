import { useCallback, useEffect, useRef, useState } from 'react';
import { NAV_LINKS, goHome } from '../../content';
import { AuthPanel } from '../AuthPanel';
import { request, safeList, safeNumber, safeText, safeTime } from '../api';
import type { User } from './primitives';

type NotificationItem = {
  id: unknown; kind: unknown; subject: unknown; message: unknown;
  created_at: unknown; read: unknown; ticket_id?: unknown;
};

function NotificationBell() {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const wrapRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await request<{ notifications: NotificationItem[]; unread_count: number }>('/api/notifications');
      setItems(safeList(data.notifications));
      setUnread(safeNumber(data.unread_count, 0));
    } catch {
      setError('Notifications unavailable.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    if (!open) return;
    void load();
    const onPointer = (event: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, load]);

  const markAll = async () => {
    try {
      await request('/api/notifications/read', { method: 'POST', body: JSON.stringify({ all: true }) });
      setItems((prev) => prev.map((item) => ({ ...item, read: true })));
      setUnread(0);
    } catch {
      setError('Could not mark notifications read.');
    }
  };

  const markOne = async (id: string) => {
    try {
      await request('/api/notifications/read', { method: 'POST', body: JSON.stringify({ ids: [id] }) });
      setItems((prev) => prev.map((item) => (safeText(item.id) === id ? { ...item, read: true } : item)));
      setUnread((count) => Math.max(0, count - 1));
    } catch {
      setError('Could not mark that notification read.');
    }
  };

  return (
    <div className="notif-wrapper" ref={wrapRef}>
      <button
        type="button"
        className="icon-button notif-bell"
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
        aria-expanded={open}
        aria-haspopup="dialog"
        onClick={() => setOpen((value) => !value)}
      >
        <svg viewBox="0 0 20 20" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.6">
          <path d="M10 3a4.5 4.5 0 0 0-4.5 4.5c0 3.5-1.5 5-1.5 5h12s-1.5-1.5-1.5-5A4.5 4.5 0 0 0 10 3Z" strokeLinejoin="round" />
          <path d="M8.5 15.5a1.5 1.5 0 0 0 3 0" strokeLinecap="round" />
        </svg>
        {unread > 0 && <span className="notif-badge" aria-hidden>{unread > 9 ? '9+' : unread}</span>}
      </button>
      {open && (
        <div className="notif-popover" role="dialog" aria-label="Notifications">
          <div className="notif-head">
            <strong>Notifications</strong>
            <button type="button" className="notif-action" onClick={() => void markAll()} disabled={unread === 0}>
              Mark all read
            </button>
          </div>
          {loading && items.length === 0 ? (
            <p className="notif-empty">Loading…</p>
          ) : error && items.length === 0 ? (
            <p className="notif-empty">{error}</p>
          ) : items.length === 0 ? (
            <p className="notif-empty">You are all caught up.</p>
          ) : (
            <ul className="notif-list">
              {items.map((item) => {
                const id = safeText(item.id);
                const isRead = item.read === true;
                const kind = safeText(item.kind) === 'ticket_reply' ? 'Support reply' : 'Account';
                return (
                  <li key={id} className={isRead ? 'notif-item read' : 'notif-item'}>
                    <a
                      href={safeText(item.kind) === 'ticket_reply' ? '#support' : '#account'}
                      onClick={() => {
                        setOpen(false);
                        if (!isRead && id) void markOne(id);
                      }}
                    >
                      <span className="notif-kind">{kind}</span>
                      <span className="notif-subject">{safeText(item.subject, 'Update')}</span>
                      {safeText(item.message) && <span className="notif-message">{safeText(item.message)}</span>}
                      <span className="notif-time">{safeTime(item.created_at)}</span>
                    </a>
                    {!isRead && id && (
                      <button type="button" className="notif-action" onClick={() => void markOne(id)}>
                        Mark read
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function Wordmark() {
  return (
    <a className="wordmark" href="/" onClick={(event) => { event.preventDefault(); goHome(); }}>
      <b>AGP</b> STUDIOS
    </a>
  );
}

export function SiteHeader({ user, onUserChange }: { user: User | null; onUserChange: (user: User | null) => void }) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [onlineUsers, setOnlineUsers] = useState<number | null>(null);
  // The homepage is a clean `/` (no `#top` hash); legacy `#top` URLs count as home too.
  const homeActive = () => (!window.location.hash || window.location.hash === '#top' ? '/' : window.location.hash);
  const [active, setActive] = useState<string>(() => homeActive());

  useEffect(() => {
    const onHash = () => {
      setActive(homeActive());
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
  // Support is a member area: signed-out visitors get Home only (plus Admin
  // for admins, who are always signed in).
  const visibleLinks = NAV_LINKS.filter((link) => user || link.href !== '#support');

  return (
    <header className="site-header">
      <div className="site-header-inner">
        <Wordmark />
        <nav className="main-nav" aria-label="Primary">
          {visibleLinks.map((link) => (
            <a key={link.href} href={link.href} className={active === link.href ? 'active' : undefined} onClick={link.href === '/' ? (event) => { event.preventDefault(); goHome(); } : undefined}>
              {link.label}
            </a>
          ))}
          {isAdmin && <a href="#admin">Admin</a>}
        </nav>
        <div className="header-tools">
          {user && <NotificationBell />}
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
          {visibleLinks.map((link) => (
            <a key={link.href} href={link.href} className={active === link.href ? 'active' : undefined} onClick={link.href === '/' ? (event) => { event.preventDefault(); goHome(); } : undefined}>
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
