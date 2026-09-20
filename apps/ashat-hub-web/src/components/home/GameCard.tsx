import type { Game } from '../../content';
import { statusPill, GameTags } from './primitives';

/**
 * Game card for the Our Games grid. Data-driven; missing art falls back to a
 * branded gradient frame so new titles can be added before artwork exists.
 */
export function GameCard({ game }: { game: Game }) {
  return (
    <a
      className="game-card"
      href={game.url}
      target={game.url.startsWith('http') ? '_blank' : undefined}
      rel={game.url.startsWith('http') ? 'noreferrer' : undefined}
    >
      <div className={`game-thumb${game.poster ? '' : ` fallback-${game.status}`}`}>
        {game.poster ? (
          <img src={game.poster} alt={game.posterAlt ?? `${game.title} thumbnail`} />
        ) : (
          <span className="thumb-fallback" aria-hidden>
            {game.status === 'coming-soon' ? '✦' : '◎'}
          </span>
        )}
      </div>
      <div className="game-body">
        <h3>{game.title}</h3>
        <p>{game.summary}</p>
        <div className="pill-row">
          {statusPill(game)}
          <GameTags tags={game.tags} />
        </div>
      </div>
      <span className="game-chevron" aria-hidden>
        ›
      </span>
    </a>
  );
}
