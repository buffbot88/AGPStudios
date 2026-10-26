-- 0003: the only live games are Paws & Parcels and Everenfell. Replace the
-- World of Ra / placeholder seeds with the two-game lineup. Safe to re-run:
-- upserts by slug, then removes legacy slugs.
INSERT INTO games (id, slug, title, summary, status, tags, url, poster, featured, sort_order, created_at, updated_at)
VALUES
  ('paws01', 'paws-and-parcels', 'Paws & Parcels', 'Deliver small moments. Explore a growing world.', 'live', '["Cozy","Exploration","Social"]', 'https://pawsandparcels.agpstudios.org', '/paws-parcels-poster.svg', 1, 1, CAST(strftime('%s','now') AS INTEGER), CAST(strftime('%s','now') AS INTEGER)),
  ('everenfell01', 'everenfell', 'Everenfell', 'Season 1 is live. Sign in with AGP Studios to enter.', 'live', '["Adventure","Season 1","Multiplayer"]', 'https://everenfell.agpstudios.org', '/everenfell-poster.svg', 1, 2, CAST(strftime('%s','now') AS INTEGER), CAST(strftime('%s','now') AS INTEGER))
ON CONFLICT(slug) DO UPDATE SET
  title = excluded.title,
  summary = excluded.summary,
  status = excluded.status,
  tags = excluded.tags,
  url = excluded.url,
  poster = excluded.poster,
  featured = excluded.featured,
  sort_order = excluded.sort_order,
  updated_at = excluded.updated_at;

DELETE FROM games WHERE slug IN ('world-of-ra-horizon', 'more-titles');
