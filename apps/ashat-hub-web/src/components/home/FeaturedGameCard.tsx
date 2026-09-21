import { useEffect, useState } from 'react';
import type { Game } from '../../content';
import { statusPill, GameTags, type User } from './primitives';
import { safeText } from '../api';
import { PosterImage } from './PosterImage';

const AUTO_ADVANCE_MS = 8000;

function Chevron({ direction }: { direction: 'left' | 'right' }) {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
      {direction === 'left' ? <path d="m14.5 5.5-6.5 6.5 6.5 6.5" strokeLinecap="round" strokeLinejoin="round" /> : <path d="m9.5 5.5 6.5 6.5-6.5 6.5" strokeLinecap="round" strokeLinejoin="round" />}
    </svg>
  );
}

/**
 * Featured game carousel. Cycles through the studio's games with slide
 * markers (01 / 03), prev/next controls, and gentle auto-advance that pauses
 * on hover or keyboard focus (and respects prefers-reduced-motion). Works
 * with or without game art.
 */
export function FeaturedGameCard({ games, user }: { games: Game[]; user: User | null }) {
  const [active, setActive] = useState(0);
  const [paused, setPaused] = useState(false);
  const count = games.length;
  const safeIndex = count ? Math.min(active, count - 1) : 0;
  const game = games[safeIndex];

  const go = (delta: number) => {
    if (count) setActive((index) => (index + delta + count) % count);
  };

  useEffect(() => {
    const reducedMotion =
      typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (paused || count < 2 || reducedMotion) return undefined;
    const timer = window.setInterval(() => setActive((index) => (index + 1) % count), AUTO_ADVANCE_MS);
    return () => window.clearInterval(timer);
  }, [paused, count, active]);

  if (!game) return null;

  const playHref = user ? `${safeText(game.url, '#games')}/?auth=1` : undefined;
  const target = (playHref ?? safeText(game.url)).startsWith('http') ? '_blank' : undefined;

  return (
    <article
      className="featured-card"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocusCapture={() => setPaused(true)}
      onBlurCapture={() => setPaused(false)}
    >
      <div className="featured-main">
        <div className="featured-meta">
          <span className="featured-label">
            <i className="dot live" aria-hidden /> Featured Game
          </span>
          {count > 1 && (
            <span className="featured-marker">
              <span className="featured-studio">AGP Studios</span>
              <i aria-hidden />
              <span className="marker-count">
                <b>{String(safeIndex + 1).padStart(2, '0')}</b> / {String(count).padStart(2, '0')}
              </span>
            </span>
          )}
        </div>
        <div className="featured-slide" key={safeText(game.slug)}>
          <div className={`featured-poster${game.poster ? '' : ' no-art'}`}>
            <PosterImage
              src={safeText(game.poster)}
              alt={game.posterAlt ?? `${safeText(game.title)} poster`}
              fallbackClass="poster-fallback no-art"
              fallback={<span>{safeText(game.title)}</span>}
            />
          </div>
          <div className="featured-body">
            <h2>{safeText(game.title)}</h2>
            <p className="featured-summary">{safeText(game.summary)}</p>
            <div className="pill-row">
              {statusPill(game)}
              <GameTags tags={game.tags} />
            </div>
            <a
              className="primary-button"
              href={playHref ?? safeText(game.url, '#games')}
              target={target}
              rel={target ? 'noreferrer' : undefined}
              onClick={(event) => {
                if (!playHref) {
                  event.preventDefault();
                  window.dispatchEvent(new CustomEvent('agp:signin'));
                }
              }}
            >
              Play Now <span aria-hidden>→</span>
            </a>
          </div>
        </div>
        {count > 1 && (
          <div className="featured-nav">
            <button type="button" className="featured-nav-button" aria-label="Previous game" onClick={() => go(-1)}>
              <Chevron direction="left" />
            </button>
            <button type="button" className="featured-nav-button" aria-label="Next game" onClick={() => go(1)}>
              <Chevron direction="right" />
            </button>
          </div>
        )}
      </div>
      <aside className="featured-aside" aria-hidden>
        <span>Kind</span>
        <span>People</span>
        <span>Brighter</span>
        <span>Worlds</span>
      </aside>
    </article>
  );
}
