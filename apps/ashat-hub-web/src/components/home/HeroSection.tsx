import type { ReactNode } from 'react';
import { FeaturedGameCard } from './FeaturedGameCard';
import type { Game } from '../../content';

export function HeroSection({
  games,
  signedIn,
  account,
}: {
  games: Game[];
  signedIn: boolean;
  account?: ReactNode;
}) {
  return (
    <>
      <section className="hero" aria-label="Castle vista backdrop">
        {/* Cinematic backdrop: art-directed castle vista under a grey glass
            veil so the photo sits back behind the foreground content. */}
        <div className="hero-art" aria-hidden />
      </section>
      <section className="worlds reveal" aria-label="Featured worlds">
        <div className="worlds-inner">
          <FeaturedGameCard games={games} signedIn={signedIn} account={account} />
        </div>
      </section>
    </>
  );
}
