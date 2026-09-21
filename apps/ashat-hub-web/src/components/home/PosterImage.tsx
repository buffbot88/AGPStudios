import { useEffect, useState } from 'react';
import { safeText } from '../api';

/**
 * Poster image with a branded onerror fallback.
 *
 * A broken/missing poster URL must never show the browser's broken-image
 * icon: the first load error swaps the image for the same gradient frame the
 * "no art yet" state uses. Fallback content and class come from the caller so
 * the carousel and the grid thumbnails keep their distinct frames.
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
