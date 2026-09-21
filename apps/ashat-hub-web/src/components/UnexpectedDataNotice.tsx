import { useState, useSyncExternalStore } from 'react';
import { clearShapeDrifts, shapeDrifts, subscribeShapeDrift } from './api';

/**
 * Inline "unexpected data" notice.
 *
 * A field the UI expects but the API sends in the wrong shape used to render as
 * a blank with no explanation. This names what actually arrived, on the surface
 * where it happened, and clears when a fresh payload loads.
 *
 * It lists every recorded drift rather than filtering by surface: a minified
 * build has no readable component names, and hiding a real problem because its
 * origin was unreadable would be the worse failure.
 */
export function UnexpectedDataNotice({ onRetry }: { onRetry?: () => void }) {
  const drifts = useSyncExternalStore(subscribeShapeDrift, shapeDrifts, shapeDrifts);
  const [open, setOpen] = useState(false);

  if (drifts.length === 0) return null;

  return (
    <section className="data-notice" role="status" aria-live="polite">
      <span className="data-notice-mark" aria-hidden>▲</span>
      <div className="data-notice-body">
        <strong>Unexpected data{drifts.length > 1 ? ` (${drifts.length})` : ''}</strong>
        <p>
          Something this page expects didn&apos;t arrive in the right shape, so the values below show
          blanks or defaults. What the API actually sent:
        </p>
        {open && (
          <ul className="data-notice-details">
            {drifts.map((drift) => (
              <li key={drift.key}>
                <code>{drift.origin || 'unknown call site'}</code>
                <span>
                  expected {drift.expected}, got {drift.got}
                  {drift.count > 1 ? ` · ×${drift.count}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="data-notice-actions">
          <button
            type="button"
            className="data-notice-toggle"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? 'Hide details' : 'Show details'}
          </button>
          {onRetry && (
            <button type="button" className="secondary-button" onClick={onRetry}>
              Try again
            </button>
          )}
          <button type="button" className="data-notice-toggle" onClick={() => clearShapeDrifts()}>
            Dismiss
          </button>
        </div>
      </div>
    </section>
  );
}

/**
 * Stand-in for a panel whose payload arrived but can't be rendered, so an unusable response never reads as "still loading".
 */
export function UnexpectedDataPanel({ what, reason, onRetry }: {
  what: string; reason?: string; onRetry?: () => void;
}) {
  return (
    <section className="data-notice" role="status">
      <span className="data-notice-mark" aria-hidden>▲</span>
      <div className="data-notice-body">
        <strong>Unexpected data</strong>
        <p>
          {what} didn&apos;t arrive in a shape this page can use, so there is nothing to show here.
          That is a data problem, not an empty result.
        </p>
        {reason && <code className="data-notice-reason">{reason}</code>}
        {onRetry && (
          <div className="data-notice-actions">
            <button type="button" className="secondary-button" onClick={onRetry}>
              Try again
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
