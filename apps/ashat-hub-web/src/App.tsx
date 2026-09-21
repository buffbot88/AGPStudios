import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import './styles.css';
import { fetchGames, fetchNews, FEATURED_GAME, type Game, type NewsItem } from './content';
import { SiteHeader } from './components/home/SiteHeader';
import { HeroSection } from './components/home/HeroSection';
import { QuickInfoCard } from './components/home/QuickInfoCard';
import { GameCard } from './components/home/GameCard';
import { SectionHeader } from './components/home/primitives';
import { SiteFooter } from './components/home/SiteFooter';
import { MemberSurfaces } from './components/MemberSurfaces';
import { AdminSurface } from './components/AdminSurface';
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

function GameIcon() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <path d="M6 9h12a4 4 0 0 1 4 4v2a3 3 0 0 1-5.6 1.5L15.5 15h-7l-.9 1.5A3 3 0 0 1 2 15v-2a4 4 0 0 1 4-4Z" strokeLinejoin="round" />
      <path d="M8 12.2v2.2M6.9 13.3h2.2" strokeLinecap="round" />
      <circle cx="15.5" cy="12.4" r=".9" fill="currentColor" stroke="none" />
      <circle cx="17.6" cy="14.2" r=".9" fill="currentColor" stroke="none" />
    </svg>
  );
}

function NewsIcon() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <rect x="4" y="4.5" width="16" height="15" rx="2.5" />
      <path d="M8 9h8M8 12.5h8M8 16h5" strokeLinecap="round" />
    </svg>
  );
}

function AccountIcon() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden>
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M5 19.5a7 7 0 0 1 14 0" strokeLinecap="round" />
    </svg>
  );
}

type Route = 'home' | 'account' | 'support' | 'admin';

function routeFromHash(): Route {
  // AdminSurface deep-links tabs as /admin#users etc.; the path, not just the
  // hash, must keep the admin route across a reload.
  if (window.location.pathname.replace(/\/+$/, '').endsWith('/admin')) {
    return 'admin';
  }
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

  const featuredGame = games?.find((game) => game.status === 'live') ?? games?.[0] ?? FEATURED_GAME;
  const latestNews = news?.[0] ?? {
    title: 'A New Chapter Ahead',
    summary: 'We\'re working on new worlds, new ideas, and more ways to play together.',
    url: '#news',
  };
  const newsList = news ?? [
    {
      title: 'A New Chapter Ahead',
      summary: 'We\'re working on new worlds, new ideas, and more ways to play together.',
      url: '#news',
    },
  ];

  return (
    <div className="portal" id="top">
      <SiteHeader user={user} onUserChange={setUser} />
      <main>
        {route === 'admin' && (
          <SurfaceErrorBoundary fallbackTitle="The admin panel couldn't load">
            <AdminSurface user={user} />
          </SurfaceErrorBoundary>
        )}
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
        <HeroSection games={games ?? [FEATURED_GAME]} user={user} />

        <section className="quick-row" aria-label="Highlights">
          <QuickInfoCard
            icon={<GameIcon />}
            eyebrow="Featured Game"
            title={safeText(featuredGame.title)}
            description={safeText(featuredGame.summary)}
            link="Play Now"
            href={user ? `${featuredGame.url}/?auth=1` : featuredGame.url}
          />
          <QuickInfoCard
            icon={<NewsIcon />}
            eyebrow="Studio News"
            title={latestNews.title}
            description={latestNews.summary}
            link="Read the Latest"
            href={latestNews.url}
          />
          <QuickInfoCard
            icon={<AccountIcon />}
            eyebrow="Your Account"
            title={user ? `Welcome back, ${safeText(user.display_name) || safeText(user.username)}` : 'Your Account'}
            description={
              user
                ? 'Manage your account, preferences, and more.'
                : 'Sign in to access your account, updates, and community tools.'
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
        </section>

        <section className="section" id="games" aria-labelledby="games-title">
          <SectionHeader title="Our Games" text="Different worlds. A shared home." action="View All Games" href="#games" />
          <div className="games-grid">
            {(games ?? [FEATURED_GAME]).map((game) => (
              <GameCard key={safeText(game.slug)} game={game} />
            ))}
          </div>
        </section>

        <section className="section" id="news" aria-labelledby="news-title">
          <SectionHeader title="Studio News" text="Updates from across the studio." action="View All News" href="#news" />
          <div className="news-list-panel">
            {newsList.map((item, index) => (
              <article className="news-panel" key={`${safeText(item.title)}-${index}`}>
                {index === 0 && <span className="eyebrow">Latest</span>}
                <h3>{safeText(item.title)}</h3>
                <p>{safeText(item.summary)}</p>
                <a className="link-action" href={safeText(item.url, '#news')}>
                  Read the Latest <span aria-hidden>→</span>
                </a>
              </article>
            ))}
          </div>
        </section>

        <section className="section" id="community" aria-labelledby="community-title">
          <SectionHeader title="Community" text="Play together. A brighter tomorrow." />
          <p className="community-note">
            Community spaces are opening soon. Sign in and stay tuned — {user ? 'see you in the worlds.' : 'more is on the way.'}
          </p>
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
