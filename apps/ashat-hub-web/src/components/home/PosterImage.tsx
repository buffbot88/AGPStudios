import { useEffect, useState } from 'react';
import { safeText } from '../api';

/**
 * Poster image that swaps to the caller's branded frame if the URL fails to load.
 */
export function PosterImage({
  src,
  alt,
  fallback,
  fallbackClass,
}: {
  src: string;
  alt: string;
  fallback: React.ReactNode;
  /** Extra class applied to the wrapper while the fallback is showing. */
  fallbackClass: string;
}) {
  const [failed, setFailed] = useState(false);

  // A new URL resets the failure state so a fixed poster recovers on next load.
  useEffect(() => setFailed(false), [src]);

  if (failed || !safeText(src)) {
    return (
      <div className={fallbackClass} aria-hidden>
        {fallback}
      </div>
    );
  }
  return <img src={src} alt={alt} onError={() => setFailed(true)} />;
}
