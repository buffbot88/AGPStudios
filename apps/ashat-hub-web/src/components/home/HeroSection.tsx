import { HERO } from '../../content';
import { FeaturedGameCard } from './FeaturedGameCard';
import type { Game } from '../../content';
import type { User } from './primitives';

export function HeroSection({ games, user }: { games: Game[]; user: User | null }) {
  const featured = games.find((game) => game.status === 'live') ?? games[0];
  const playHref = featured && user ? `${featured.url}/?auth=1` : undefined;
  return (
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-copy">
        <span className="eyebrow">{HERO.eyebrow}</span>
        <h1 id="hero-title">
          {HERO.headlineTop}
          <br />
          <em>{HERO.headlineEmphasis}</em>
        </h1>
        <p className="hero-description">{HERO.description}</p>
        <div className="hero-ctas">
          <a className="primary-button large" href={HERO.primaryCta.href}>
            {HERO.primaryCta.label} <span aria-hidden>→</span>
          </a>
          <a className="secondary-button" href={HERO.secondaryCta.href}>
            <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
              <rect x="2.5" y="2.5" width="11" height="11" rx="2" />
              <path d="M5.5 6.5h5M5.5 9h5" strokeLinecap="round" />
            </svg>
            {HERO.secondaryCta.label}
          </a>
        </div>
      </div>
      <div className="hero-featured">
        <FeaturedGameCard games={games} user={user} />
      </div>
    </section>
  );
}
