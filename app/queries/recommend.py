"""sql for the recommender: load the raw material (tag corpus, a user's plays)
and cache the results. the vector math is in app/recommender.py. this file only
moves rows in and out of postgres, mirroring queries/sync.py.
"""


def get_tag_corpus(cur):
    """flat (artist, tag, weight) rows for the artist vectors. the caller folds
    them into {artist: {tag: weight}}.

    folded on lower(artist_name): split by casing, an artist gets two vectors
    and can win two slots, which once put "Tyler, the Creator" and "Tyler, The
    Creator" in one list.
    """
    cur.execute(
        """
        WITH canon AS (
            SELECT lower(artist_name) AS akey,
                   mode() WITHIN GROUP (ORDER BY artist_name) AS display
            FROM artist_tags_clean
            GROUP BY lower(artist_name)
        )
        SELECT c.display AS artist_name, t.tag, MAX(t.weight) AS weight
        FROM artist_tags_clean t
        JOIN canon c ON c.akey = lower(t.artist_name)
        GROUP BY c.display, t.tag
        """
    )
    return cur.fetchall()


# taste-vector half-life in days. a play this old counts half as much as one
# today, which keeps a season dominant without wiping out older favourites.
TASTE_HALF_LIFE_DAYS = 90


def get_user_plays(cur, user_id: int):
    """(artist_name, recency_weight) per artist: 0.5 ** (age_days / half-life)
    summed over their plays.

    every played artist must appear, because this doubles as the exclusion set,
    so the decay can never reach zero. grouped on lower(artist_name) like the
    corpus: 22 plays of "Charli xcx" once failed to exclude "Charli XCX".
    """
    cur.execute(
        """
        SELECT mode() WITHIN GROUP (ORDER BY artist_name) AS artist_name,
               SUM(power(0.5, EXTRACT(EPOCH FROM (now() - listened_at))
                              / (86400.0 * %s))) AS recency_weight
        FROM scrobbles WHERE user_id = %s GROUP BY lower(artist_name)
        """,
        (TASTE_HALF_LIFE_DAYS, user_id),
    )
    return cur.fetchall()


def get_all_user_ids(cur):
    """every user id. the maintenance pass recomputes all of them."""
    cur.execute("SELECT id FROM users")
    return [row[0] for row in cur.fetchall()]


def replace_recommendations(cur, user_id: int, ranked: list[tuple[str, float]]) -> None:
    """swap a user's cached recommendations for a fresh list. delete and insert
    together, and the caller commits once after, so a reader never sees a
    half-written set."""
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
    """cached recommendations, best first. empty until the maintenance pass has
    run once, same as durations and tags."""
    cur.execute(
        """
        SELECT artist_name, score, rank
        FROM recommendations WHERE user_id = %s ORDER BY rank
        """,
        (user_id,),
    )
    return cur.fetchall()


# song recommendations, built on artist_top_tracks. no vector math.

# an artist counts as a favorite if it is in the user's top N by plays
FAVORITE_ARTISTS = 15
# per-artist caps on the two song lists, both there to stop one artist filling
# the panel. the favourites list round-robins on the first, the gateway list
# shows at most the second under each recommended artist.
TRACKS_PER_FAVORITE = 2
GATEWAY_TRACKS = 3


# how many top artists to ask for similars, and how many each. 10 x 10 is at
# most 100 names per user per pass, and after the first run nearly all are known.
SEED_ARTISTS = 10
SIMILAR_PER_ARTIST = 10


def get_top_artists(cur, user_id: int, limit: int = SEED_ARTISTS):
    """a user's most-played artists, best first. seeds the similar lookup.
    grouped case-insensitively with a mode() display name, matching
    get_user_plays, so a mixed-casing artist is one seed and not two.

    blocked artists are skipped, because "not interested" has to stop the pool
    growing in that direction, not just hide one name."""
    cur.execute(
        """
        SELECT mode() WITHIN GROUP (ORDER BY artist_name) AS artist_name
        FROM scrobbles s
        WHERE s.user_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM artist_feedback f
              WHERE f.user_id = s.user_id AND f.verdict = 'block'
                AND lower(f.artist_name) = lower(s.artist_name)
          )
        GROUP BY lower(s.artist_name)
        ORDER BY COUNT(*) DESC
        LIMIT %s
        """,
        (user_id, limit),
    )
    return [row[0] for row in cur.fetchall()]


# explicit feedback: the one place an opinion, rather than a play, steers this.


def set_feedback(cur, user_id: int, artist_name: str, verdict: str) -> None:
    """record "more like this" (seed) or "not interested" (block).

    upsert on the case-insensitive key, so flipping a verdict replaces it rather
    than leaving both. a re-seed clears expanded_at so the similar lookup runs
    again next pass."""
    cur.execute(
        """
        INSERT INTO artist_feedback (user_id, artist_name, verdict)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id, lower(artist_name)) DO UPDATE
            SET artist_name = EXCLUDED.artist_name,
                verdict     = EXCLUDED.verdict,
                expanded_at = NULL
        """,
        (user_id, artist_name, verdict),
    )


def clear_feedback(cur, user_id: int, artist_name: str) -> None:
    """undo a seed or block. the artist goes back to being judged on plays."""
    cur.execute(
        """
        DELETE FROM artist_feedback
        WHERE user_id = %s AND lower(artist_name) = lower(%s)
        """,
        (user_id, artist_name),
    )


