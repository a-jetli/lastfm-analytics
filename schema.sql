-- rotation schema. apply once to a fresh db: psql -d lastfm -f schema.sql

CREATE TABLE users (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lastfm_username TEXT NOT NULL UNIQUE,
    last_synced_at TIMESTAMPTZ  -- sync high-water mark. null means never synced
);

CREATE TABLE scrobbles (
    id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) NOT NULL,
    artist_name TEXT NOT NULL,
    track_name TEXT NOT NULL,
    album_name TEXT,
    listened_at TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, track_name, listened_at)  -- what makes re-syncs idempotent
);

-- every analytics query filters on user_id and ranges or sorts on listened_at.
-- loyalty and discovery also group by artist_name.
CREATE INDEX idx_scrobbles_user_time ON scrobbles (user_id, listened_at);
CREATE INDEX idx_scrobbles_user_artist ON scrobbles (user_id, artist_name);

-- track lengths from track.getInfo, since the scrobble feed carries none.
-- a global cache shared by all users, filled by the periodic backfill.
-- duration_ms is 0 when Last.fm has none, stored so we do not re-ask.
CREATE TABLE track_durations (
    artist_name TEXT NOT NULL,
    track_name  TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    PRIMARY KEY (artist_name, track_name)
);

-- artist genre tags from artist.getTopTags, stored raw, where weight is
-- Last.fm's 0-100 count. cleaning happens at read time in artist_tags_clean, so
-- the lists below can change with no refetch. an artist with no tags gets one
-- sentinel row (tag = '') so the backfill skips it.
CREATE TABLE artist_tags (
    artist_name TEXT NOT NULL,
    tag         TEXT NOT NULL,
    weight      INTEGER NOT NULL,
    PRIMARY KEY (artist_name, tag)
);

-- hand-curated deny-list of non-taste tags. rolling, so add rows as junk shows
-- up. deliberately narrow: descriptive tags like "female vocalists" or
-- "japanese" are real taste signal. blocked are generic anglophone
-- nationalities, which mean nothing in an english-heavy library, quality
-- judgements, and platform meta. all lowercase, compared against lowered tags.
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

-- spelling variants lowercasing cannot collapse, rolling like the blocklist.
-- maps a lowercased raw tag to its canonical form.
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

-- context-sensitive suppression: if an artist carries context_tag, that
-- artist's excluded_tag is meaningless and gets dropped. the blocklist cannot
-- express this because it is unconditional, and these tags are only wrong in
-- combination. "pop" on a bollywood playback singer is crowd shorthand for
-- "popular music", not the western genre, and it swamps the real tag in every
-- chart. same shape as the two lists above, so a new pair is one insert.
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

-- the one clean view of artist tags every genre query reads. lowercase and
-- trim, apply aliases, drop the sentinel and blocklisted tags, apply the
-- exclusion pairs, then collapse to one row per (artist, tag) keeping the
-- strongest weight so joins cannot double-count a play.
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
-- every (artist, tag) the rules kill, built once. small, since only artists
-- carrying a context tag contribute. written as a set plus an anti-join rather
-- than the obvious correlated NOT EXISTS, which re-scanned the `allowed` cte per
-- row and took this view from 10ms to 1.3s on a 6k-row corpus. every genre
-- query reads it.
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

-- each artist's globally most-played tracks from artist.getTopTracks, best
-- first, so rank 1 is biggest. feeds song recommendations, fetched on a schedule
-- for favourite and recommended artists. an unknown artist gets one sentinel row
-- so it is not re-fetched.
CREATE TABLE artist_top_tracks (
    artist_name TEXT NOT NULL,
    track_name  TEXT NOT NULL,
    rank        INTEGER NOT NULL,
    PRIMARY KEY (artist_name, track_name)
);

-- cached artist recommendations: precomputed output, not source data. rebuilt
-- per user by sync_service._refresh_recommendations from scrobbles and
-- artist_tags_clean, and /recommendations serves it as-is. empty for a user
-- until that pass has run once.
CREATE TABLE recommendations (
    user_id     INTEGER REFERENCES users(id) NOT NULL,
    artist_name TEXT NOT NULL,     -- an artist the user has NOT played
    score       REAL NOT NULL,     -- 0-1 cosine against their taste vector
    rank        INTEGER NOT NULL,  -- 1 is the best match
    computed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, artist_name)
);

-- explicit taste feedback, the only place a user's opinion rather than their
-- play history enters the recommender.
--   block  never recommend this artist, and never seed similars off it
--   seed   treat it like a most-played artist and go find more like it
-- read by _refresh_recommendations (taste vector and exclusion set) and
-- _widen_candidate_pool (which artists to ask about).
CREATE TABLE artist_feedback (
    user_id     INTEGER REFERENCES users(id) NOT NULL,
    artist_name TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('block', 'seed')),
    -- when the similar lookup last ran for this seed. null means pending.
    -- without it a seed would re-fetch its similars every pass forever, since
    -- the tag inserts are NOT EXISTS filtered but the lookups are not.
    expanded_at TIMESTAMPTZ
);
-- lower(), because the same artist reaches this table from two sources that
-- disagree on casing, scrobbles and the recommendations cache. that is the same
-- split that once put already-played artists back into their own list.
CREATE UNIQUE INDEX idx_artist_feedback_key
    ON artist_feedback (user_id, lower(artist_name));
