import { HERO } from '../../content';
import { FeaturedGameCard } from './FeaturedGameCard';
import type { Game } from '../../content';

export function HeroSection({ games }: { games: Game[] }) {
  return (
    <>
      <section className="hero" aria-labelledby="hero-title">
        {/* Cinematic backdrop. The layered-gradient composition below is a
            clearly marked stand-in until the Ultima3D hero vista
            (see the asset brief) is rendered and integrated. */}
        <div className="hero-art" aria-hidden />
        <span className="hero-art-badge" aria-hidden>
          Concept backdrop · Ultima3D vista pending
        </span>
        <div className="hero-inner">
          <span className="eyebrow">{HERO.eyebrow}</span>
          <h1 id="hero-title">
            {HERO.headlineTop}
            <br />
            <em>{HERO.headlineEmphasis}</em>
          </h1>
          <p className="hero-description">{HERO.description}</p>
        </div>
        <div className="hero-fade" aria-hidden />
      </section>
      <section className="worlds reveal" aria-label="Featured worlds">
        <div className="worlds-inner">
          <FeaturedGameCard games={games} />
        </div>
      </section>
    </>
  );
}
