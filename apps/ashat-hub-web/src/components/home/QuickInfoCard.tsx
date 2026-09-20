import type { ReactNode } from 'react';

/** Compact quick-info card: icon, eyebrow, title, description, link. */
export function QuickInfoCard({
  icon,
  eyebrow,
  title,
  description,
  link,
  href,
  onAction,
}: {
  icon: ReactNode;
  eyebrow: string;
  title: string;
  description: string;
  link: string;
  href: string;
  onAction?: () => void;
}) {
  return (
    <article className="quick-card">
      <span className="quick-icon" aria-hidden>
        {icon}
      </span>
      <div className="quick-body">
        <span className="eyebrow">{eyebrow}</span>
        <h3>{title}</h3>
        <p>{description}</p>
        {onAction ? (
          <button type="button" className="link-action" onClick={onAction}>
            {link} <span aria-hidden>→</span>
          </button>
        ) : (
          <a className="link-action" href={href}>
            {link} <span aria-hidden>→</span>
          </a>
        )}
      </div>
      <span className="quick-chevron" aria-hidden>
        ›
      </span>
    </article>
  );
}
