import { HERO } from '../../content';
import { FeaturedGameCard } from './FeaturedGameCard';
import type { Game } from '../../content';

export function HeroSection({ games }: { games: Game[] }) {
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
      </div>
      <div className="hero-featured">
        <FeaturedGameCard games={games} />
      </div>
    </section>
  );
}
