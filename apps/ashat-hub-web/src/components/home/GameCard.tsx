import type { Game } from '../../content';
import { statusPill, GameTags } from './primitives';
import { safeText } from '../api';

/**
 * Game card for the Our Games grid. Data-driven; missing art falls back to a
 * branded gradient frame so new titles can be added before artwork exists.
 */
export function GameCard({ game }: { game: Game }) {
  return (
    <a
      className="game-card"
      href={safeText(game.url, '#games')}
      target={safeText(game.url).startsWith('http') ? '_blank' : undefined}
      rel={safeText(game.url).startsWith('http') ? 'noreferrer' : undefined}
    >
      <div className={`game-thumb${game.poster ? '' : ` fallback-${safeText(game.status)}`}`}>
        {game.poster ? (
          <img src={game.poster} alt={game.posterAlt ?? `${safeText(game.title)} thumbnail`} />
        ) : (
          <span className="thumb-fallback" aria-hidden>
            {game.status === 'coming-soon' ? '✦' : '◎'}
          </span>
        )}
      </div>
      <div className="game-body">
        <h3>{safeText(game.title)}</h3>
        <p>{safeText(game.summary)}</p>
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
