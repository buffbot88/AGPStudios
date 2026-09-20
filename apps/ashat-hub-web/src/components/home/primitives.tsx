import type { ReactNode } from 'react';
import type { Game } from '../../content';
import { safeList, safeText } from '../api';

export type User = {
  id: string;
  username: string;
  email: string;
  display_name: string;
  role: string;
};

/** Small uppercase label used above headings and inside cards. */
export function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="eyebrow">{children}</span>;
}

/** Small pill label. Variants: neutral (default), live (green), accent (gold). */
export function Pill({ variant, children }: { variant?: 'live' | 'accent' | 'coming'; children: ReactNode }) {
  return <span className={`pill ${variant ?? ''}`.trim()}>{children}</span>;
}

export function statusPill(game: Game) {
  return (
    <Pill variant={game.status === 'live' ? 'live' : game.status === 'coming-soon' ? 'coming' : undefined}>
      {safeText(game.statusLabel, safeText(game.status))}
    </Pill>
  );
}

export function GameTags({ tags }: { tags?: readonly string[] }) {
  // API-derived: a null/non-array tags field must not throw (safeList).
  return (
    <div className="pill-row">
      {safeList<string>(tags).map((tag) => (
        <Pill key={tag}>{tag}</Pill>
      ))}
    </div>
  );
}

export function SectionHeader({
  title,
  text,
  action,
  href = '#',
}: {
  title: string;
  text?: string;
  action?: string;
  href?: string;
}) {
  return (
    <div className="section-header">
      <div>
        <h2>{title}</h2>
        {text && <p>{text}</p>}
      </div>
      {action && (
        <a className="link-action" href={href}>
          {action} <span aria-hidden>→</span>
        </a>
      )}
    </div>
  );
}