def get_feedback(cur, user_id: int):
    """every artist this user has an opinion on, for the tuning list."""
    cur.execute(
        """
        SELECT artist_name, verdict FROM artist_feedback
        WHERE user_id = %s ORDER BY verdict, lower(artist_name)
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_feedback_names(cur, user_id: int, verdict: str) -> list[str]:
    """just the names for one verdict, which is what the pass needs."""
    cur.execute(
        "SELECT artist_name FROM artist_feedback WHERE user_id = %s AND verdict = %s",
        (user_id, verdict),
    )
    return [row[0] for row in cur.fetchall()]


def get_pending_seeds(cur, user_id: int) -> list[str]:
    """seeds whose similars have not been fetched. bounds the cost: one call
    per seed, once."""
    cur.execute(
        """
        SELECT artist_name FROM artist_feedback
        WHERE user_id = %s AND verdict = 'seed' AND expanded_at IS NULL
        """,
        (user_id,),
    )
    return [row[0] for row in cur.fetchall()]


def mark_seeds_expanded(cur, user_id: int) -> None:
    """stamp this user's seeds as looked up so the next pass skips them."""
    cur.execute(
        """
        UPDATE artist_feedback SET expanded_at = now()
        WHERE user_id = %s AND verdict = 'seed' AND expanded_at IS NULL
        """,
        (user_id,),
    )


def drop_recommendation(cur, user_id: int, artist_name: str) -> None:
    """evict one artist from the cached list. blocking already keeps it out of
    the next rebuild; this is what makes the click take effect now."""
    cur.execute(
        """
        DELETE FROM recommendations
        WHERE user_id = %s AND lower(artist_name) = lower(%s)
        """,
        (user_id, artist_name),
    )


def filter_unknown_artists(cur, names: list[str]) -> list[str]:
    """of `names`, the ones with no tags yet.

    two case-insensitive filters in one pass: drop anything already in
    artist_tags (we have it, or we asked and got nothing), and drop anything
    anyone has scrobbled (already in the corpus the normal way). what is left is
    genuinely new.

    this is what bounds the api cost. a user's first pass fetches up to ~100
    artists and later passes fetch almost none, as the corpus converges.
    """
    if not names:
        return []
    cur.execute(
        """
        SELECT n FROM unnest(%s::text[]) AS n
        WHERE NOT EXISTS (
            SELECT 1 FROM artist_tags a WHERE lower(a.artist_name) = lower(n)
        )
        AND NOT EXISTS (
            SELECT 1 FROM scrobbles s WHERE lower(s.artist_name) = lower(n)
        )
        """,
        (names,),
    )
    return [row[0] for row in cur.fetchall()]


def get_artists_missing_top_tracks(cur):
    """work list for the top-tracks backfill: artists we want songs for (every
    user's favorites plus every recommended artist) that are not cached yet.
    same incremental NOT EXISTS shape as the other work lists."""
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
    # overlapping or re-run passes stay idempotent
    cur.execute(
        """
        INSERT INTO artist_top_tracks (artist_name, track_name, rank)
        VALUES (%s, %s, %s)
        ON CONFLICT (artist_name, track_name) DO NOTHING
        """,
        (artist_name, track_name, rank),
    )


def get_song_recs_favorites(cur, user_id: int):
    """gap mining: popular tracks by the user's most-played artists that they
    have never played. no taste-guessing, since their own plays pick the artists
    and Last.fm's global ranks pick the tracks.

    capped at TRACKS_PER_FAVORITE per artist and ordered round-robin, so 25
    slots cover ~13 artists. ordered by plays DESC it read as "here are two
    bands", which is not a discovery list.

    note: the anti-join matches exact track names, so a song scrobbled under a
    variant title ("... (feat X)") can slip through. acceptable noise.
    """
    cur.execute(
        """
        WITH favorites AS (
            SELECT artist_name, COUNT(*) AS plays
            FROM scrobbles WHERE user_id = %s
            GROUP BY artist_name ORDER BY plays DESC LIMIT %s
        ),
        candidates AS (
            SELECT t.artist_name, t.track_name, f.plays,
                   ROW_NUMBER() OVER (PARTITION BY t.artist_name
                                      ORDER BY t.rank) AS per_artist
            FROM artist_top_tracks t
            JOIN favorites f USING (artist_name)
            WHERE t.track_name <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM scrobbles s
                  WHERE s.user_id = %s
                    AND s.artist_name = t.artist_name
                    AND s.track_name  = t.track_name
              )
        )
        SELECT artist_name, track_name, plays AS your_artist_plays
        FROM candidates
        WHERE per_artist <= %s
        -- per_artist first: every artist's best track, then every artist's
        -- second, rather than exhausting one artist before the next appears.
        ORDER BY per_artist, plays DESC
        LIMIT 25
        """,
        (user_id, FAVORITE_ARTISTS, user_id, TRACKS_PER_FAVORITE),
    )
    return cur.fetchall()


def get_song_recs_discovery(cur, user_id: int):
    """entry points into recommended artists: the top few tracks of each pick,
    ordered by how well the artist matched. the user has played none of these,
    so no anti-join is needed.

    the frontend groups these under their artist, so every recommended artist
    needs rows or it renders with no "start with" line. the limit is sized to
    the whole cached set rather than a flat 25, which ran out at artist nine.
    """
    cur.execute(
        """
        SELECT r.artist_name, t.track_name, r.score AS artist_score
        FROM recommendations r
        JOIN artist_top_tracks t USING (artist_name)
        WHERE r.user_id = %s AND t.track_name <> '' AND t.rank <= %s
        ORDER BY r.rank, t.rank
        """,
        (user_id, GATEWAY_TRACKS),
    )
    return cur.fetchall()
