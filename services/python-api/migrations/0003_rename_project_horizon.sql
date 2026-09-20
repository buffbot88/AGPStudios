-- Migration 0003: rename the codename game row to its real title.
--
-- For databases that already applied migration 0002, whose seed inserted the
-- in-development title as 'project-horizon' / 'Project Horizon'. The product
-- name is "The World of Ra - Horizon - Season 1" and the slug is
-- 'world-of-ra-horizon' ("project" was codename language, not part of the name).
--
-- Fresh databases don't need this file: 0002 (current revision) already seeds
-- the correct slug and title. This migration is safe to run on those too —
-- every statement is written to converge to the same end state from any of
-- the four possible prior states:
--
--   1. old codename row only          -> renamed in place to the new slug
--   2. new row already correct        -> all statements match 0 rows
--   3. BOTH rows (happens when an     -> duplicate dropped, new row kept
--      updated 0002 is re-run on a
--      DB holding the old row)
--   4. games table empty/fresh        -> row seeded to match other environments
--
-- Statement order matters: the duplicate is removed BEFORE the rename, because
-- renaming first would collide with the existing new-slug row (slug is UNIQUE)
-- and abort the migration.
--
-- Note: slug changes are not cosmetic — anything storing the old slug
-- (bookmarks, analytics filters, external references to /#games entries)
-- should be updated to 'world-of-ra-horizon'.

-- 1) Drop the codename row when the correctly-slugged row already exists
--    (both-rows state, state 3). Keeps the new row, its id, and its data.
--    Matches 0 rows in every other state.
DELETE stale
FROM games AS stale
JOIN games AS keeper
  ON keeper.slug = 'world-of-ra-horizon'
 AND keeper.id <> stale.id
WHERE stale.slug = 'project-horizon';

-- 2) Rename the codename row in place when it is the only copy (state 1).
--    Keeping the row id preserves any external references to it. Matches 0
--    rows in every other state; cannot collide thanks to step 1.
UPDATE games
SET slug = 'world-of-ra-horizon',
    title = 'The World of Ra - Horizon - Season 1',
    updated_at = UNIX_TIMESTAMP()
WHERE slug = 'project-horizon';

-- 3) Title convergence: if a correctly-slugged row survived with a stale or
--    placeholder title, normalize it. Matches 0 rows when the title is right.
UPDATE games
SET title = 'The World of Ra - Horizon - Season 1',
    updated_at = UNIX_TIMESTAMP()
WHERE slug = 'world-of-ra-horizon'
  AND title <> 'The World of Ra - Horizon - Season 1';

-- 3b) Same convergence for the themed poster art, so databases that already
--     had a correct row still pick up the fallback poster.
UPDATE games
SET poster = '/world-of-ra-poster.svg',
    updated_at = UNIX_TIMESTAMP()
WHERE slug = 'world-of-ra-horizon'
  AND (poster IS NULL OR poster = '');

-- 4) Seed the row on fresh databases where 0002's games seed never ran
--    (state 4), so every environment lists the same games. The WHERE guard
--    makes this a no-op whenever the slug already exists (states 1-3).
INSERT INTO games (id, slug, title, summary, status, tags, url, poster, featured, sort_order, created_at, updated_at)
SELECT 'horizon01', 'world-of-ra-horizon', 'The World of Ra - Horizon - Season 1',
       'A new world is taking shape.', 'in-development', '["Exploration"]', '', '', 0, 2,
       UNIX_TIMESTAMP(), UNIX_TIMESTAMP()
FROM DUAL
WHERE NOT EXISTS (SELECT 1 FROM games WHERE slug = 'world-of-ra-horizon');
