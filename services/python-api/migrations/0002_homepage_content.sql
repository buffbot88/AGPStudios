-- Homepage games and news content. Public, read-only surface data.
CREATE TABLE IF NOT EXISTS games (
  id VARCHAR(32) PRIMARY KEY,
  slug VARCHAR(80) NOT NULL UNIQUE,
  title VARCHAR(120) NOT NULL,
  summary VARCHAR(400) NOT NULL,
  status ENUM('live','in-development','coming-soon') NOT NULL DEFAULT 'in-development',
  tags TEXT NOT NULL,
  url VARCHAR(400) NOT NULL DEFAULT '',
  poster VARCHAR(400) NOT NULL DEFAULT '',
  featured TINYINT(1) NOT NULL DEFAULT 0,
  sort_order INT NOT NULL DEFAULT 0,
  created_at BIGINT UNSIGNED NOT NULL DEFAULT 0,
  updated_at BIGINT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS studio_news (
  id VARCHAR(32) PRIMARY KEY,
  title VARCHAR(200) NOT NULL,
  summary VARCHAR(600) NOT NULL,
  url VARCHAR(400) NOT NULL DEFAULT '',
  published_at BIGINT UNSIGNED NOT NULL DEFAULT 0,
  created_at BIGINT UNSIGNED NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Seed content matching the current homepage defaults.
INSERT INTO games (id, slug, title, summary, status, tags, url, poster, featured, sort_order, created_at, updated_at)
VALUES
  ('paws01','paws-and-parcels','Paws & Parcels','Deliver small moments. Explore a growing world.','live','["Cozy","Exploration","Social"]','https://pawsandparcels.agpstudios.org','/paws-parcels-poster.svg',1,1,UNIX_TIMESTAMP(),UNIX_TIMESTAMP()),
  ('horizon01','world-of-ra-horizon','The World of Ra - Horizon - Season 1','A new world is taking shape.','in-development','["Exploration"]','', '',0,2,UNIX_TIMESTAMP(),UNIX_TIMESTAMP())
ON DUPLICATE KEY UPDATE title=VALUES(title);

INSERT INTO studio_news (id, title, summary, url, published_at, created_at)
VALUES
  ('news01','A New Chapter Ahead','We''re working on new worlds, new ideas, and more ways to play together.','#news',UNIX_TIMESTAMP(),UNIX_TIMESTAMP())
ON DUPLICATE KEY UPDATE title=VALUES(title);
