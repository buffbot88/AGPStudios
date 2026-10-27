import { useEffect, useState } from 'react';
import './styles.css';
import { fetchGames, fetchNews, FEATURED_GAME, FEATURED_VIDEO, type Game, type NewsItem } from './content';
import { SiteHeader } from './components/home/SiteHeader';
import { HeroSection } from './components/home/HeroSection';
import { QuickInfoCard } from './components/home/QuickInfoCard';
import { SectionHeader } from './components/home/primitives';
import { SiteFooter } from './components/home/SiteFooter';
import { MemberSurfaces } from './components/MemberSurfaces';
import { AdminSurface } from './components/AdminSurface';
import { WorldOfRaHandoff } from './components/WorldOfRaHandoff';
import { SurfaceErrorBoundary } from './components/SurfaceErrorBoundary';
import { AuthPanel } from './components/AuthPanel';
import { getSession, onSessionChange, onSessionExpired, peekSession, setSessionUser } from './components/session';
import { ToastHost, toast } from './components/toast';
import { safeText } from './components/api';
import { installGlobalErrorHandlers } from './components/globalErrors';

type User = {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: string;
};

function AccountIcon() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 19.5a7 7 0 0 1 14 0" strokeLinecap="round" />
    </svg>
  );
}

type Route = 'home' | 'account' | 'support' | 'admin' | 'world-of-ra';

function routeFromHash(): Route {
  // AdminSurface deep-links tabs as /admin#users etc.; the path, not just the
  // hash, must keep the admin route across a reload.
  const path = window.location.pathname.replace(/\/+$/, '');
  if (['return', 'cancel'].includes(new URLSearchParams(window.location.search).get('acoins') || '')) return 'account';
  if (path.endsWith('/admin')) return 'admin';
  if (path === '/play/world-of-ra') return 'world-of-ra';
  switch (window.location.hash) {
    case '#account':
      return 'account';
    case '#support':
      return 'support';
    case '#admin':
      return 'admin';
    default:
      return 'home';
  }
}

