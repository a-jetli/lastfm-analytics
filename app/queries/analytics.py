"""SQL for the insight endpoints -- the SQL layer (read side).

One function per feature: takes a cursor (+ params), runs one query, returns
rows. No HTTP here; routers/analytics.py serves the rows as JSON. All genre
work reads the artist_tags_clean view, so blocklist/alias/case rules live in
one place (schema.sql) and never need a refetch to change.
"""

# Shared CTE: each artist's single strongest cleaned tag ("primary genre").
# Every tag-based query below builds on it so the definition lives in one place.
_PRIMARY_TAG_CTE = (
    "WITH primary_tag AS (SELECT DISTINCT ON (artist_name) artist_name, tag "
    "FROM artist_tags_clean ORDER BY artist_name, weight DESC, tag)"
)


def get_user(cur, username: str):
    """Returns {id, last_synced_at} for the user, or None if they don't exist.
    The router needs last_synced_at to decide whether a refresh is due."""
    cur.execute(
        "SELECT id, last_synced_at FROM users WHERE lastfm_username = %s", (username,)
    )
    return cur.fetchone()


def get_streaks(cur, user_id: int):
    """Consecutive-day listening runs (gaps-and-islands).

    1. Reduce scrobbles to distinct listening days.
    2. Number the days in order; day minus row-number is constant while days
       are consecutive and jumps on a gap -- that constant is the streak id.
    3. Group by it: MIN/MAX day = start/end, COUNT = length.
    """
    cur.execute(
        """
        WITH days AS (
            SELECT DISTINCT listened_at::date AS play_day
            FROM scrobbles
            WHERE user_id = %s
        ),
        grouped AS (
            SELECT play_day,
                   play_day - (ROW_NUMBER() OVER (ORDER BY play_day))::int AS streak_id
            FROM days
        )
        SELECT MIN(play_day) AS start_day,
               MAX(play_day) AS end_day,
               COUNT(*)      AS length_days
        FROM grouped
        GROUP BY streak_id
        ORDER BY length_days DESC, start_day DESC
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_discovery(cur, user_id: int):
    """New artists per month: count each artist in the month you first heard it.
    Grouped case-insensitively (Last.fm scrobbles the same artist under mixed
    casing, e.g. "Twenty One Pilots" vs "twenty one pilots"), so a lowercase
    re-scrobble isn't mistaken for a brand-new artist."""
    cur.execute(
        """
        WITH firsts AS (
            SELECT lower(artist_name) AS akey, MIN(listened_at) AS first_play
            FROM scrobbles
            WHERE user_id = %s
            GROUP BY lower(artist_name)
        )
        SELECT date_trunc('month', first_play)::date AS month,
               COUNT(*)                              AS new_artists
        FROM firsts
        GROUP BY month
        ORDER BY month
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_loyalty(cur, user_id: int):
    """Steady favorite vs. one-week binge.

    active_days / span_days near 1.0 = you return to them over time (loyal);
    near 0 = many plays crammed into a short window (a binge, then dropped).
    """
    # Grouped case-insensitively; mode() picks the most common casing to display.
    cur.execute(
        """
        SELECT mode() WITHIN GROUP (ORDER BY artist_name)      AS artist_name,
               COUNT(*)                                        AS plays,
               COUNT(DISTINCT listened_at::date)               AS active_days,
               (MAX(listened_at)::date - MIN(listened_at)::date) + 1 AS span_days,
               ROUND(
                   COUNT(DISTINCT listened_at::date)::numeric
                   / ((MAX(listened_at)::date - MIN(listened_at)::date) + 1),
                   2
               ) AS loyalty
        FROM scrobbles
        WHERE user_id = %s
        GROUP BY lower(artist_name)
        HAVING COUNT(*) >= 5
        ORDER BY plays DESC
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_listening_clock(cur, user_id: int):
    """Plays bucketed by weekday (0=Sunday) and hour of day."""
    cur.execute(
        """
        SELECT EXTRACT(DOW  FROM listened_at)::int AS weekday,
               EXTRACT(HOUR FROM listened_at)::int AS hour,
               COUNT(*)                            AS plays
        FROM scrobbles
        WHERE user_id = %s
        GROUP BY weekday, hour
        ORDER BY weekday, hour
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_genre_clock(cur, user_id: int):
    """Genre heatmap: plays per (weekday, hour, primary tag) -- the tag-colored
    version of get_listening_clock. weekday 0=Sunday. Hours are UTC, not the
    listener's local time; fixing that needs a stored per-user timezone."""
    cur.execute(
        _PRIMARY_TAG_CTE + """
        SELECT EXTRACT(DOW  FROM s.listened_at)::int AS weekday,
               EXTRACT(HOUR FROM s.listened_at)::int AS hour,
               p.tag,
               COUNT(*) AS plays
        FROM scrobbles s
        JOIN primary_tag p ON p.artist_name = s.artist_name
        WHERE s.user_id = %s
        GROUP BY weekday, hour, p.tag
        ORDER BY weekday, hour, plays DESC
        """,
        (user_id,),
    )
    return cur.fetchall()


def get_binges(cur, user_id: int, min_plays: int):
    """Albums played heavily in a short burst.

    The window counts, for each play, how many plays of that same album fall in
    the trailing 3-day span. The album's peak of that count is its burst size;
    keep albums whose peak hits min_plays. Album-less plays are excluded (a real
    album is required to call something a binge).
    """
    cur.execute(
        """
        WITH counted AS (
            SELECT artist_name, album_name,
                   COUNT(*) OVER (
                       PARTITION BY artist_name, album_name
                       ORDER BY listened_at
                       RANGE BETWEEN INTERVAL '3 days' PRECEDING AND CURRENT ROW
                   ) AS plays_3d
            FROM scrobbles
            WHERE user_id = %s AND album_name IS NOT NULL AND album_name <> ''
        )
        SELECT artist_name, album_name,
               MAX(plays_3d) AS peak_plays_in_3_days
        FROM counted
        GROUP BY artist_name, album_name
        HAVING MAX(plays_3d) >= %s
        ORDER BY peak_plays_in_3_days DESC
        """,
        (user_id, min_plays),
    )
    return cur.fetchall()


def get_tag_shift(cur, user_id: int, period: str = "month"):
    """Tag mix over time: one row per (period, tag) with plays and
    pct_of_period, raw and chart-ready. `period` is "month" (default) or
    "week" (ISO, Monday start). Each play maps to its artist's primary tag, so
    a period's percentages sum to ~100 -- of TAGGED plays; untagged artists'
    plays are simply absent."""
    # Whitelist the bucket, then pass it as a bound param (never interpolate).
    bucket = "week" if period == "week" else "month"
    cur.execute(
        _PRIMARY_TAG_CTE + """,
        bucketed AS (
            SELECT date_trunc(%s, s.listened_at)::date AS period_start,
                   p.tag,
                   COUNT(*) AS plays
            FROM scrobbles s
            JOIN primary_tag p ON p.artist_name = s.artist_name
            WHERE s.user_id = %s
            GROUP BY period_start, p.tag
        )
        SELECT period_start,
               tag,
               plays,
               ROUND(100.0 * plays / SUM(plays) OVER (PARTITION BY period_start), 1)
                   AS pct_of_period
        FROM bucketed
        ORDER BY period_start, plays DESC
        """,
        (bucket, user_id),
    )
    return cur.fetchall()


def get_listening_time(cur, user_id: int, period: str = "month"):
    """Listening time per period, in hours, from the stored track durations.
    `period` is "month" (default) or "week". LEFT JOIN so a play whose track
    hasn't been backfilled yet (or has no duration on Last.fm) still counts
    toward `plays` but adds 0 to `hours`. Sum is in ms; /3.6e6 to get hours.
    """
    bucket = "week" if period == "week" else "month"
    cur.execute(
        """
        SELECT date_trunc(%s, s.listened_at)::date AS month,
               COUNT(*)                                  AS plays,
               ROUND(SUM(COALESCE(d.duration_ms, 0)) / 3600000.0, 1) AS hours
        FROM scrobbles s
        LEFT JOIN track_durations d
               ON d.artist_name = s.artist_name
              AND d.track_name  = s.track_name
        WHERE s.user_id = %s
        GROUP BY month
        ORDER BY month
        """,
        (bucket, user_id),
    )
    return cur.fetchall()


def get_monthly_report(cur, user_id: int, period: str = "month"):
    """Per-period totals, with play count vs. the previous period (LAG).
    `period` is "month" (default) or "week". Artists counted case-insensitively.
    Output column stays named `month` (it's the period start) so callers don't
    need to branch on the bucket."""
    bucket = "week" if period == "week" else "month"
    cur.execute(
        """
        WITH monthly AS (
            SELECT date_trunc(%s, listened_at)::date AS month,
                   COUNT(*)                          AS plays,
                   COUNT(DISTINCT track_name)        AS distinct_tracks,
                   COUNT(DISTINCT lower(artist_name)) AS distinct_artists
            FROM scrobbles
            WHERE user_id = %s
            GROUP BY month
        )
        SELECT month, plays, distinct_tracks, distinct_artists,
               plays - LAG(plays) OVER (ORDER BY month) AS plays_vs_prev_month
        FROM monthly
        ORDER BY month
        """,
        (bucket, user_id),
    )
    return cur.fetchall()


def get_monthly_highlights(cur, user_id: int):
    """Per-month standouts: most_played_artist, top_new_artist (best find
    first heard that month; NULL if none), and revisited_artists (count of
    artists discovered in an earlier month). "Discovered" = first-ever play."""
    cur.execute(
        """
        WITH first_play AS (  -- case-insensitive: a lowercase re-scrobble of an
                              -- already-known artist is not a new discovery
            SELECT lower(artist_name) AS akey, MIN(listened_at) AS first_ever
            FROM scrobbles WHERE user_id = %s GROUP BY lower(artist_name)
        ),
        ma AS (  -- one row per (month, artist): plays + was-this-their-debut-month
            SELECT date_trunc('month', s.listened_at)::date AS month,
                   mode() WITHIN GROUP (ORDER BY s.artist_name) AS artist_name,
                   COUNT(*) AS plays,
                   bool_or(date_trunc('month', fp.first_ever)
                           = date_trunc('month', s.listened_at)) AS is_new
            FROM scrobbles s
            JOIN first_play fp ON fp.akey = lower(s.artist_name)
            WHERE s.user_id = %s
            GROUP BY month, lower(s.artist_name)
        ),
        most_played AS (  -- top artist per month (ties broken alphabetically)
            SELECT DISTINCT ON (month) month, artist_name, plays
            FROM ma ORDER BY month, plays DESC, artist_name
        ),
        top_new AS (  -- top artist per month among those NEW that month
            SELECT DISTINCT ON (month) month, artist_name, plays
            FROM ma WHERE is_new ORDER BY month, plays DESC, artist_name
        ),
        revisited AS (  -- count of returning (non-new) artists per month
            SELECT month, COUNT(*) AS revisited_artists
            FROM ma WHERE NOT is_new GROUP BY month
        )
        SELECT mp.month,
               mp.artist_name AS most_played_artist,
               mp.plays       AS most_played_plays,
               tn.artist_name AS top_new_artist,
               tn.plays       AS top_new_plays,
               COALESCE(r.revisited_artists, 0) AS revisited_artists
        FROM most_played mp
        LEFT JOIN top_new  tn ON tn.month = mp.month
        LEFT JOIN revisited r ON r.month  = mp.month
        ORDER BY mp.month
        """,
        (user_id, user_id),
    )
    return cur.fetchall()


def get_artist_detail(cur, user_id: int, name: str) -> dict:
    """Everything we can show about one artist when the user clicks it: their own
    play count, the artist's cleaned genre tags, and the artist's top tracks.
    All matched case-insensitively against `name`."""
    cur.execute(
        "SELECT COUNT(*) AS plays FROM scrobbles WHERE user_id = %s AND lower(artist_name) = lower(%s)",
        (user_id, name),
    )
    plays = cur.fetchone()["plays"]
    # GROUP BY collapses mixed-casing artist rows (both variants carry tag/track
    # rows) so tags and tracks aren't listed twice.
    cur.execute(
        """
        SELECT tag, MAX(weight) AS weight FROM artist_tags_clean
        WHERE lower(artist_name) = lower(%s)
        GROUP BY tag
        ORDER BY MAX(weight) DESC, tag LIMIT 8
        """,
        (name,),
    )
    tags = cur.fetchall()
    cur.execute(
        """
        SELECT track_name FROM artist_top_tracks
        WHERE lower(artist_name) = lower(%s) AND track_name <> ''
        GROUP BY track_name
        ORDER BY MIN(rank) LIMIT 8
        """,
        (name,),
    )
    tracks = [r["track_name"] for r in cur.fetchall()]
    return {"name": name, "plays": plays, "tags": tags, "top_tracks": tracks}


# Columns the history table is allowed to sort by. Whitelisted so `sort` can be
# safely interpolated into ORDER BY (a bound param can't name a column).
_SCROBBLE_SORT_COLS = {"listened_at", "artist_name", "track_name", "album_name"}


def get_scrobbles(cur, user_id: int, search: str | None, limit: int, offset: int,
                  sort: str = "listened_at", direction: str = "desc",
                  start=None, end=None):
    """A page of a user's scrobbles for the browsable, sortable history table.
    `search` (optional) matches artist OR track, case-insensitively. `start`/`end`
    (optional dates) restrict to a range [start, end), which is how a clicked
    week's bar drills into its scrobbles. `sort` + `direction` order the WHOLE
    filtered history (not just the page) so paging stays consistent; both are
    whitelisted. limit/offset paginate; caller clamps limit.
    """
    sort = sort if sort in _SCROBBLE_SORT_COLS else "listened_at"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    tiebreak = "" if sort == "listened_at" else ", listened_at DESC"
    conds = ["user_id = %s"]
    params: list = [user_id]
    if search:
        conds.append("(artist_name ILIKE %s OR track_name ILIKE %s)")
        params += [f"%{search}%", f"%{search}%"]
    if start:
        conds.append("listened_at >= %s")
        params.append(start)
    if end:
        conds.append("listened_at < %s")
        params.append(end)
    params += [limit, offset]
    cur.execute(
        f"""
        SELECT artist_name, track_name, album_name, listened_at
        FROM scrobbles
        WHERE {" AND ".join(conds)}
        ORDER BY {sort} {direction}{tiebreak}
        LIMIT %s OFFSET %s
        """,
        params,
    )
    return cur.fetchall()


def get_song_binges(cur, user_id: int, min_plays: int):
    """Individual tracks played heavily in a short burst (the song-level twin of
    get_binges). Same rolling-3-day-window peak; keep tracks whose peak hits
    min_plays."""
    cur.execute(
        """
        WITH counted AS (
            SELECT artist_name, track_name,
                   COUNT(*) OVER (
                       PARTITION BY artist_name, track_name
                       ORDER BY listened_at
                       RANGE BETWEEN INTERVAL '3 days' PRECEDING AND CURRENT ROW
                   ) AS plays_3d
            FROM scrobbles
            WHERE user_id = %s
        )
        SELECT artist_name, track_name,
               MAX(plays_3d) AS peak_plays_in_3_days
        FROM counted
        GROUP BY artist_name, track_name
        HAVING MAX(plays_3d) >= %s
        ORDER BY peak_plays_in_3_days DESC
        LIMIT 25
        """,
        (user_id, min_plays),
    )
    return cur.fetchall()


def get_genre_tracks(cur, user_id: int, tag: str):
    """The user's tracks whose artist's primary (strongest) genre is `tag`, most
    played first. Powers the "show me this genre" drill-down. Reuses the same
    primary-tag definition as the genre clock and tag shift."""
    cur.execute(
        _PRIMARY_TAG_CTE + """
        SELECT s.artist_name, s.track_name, COUNT(*) AS plays
        FROM scrobbles s
        JOIN primary_tag p ON p.artist_name = s.artist_name
        WHERE s.user_id = %s AND p.tag = %s
        GROUP BY s.artist_name, s.track_name
        ORDER BY plays DESC, s.track_name
        LIMIT 60
        """,
        (user_id, tag),
    )
    return cur.fetchall()


def get_compatibility(cur, a_id: int, b_id: int) -> dict:
    """Taste compatibility between two users. The one function that runs
    several queries and assembles a dict:
      score          -- 0-100 cosine similarity of primary-tag play vectors
      shared_artists -- artists both play, with each user's counts (top 25)
      shared_tags    -- genres both listen to, with each user's % share
      divergent_tags -- genres where their shares differ most
    """
    # 1) Overall score: cosine similarity of the two primary-tag play vectors.
    cur.execute(
        _PRIMARY_TAG_CTE + """,
        ta AS (SELECT p.tag, COUNT(*)::numeric AS plays FROM scrobbles s
               JOIN primary_tag p ON p.artist_name = s.artist_name
               WHERE s.user_id = %s GROUP BY p.tag),
        tb AS (SELECT p.tag, COUNT(*)::numeric AS plays FROM scrobbles s
               JOIN primary_tag p ON p.artist_name = s.artist_name
               WHERE s.user_id = %s GROUP BY p.tag),
        dot AS (SELECT COALESCE(SUM(ta.plays * tb.plays), 0) AS d
                FROM ta JOIN tb USING (tag)),          -- dot product over shared tags
        mag AS (SELECT (SELECT sqrt(SUM(plays*plays)) FROM ta) AS na,
                       (SELECT sqrt(SUM(plays*plays)) FROM tb) AS nb)  -- vector lengths
        SELECT CASE WHEN mag.na IS NULL OR mag.nb IS NULL
                         OR mag.na = 0 OR mag.nb = 0 THEN 0
                    ELSE ROUND(100 * dot.d / (mag.na * mag.nb), 1) END AS score
        FROM dot, mag
        """,
        (a_id, b_id),
    )
    score = cur.fetchone()["score"]

    # 2) Shared artists: those both users have played, with each user's counts.
    # Wrapped in a subquery so a_plays/b_plays are real columns we can filter and
    # sort by an expression (Postgres won't allow SELECT aliases inside an
    # ORDER BY expression like a_plays + b_plays).
    cur.execute(
        """
        SELECT artist_name, a_plays, b_plays FROM (
            SELECT mode() WITHIN GROUP (ORDER BY s.artist_name) AS artist_name,
                   COUNT(*) FILTER (WHERE s.user_id = %s) AS a_plays,
                   COUNT(*) FILTER (WHERE s.user_id = %s) AS b_plays
            FROM scrobbles s
            WHERE s.user_id IN (%s, %s)
            GROUP BY lower(s.artist_name)
        ) t
        WHERE a_plays > 0 AND b_plays > 0
        ORDER BY a_plays + b_plays DESC
        """,
        (a_id, b_id, a_id, b_id),
    )
    shared_artists = cur.fetchall()

    # 3) Tag-share comparison -> drives both shared and divergent genres.
    cur.execute(
        _PRIMARY_TAG_CTE + """,
        ta AS (SELECT p.tag, COUNT(*)::numeric AS plays FROM scrobbles s
               JOIN primary_tag p ON p.artist_name = s.artist_name
               WHERE s.user_id = %s GROUP BY p.tag),
        tb AS (SELECT p.tag, COUNT(*)::numeric AS plays FROM scrobbles s
               JOIN primary_tag p ON p.artist_name = s.artist_name
               WHERE s.user_id = %s GROUP BY p.tag),
        sa AS (SELECT tag, ROUND(100.0*plays/SUM(plays) OVER (), 1) AS pct FROM ta),
        sb AS (SELECT tag, ROUND(100.0*plays/SUM(plays) OVER (), 1) AS pct FROM tb)
        SELECT COALESCE(sa.tag, sb.tag) AS tag,
               COALESCE(sa.pct, 0) AS a_pct,
               COALESCE(sb.pct, 0) AS b_pct,
               ROUND(ABS(COALESCE(sa.pct, 0) - COALESCE(sb.pct, 0)), 1) AS gap
        FROM sa FULL JOIN sb USING (tag)
        """,
        (a_id, b_id),
    )
    tag_rows = cur.fetchall()

    shared_tags = sorted(
        (r for r in tag_rows if r["a_pct"] > 0 and r["b_pct"] > 0),
        key=lambda r: r["a_pct"] + r["b_pct"],
        reverse=True,
    )[:15]
    divergent_tags = sorted(tag_rows, key=lambda r: r["gap"], reverse=True)[:10]

    return {
        "score": score,
        "shared_artist_count": len(shared_artists),
        "shared_artists": shared_artists[:25],
        "shared_tags": shared_tags,
        "divergent_tags": divergent_tags,
    }
