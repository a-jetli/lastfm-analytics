"""SQL for the recommender: load the raw material (tag corpus + a user's plays)
and cache the computed results. The vector math itself is in app/recommender.py
-- this file only moves rows in and out of Postgres, mirroring queries/sync.py.
"""


def get_tag_corpus(cur):
    """Every (artist, tag, weight) from the cleaned tag view -- the raw material
    for artist vectors. Returned flat; the caller folds it into {artist: {tag:
    weight}}. Reads artist_tags_clean, so blocklist/alias/case rules apply."""
    cur.execute("SELECT artist_name, tag, weight FROM artist_tags_clean")
    return cur.fetchall()


# Recency half-life for the taste vector, in days. A play this old counts half
# as much toward "what you're into now" as a play today; twice this old, a
# quarter; and so on (exponential decay). 90 days keeps a season of listening
# dominant without erasing older favorites. Tuning knob, not a hard rule.
TASTE_HALF_LIFE_DAYS = 90


def get_user_plays(cur, user_id: int):
    """(artist_name, recency_weight) for one user: every artist they've played,
    each with a recency-weighted play score instead of a raw count. A play
    contributes 0.5 ** (age_days / TASTE_HALF_LIFE_DAYS), so recent listening
    dominates the taste vector while old plays fade but never vanish.

    Drives two things, which is why EVERY played artist must stay in the result:
    the taste vector (this weight, then log-compressed in build_user_vector) and
    the exclusion set (we never recommend an artist the user already plays -- an
    artist they loved a year ago still counts as 'played' and stays excluded).
    Decay never reaches zero, so the row is always present.
    """
    cur.execute(
        """
        SELECT artist_name,
               SUM(power(0.5, EXTRACT(EPOCH FROM (now() - listened_at))
                              / (86400.0 * %s))) AS recency_weight
        FROM scrobbles WHERE user_id = %s GROUP BY artist_name
        """,
        (TASTE_HALF_LIFE_DAYS, user_id),
    )
    return cur.fetchall()


def get_all_user_ids(cur):
    """Every user id -- the overnight pass recomputes recommendations for all."""
    cur.execute("SELECT id FROM users")
    return [row[0] for row in cur.fetchall()]


def replace_recommendations(cur, user_id: int, ranked: list[tuple[str, float]]) -> None:
    """Swap a user's cached recommendations for a freshly computed list. Delete +
    insert together so a reader never sees a half-written set (the caller commits
    once after this, making the swap atomic)."""
    cur.execute("DELETE FROM recommendations WHERE user_id = %s", (user_id,))
    cur.executemany(
        """
        INSERT INTO recommendations (user_id, artist_name, score, rank)
        VALUES (%s, %s, %s, %s)
        """,
        [
            (user_id, artist_name, score, rank)
            for rank, (artist_name, score) in enumerate(ranked, start=1)
        ],
    )


def get_recommendations(cur, user_id: int):
    """Cached recommendations for a user, best first. Empty until the overnight
    pass has computed them at least once (same "fills in later" story as
    durations and tags)."""
    cur.execute(
        """
        SELECT artist_name, score, rank
        FROM recommendations WHERE user_id = %s ORDER BY rank
        """,
        (user_id,),
    )
    return cur.fetchall()


# --- Song recommendations (built on artist_top_tracks, no vector math) --------

# An artist qualifies as a user "favorite" if it's in their top N by plays.
FAVORITE_ARTISTS = 15


def get_artists_missing_top_tracks(cur):
    """Work list for the top-tracks backfill: artists we want songs for (every
    user's favorites + every recommended artist) that aren't cached yet. Same
    incremental NOT EXISTS shape as the duration/tag work lists."""
    cur.execute(
        """
        WITH wanted AS (
            SELECT artist_name FROM recommendations
            UNION
            SELECT artist_name FROM (
                SELECT artist_name,
                       ROW_NUMBER() OVER (PARTITION BY user_id
                                          ORDER BY COUNT(*) DESC) AS rn
                FROM scrobbles GROUP BY user_id, artist_name
            ) ranked WHERE rn <= %s
        )
        SELECT artist_name FROM wanted
        WHERE NOT EXISTS (
            SELECT 1 FROM artist_top_tracks t
            WHERE t.artist_name = wanted.artist_name
        )
        """,
        (FAVORITE_ARTISTS,),
    )
    return cur.fetchall()


def insert_top_track(cur, artist_name: str, track_name: str, rank: int) -> None:
    # ON CONFLICT DO NOTHING: idempotent across overlapping/re-run passes.
    cur.execute(
        """
        INSERT INTO artist_top_tracks (artist_name, track_name, rank)
        VALUES (%s, %s, %s)
        ON CONFLICT (artist_name, track_name) DO NOTHING
        """,
        (artist_name, track_name, rank),
    )


def get_song_recs_favorites(cur, user_id: int):
    """Gap mining: popular tracks by the user's most-played artists that they
    have never played. No taste-guessing involved -- their own plays pick the
    artists, Last.fm's global ranks pick the tracks. NOTE: the anti-join matches
    exact track names, so a song scrobbled under a variant title ("... (feat X)")
    can slip through as a rec; acceptable noise."""
    cur.execute(
        """
        WITH favorites AS (
            SELECT artist_name, COUNT(*) AS plays
            FROM scrobbles WHERE user_id = %s
            GROUP BY artist_name ORDER BY plays DESC LIMIT %s
        )
        SELECT t.artist_name, t.track_name, f.plays AS your_artist_plays
        FROM artist_top_tracks t
        JOIN favorites f USING (artist_name)
        WHERE t.track_name <> ''
          AND NOT EXISTS (
              SELECT 1 FROM scrobbles s
              WHERE s.user_id = %s
                AND s.artist_name = t.artist_name
                AND s.track_name  = t.track_name
          )
        ORDER BY f.plays DESC, t.rank
        LIMIT 25
        """,
        (user_id, FAVORITE_ARTISTS, user_id),
    )
    return cur.fetchall()


def get_song_recs_discovery(cur, user_id: int):
    """Entry points into recommended artists: the top few tracks of each artist
    the recommender picked, ordered by how well the artist matched. The user
    hasn't played these artists at all, so no anti-join is needed."""
    cur.execute(
        """
        SELECT r.artist_name, t.track_name, r.score AS artist_score
        FROM recommendations r
        JOIN artist_top_tracks t USING (artist_name)
        WHERE r.user_id = %s AND t.track_name <> '' AND t.rank <= 3
        ORDER BY r.rank, t.rank
        LIMIT 25
        """,
        (user_id,),
    )
    return cur.fetchall()