function App() {
  // Initialize from the hydrated cache so a signed-in visitor rendering
  // #admin/#account on a warm load isn't bounced before the store resolves.
  const [user, setUser] = useState<User | null>(() => peekSession() ?? null);
  const [route, setRoute] = useState<Route>(() => routeFromHash());
  const [games, setGames] = useState<Game[] | null>(null);
  const [news, setNews] = useState<NewsItem[] | null>(null);
  const sessionResolved = peekSession() !== undefined;

  useEffect(() => {
    const onHash = () => {
      setRoute(routeFromHash());
      window.scrollTo(0, 0);
    };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  // One shared session fetch for the whole app; stay in sync with
  // login/logout and with background revalidation of a hydrated session.
  useEffect(() => {
    void getSession();
    return onSessionChange(setUser);
  }, []);

  // Surface uncaught errors and unhandled promise rejections as toasts; these
  // bypass the React error boundary and would otherwise fail silently.
  useEffect(() => installGlobalErrorHandlers(), []);

  // If background revalidation discovers the session expired server-side,
  // tell the user gently and land them somewhere safe.
  useEffect(() => {
    return onSessionExpired(() => {
      toast('Your session has ended. Sign in again to continue.', 'info', 7000);
      if (window.location.hash && window.location.hash !== '#top') {
        window.location.hash = '#top';
        setRoute('home');
      }
    });
  }, []);

  useEffect(() => {
    void fetchGames().then(({ games: next }) => setGames(next));
    void fetchNews().then(({ news: next }) => setNews(next));
  }, []);

  // Restrained scroll reveals for homepage sections. Re-observes after
  // every render so content that arrives late (games, news) is covered;
  // already-revealed elements are skipped and unobserved once shown.
  useEffect(() => {
    if (typeof IntersectionObserver !== 'function') {
      document.querySelectorAll('.reveal').forEach((element) => element.classList.add('is-visible'));
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.12 },
    );
    document.querySelectorAll('.reveal:not(.is-visible)').forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  });

  // Route guards: #account requires a session, #admin requires the admin role.
  // Guard only after the session has resolved so a signed-in admin visiting
  // #admin directly isn't bounced by the initial null user.
  useEffect(() => {
    if (!sessionResolved) return;
    if (route === 'admin' && user?.role?.toLowerCase() !== 'admin') {
      toast(user ? 'Administrator access required.' : 'Sign in with an administrator account to view the admin panel.');
      window.location.hash = '#top';
      setRoute('home');
    } else if (route === 'account' && !user) {
      toast('Sign in to view your account.');
      window.location.hash = '#top';
      setRoute('home');
    }
  }, [route, user, sessionResolved]);

  // Sign out via the shared store: the API revokes the session, the store
  // write notifies every subscriber (header, guards) in one place.
  const logout = () =>
    fetch('/api/auth/logout', { method: 'POST', credentials: 'same-origin' }).then(() => {
      setSessionUser(null);
      window.location.assign('/');
    });

  const liveGames = (games ?? [FEATURED_GAME]).filter((game) => game.status === 'live');
  const featuredGames = liveGames.length ? liveGames : (games ?? [FEATURED_GAME]);
  const latestNews = news?.[0] ?? {
    title: 'Paws & Parcels and Everenfell are live',
    summary: 'Both game servers are online. Sign in to manage your account and jump in.',
    url: '#news',
  };
  const newsList = news ?? [latestNews];

  return (
    <div className="portal" id="top">
      <SiteHeader user={user} onUserChange={setUser} />
      <main>
        {route === 'admin' && (
          <SurfaceErrorBoundary fallbackTitle="The admin panel couldn't load">
            <AdminSurface user={user} />
          </SurfaceErrorBoundary>
        )}
        {route === 'world-of-ra' && <WorldOfRaHandoff signedIn={!!user} />}
        {route === 'account' &&
          (user ? (
            <SurfaceErrorBoundary fallbackTitle="Your account couldn't load">
              <MemberSurfaces user={user} initialTab="account" />
            </SurfaceErrorBoundary>
          ) : (
            <section className="member-section workspace-locked">
              <span className="eyebrow">AGP Studios Member</span>
              <h2>Sign in to manage your account</h2>
              <p>Your account, preferences, and community tools live here.</p>
              <AuthPanel embedded onChange={setSessionUser} />
            </section>
          ))}
        {route === 'support' && (
          <SurfaceErrorBoundary fallbackTitle="Support couldn't load">
            <MemberSurfaces user={user} initialTab="support" />
          </SurfaceErrorBoundary>
        )}
        {route === 'home' && (
          <>
        <HeroSection
          games={featuredGames}
          signedIn={!!user}
          account={
            <>
              <QuickInfoCard
                icon={<AccountIcon />}
                eyebrow="Your Account"
                title={user ? `Welcome back, ${safeText(user.display_name) || safeText(user.username)}` : 'Your Account'}
                description={
                  user
                    ? 'Manage your account, support tickets, and access.'
                    : 'Sign in to access your account and community tools.'
                }
                link={user ? 'Go to Account' : 'Sign In'}
                href={user ? '#account' : '#top'}
                onAction={user ? undefined : () => window.dispatchEvent(new CustomEvent('agp:signin'))}
              />
              {user && (
                <button type="button" className="link-action signout-link" onClick={logout}>
                  Sign out
                </button>
              )}
            </>
          }
        />

        <section className="section reveal" id="news" aria-labelledby="news-title">
          <div className="news-card news-list-panel">
            <SectionHeader title="Studio News" text="Recent commits from across the studio." />
            <ol className="news-rows">
              {newsList.slice(0, 10).map((item, index) => {
                const href = safeText(item.url, '#news');
                const external = href.startsWith('http');
                return (
                  <li key={`${safeText(item.title)}-${index}`}>
                    <a
                      className="news-row-title"
                      href={href}
                      {...(external ? { target: '_blank', rel: 'noreferrer' } : {})}
                    >
                      {safeText(item.title)}
                    </a>
                    {safeText(item.summary) && (
                      <span className="news-row-summary">{safeText(item.summary)}</span>
                    )}
                  </li>
                );
              })}
            </ol>
          </div>
        </section>

        <section className="section reveal" id="featured-video" aria-labelledby="featured-video-title">
          <div className="news-card news-list-panel">
            <SectionHeader title={FEATURED_VIDEO.title} text="Latest from the studio." />
          <div className="video-frame">
            <iframe
              src={FEATURED_VIDEO.embedUrl}
              title={FEATURED_VIDEO.title}
              loading="lazy"
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
              referrerPolicy="strict-origin-when-cross-origin"
              allowFullScreen
            />
          </div>
          <p className="community-note">
            <a className="link-action" href={FEATURED_VIDEO.watchUrl} target="_blank" rel="noreferrer">
              Watch on YouTube <span aria-hidden>→</span>
            </a>
          </p>
          </div>
        </section>
          </>
        )}
      </main>
      <SiteFooter />
      <ToastHost />
    </div>
  );
}

export default App;
