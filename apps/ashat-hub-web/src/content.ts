export type GameStatus = 'live' | 'in-development' | 'coming-soon';

export type Game = {
  slug: string;
  title: string;
  summary: string;
  status: GameStatus;
  statusLabel: string;
  tags: string[];
  url: string;
  /** Poster visual for the thumbnail frame. Games without art fall back to a branded frame. */
  poster?: string;
  posterAlt?: string;
};

export type NewsItem = {
  title: string;
  summary: string;
  url: string;
};

export const NAV_LINKS = [
  { label: 'Home', href: '#top' },
  { label: 'Support', href: '#support' },
] as const;

export const HERO = {
  eyebrow: 'AGP Studios',
  headlineTop: 'Worlds Beyond',
  headlineEmphasis: 'Imagination.',
  description:
    'Creating games, adventures, and experiences worth exploring. Paws & Parcels and Everenfell are live now — one account for every world.',
} as const;

export const FEATURED_GAME: Game = {
  slug: 'paws-and-parcels',
  title: 'Paws & Parcels',
  summary: 'Deliver small moments. Explore a growing world.',
  status: 'live',
  statusLabel: 'Online',
  tags: ['Cozy', 'Exploration', 'Social'],
  url: 'https://pawsandparcels.agpstudios.org',
  poster: '/paws-parcels-poster.svg',
  posterAlt: 'Paws & Parcels key art',
};

export const GAMES: Game[] = [
  FEATURED_GAME,
  {
    slug: 'everenfell',
    title: 'Everenfell',
    summary: 'Season 1 is live. Sign in with AGP Studios to enter.',
    status: 'live',
    statusLabel: 'Online',
    tags: ['Adventure', 'Season 1', 'Multiplayer'],
    url: 'https://everenfell.agpstudios.org',
    poster: '/everenfell-poster.svg',
    posterAlt: 'Everenfell key art',
  },
];

export const STUDIO_NEWS: NewsItem = {
  title: 'Paws & Parcels and Everenfell are live',
  summary: 'Both game servers are online. Sign in to manage your account and jump in.',
  url: '#news',
};

/* ------------------------------------------------------------------ */
/* API-backed content with static fallback                             */
/* ------------------------------------------------------------------ */

const STATUS_LABELS: Record<GameStatus, string> = {
  live: 'Online',
  'in-development': 'In Development',
  'coming-soon': 'Coming Soon',
};

type ApiGame = {
  slug: string;
  title: string;
  summary: string;
  status: GameStatus;
  tags: string[];
  url: string;
  poster: string | null;
  featured: boolean;
};

type ApiNews = {
  id: string;
  title: string;
  summary: string;
  url: string;
  published_at: number;
};

function toGame(api: ApiGame): Game {
  return {
    slug: api.slug,
    title: api.title,
    summary: api.summary,
    status: api.status,
    statusLabel: STATUS_LABELS[api.status] ?? api.status,
    tags: api.tags ?? [],
    url: api.url || '#top',
    poster: api.poster ?? undefined,
    posterAlt: api.poster ? `${api.title} poster` : undefined,
  };
}

/**
 * Fetch studio games from the Python API. Resolves to the static GAMES data
 * if the API is unavailable, so the homepage always renders.
 */
export async function fetchGames(): Promise<{ games: Game[]; fromApi: boolean }> {
  try {
    const response = await fetch('/api/games');
    if (!response.ok) throw new Error(`games ${response.status}`);
    const data = (await response.json()) as { games?: ApiGame[] };
    const games = (data.games ?? []).map(toGame);
    if (!games.length) throw new Error('games empty');
    return { games, fromApi: true };
  } catch {
    return { games: GAMES, fromApi: false };
  }
}

/**
 * Fetch studio news from the Python API. Resolves to the static STUDIO_NEWS
 * fallback (kept as a single item) if the API is unavailable or empty.
 */
export async function fetchNews(): Promise<{ news: NewsItem[]; fromApi: boolean }> {
  try {
    const response = await fetch('/api/news');
    if (!response.ok) throw new Error(`news ${response.status}`);
    const data = (await response.json()) as { news?: ApiNews[] };
    const news = (data.news ?? []).map((item) => ({
      title: item.title,
      summary: item.summary,
      url: item.url || '#news',
    }));
    if (!news.length) throw new Error('news empty');
    return { news, fromApi: true };
  } catch {
    return { news: [STUDIO_NEWS], fromApi: false };
  }
}

export const FEATURED_VIDEO = {
  title: 'Featured video',
  embedUrl: 'https://www.youtube-nocookie.com/embed/ky8rKHSVgNw?rel=0',
  watchUrl: 'https://www.youtube.com/watch?v=ky8rKHSVgNw',
} as const;

export const FOOTER = {
  tagline: 'A Home For Playable Worlds.',
  links: [] as { label: string; href: string }[],
  socials: [
    { label: 'Discord', href: 'https://discord.gg/nKPtUF4KHt', icon: 'discord' },
    { label: 'YouTube', href: 'https://www.youtube.com/@stressthismess', icon: 'youtube' },
  ],
} as const;
