-- lastfm-analytics schema. Apply once to a fresh DB: psql -d lastfm -f schema.sql

CREATE TABLE users (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lastfm_username TEXT NOT NULL UNIQUE,
    last_synced_at TIMESTAMPTZ  -- sync high-water mark; NULL = never synced
);

CREATE TABLE scrobbles (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) NOT NULL,
    artist_name TEXT NOT NULL,
    track_name TEXT NOT NULL,
    album_name TEXT,
    listened_at TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, track_name, listened_at)  -- makes re-syncs idempotent
);

-- Every analytics query filters on user_id and ranges/sorts on listened_at;
-- loyalty and discovery additionally group by artist_name.
CREATE INDEX idx_scrobbles_user_time ON scrobbles (user_id, listened_at);
CREATE INDEX idx_scrobbles_user_artist ON scrobbles (user_id, artist_name);

-- Track lengths from track.getInfo (Last.fm's scrobble feed carries none).
-- Global cache shared by all users; filled by the overnight backfill.
-- duration_ms = 0 when Last.fm has no duration (stored so we don't re-ask).
CREATE TABLE track_durations (
    artist_name TEXT NOT NULL,
    track_name  TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (artist_name, track_name)
);

-- Artist genre tags from artist.getTopTags, stored RAW (weight = Last.fm's
-- 0-100 count). Cleaning happens at read time via artist_tags_clean, so the
-- blocklist/aliases below can change without refetching. An artist Last.fm has
-- no tags for gets one sentinel row (tag = '') so the backfill skips it.
CREATE TABLE artist_tags (
    artist_name TEXT NOT NULL,
    tag         TEXT NOT NULL,
    weight      INTEGER NOT NULL,
    PRIMARY KEY (artist_name, tag)
);

-- Hand-curated deny-list of non-taste tags (rolling; add rows as junk surfaces).
-- Kept deliberately narrow: descriptive tags like "female vocalists" or
-- "japanese" are real taste signal. Blocked: generic Anglophone nationalities
-- (in an English-heavy library they mean nothing), quality judgements, and
-- personal/platform meta. All lowercase; compared against lowercased tags.
CREATE TABLE tag_blocklist (
    tag TEXT PRIMARY KEY
);
INSERT INTO tag_blocklist (tag) VALUES
    ('american'),('america'),('usa'),('us'),('united states'),
    ('british'),('uk'),('united kingdom'),('england'),('english'),('britain'),
    ('canadian'),('canada'),('australian'),('australia'),
    ('favorite'),('favourite'),('favorites'),('favourites'),('best'),('good'),
    ('great'),('awesome'),('amazing'),('beautiful'),('love'),('loved'),
    ('classic'),('masterpiece'),('legendary'),('underrated'),('overrated'),
    ('essential'),('recommended'),('must hear'),
    ('top'),('top tracks'),('top albums'),('seen live'),('live'),
    ('want to see live'),('owned'),('my music'),('collection'),('playlist'),
    ('repeat'),('repeatable'),('scrobbled'),('lastfm'),('last.fm'),
    ('my top songs'),('all'),('art'),('beats'),('x factor'),
    ('spotify'),('youtube'),('tiktok'),('tik tok'),('headphones'),('meme')
ON CONFLICT (tag) DO NOTHING;

-- Spelling variants that lowercasing can't collapse (rolling, like the
-- blocklist). Maps a lowercased raw tag to its canonical form.
CREATE TABLE tag_aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
INSERT INTO tag_aliases (alias, canonical) VALUES
    ('hip hop','hip-hop'),
    ('r&b','rnb'),
    ('female vocalist','female vocalists'),
    ('male vocalist','male vocalists'),
    ('kpop','k-pop'),
    ('k pop','k-pop'),
    ('dnb','drum and bass'),
    ('drum n bass','drum and bass'),
    ('trip hop','trip-hop'),
    ('synth pop','synthpop'),
    ('pop-punk','pop punk'),
    ('alt rock','alternative rock')
ON CONFLICT (alias) DO NOTHING;

-- Context-sensitive tag suppression: "if an artist carries `context_tag`, that
-- artist's `excluded_tag` is meaningless and gets dropped". The blocklist can't
-- express this because it is unconditional, and these tags are only wrong in
-- combination: "pop" on a Bollywood playback singer is Last.fm crowd shorthand
-- for "popular music", not the Western pop genre, and it swamps the real tag in
-- every genre chart. Same table shape and rolling-curation habit as the two
-- lists above, so a new pair is one INSERT and no refetch.
CREATE TABLE tag_exclusions (
    context_tag  TEXT NOT NULL,
    excluded_tag TEXT NOT NULL,
    PRIMARY KEY (context_tag, excluded_tag)
);
INSERT INTO tag_exclusions (context_tag, excluded_tag) VALUES
    ('bollywood','pop'),('bollywood','hip-hop'),
    ('indian','pop'),('indian','hip-hop'),
    ('india','pop'),('india','hip-hop')
ON CONFLICT DO NOTHING;

-- The one clean view of artist tags every genre query reads: lowercase/trim,
-- apply aliases, drop the '' sentinel and blocklisted tags, apply the exclusion
-- pairs above, and collapse to one row per (artist, canonical tag) keeping the
-- strongest weight so joins can't double-count a play.
CREATE VIEW artist_tags_clean AS
WITH mapped AS (
    SELECT at.artist_name,
           COALESCE(al.canonical, lower(trim(at.tag))) AS tag,
           at.weight
    FROM artist_tags at
    LEFT JOIN tag_aliases al ON al.alias = lower(trim(at.tag))
    WHERE at.tag <> ''
),
allowed AS (  -- unconditional drops first, so exclusions match canonical tags
    SELECT * FROM mapped WHERE tag NOT IN (SELECT tag FROM tag_blocklist)
),
-- Every (artist, tag) the rules kill, built ONCE. Small: only artists carrying
-- a context tag contribute. Written as a set + anti-join rather than the
-- obvious correlated NOT EXISTS, which re-scanned the `allowed` CTE per row and
-- took this view from 10ms to 1.3s on a 6k-row corpus -- and every genre query
-- reads it.
suppressed AS (
    SELECT DISTINCT a.artist_name, x.excluded_tag AS tag
    FROM allowed a
    JOIN tag_exclusions x ON x.context_tag = a.tag
)
SELECT a.artist_name, a.tag, MAX(a.weight) AS weight
FROM allowed a
LEFT JOIN suppressed s ON s.artist_name = a.artist_name AND s.tag = a.tag
WHERE s.artist_name IS NULL
GROUP BY a.artist_name, a.tag;

-- Each artist's globally most-played tracks from artist.getTopTracks, best
-- first (rank 1 = biggest). Feeds song recommendations: fetched nightly for
-- users' favorite artists and for recommended artists. An artist Last.fm
-- doesn't know gets one sentinel row (track_name = '') so it isn't re-fetched.
CREATE TABLE artist_top_tracks (
    artist_name TEXT NOT NULL,
    track_name  TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    PRIMARY KEY (artist_name, track_name)
);

-- Cached artist recommendations (precomputed output, not source data). Rebuilt
-- nightly per user by sync_service._refresh_recommendations from scrobbles +
-- artist_tags_clean (TF-IDF/cosine, app/recommender.py); the /recommendations
-- endpoint serves it as-is. Empty for a user until that pass has run once.
CREATE TABLE recommendations (
    user_id     INTEGER REFERENCES users(id) NOT NULL,
    artist_name TEXT NOT NULL,     -- an artist the user has NOT played
    score       REAL NOT NULL,     -- 0-1 cosine similarity to their taste vector
    rank        INTEGER NOT NULL,  -- 1 = best match
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, artist_name)
);
