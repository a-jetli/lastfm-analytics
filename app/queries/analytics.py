"""SQL for the insight endpoints, the read side.

One function per feature: takes a cursor (+ params), runs one query, returns
rows. No HTTP here; routers/analytics.py serves the rows as JSON. All genre
work reads the artist_tags_clean view, so blocklist/alias/case rules live in
one place (schema.sql) and never need a refetch to change.
"""

import calendar
import re
from datetime import date

from app import recommender  # reuse the cosine function for taste compatibility

# shared CTE: each artist's single strongest cleaned tag, their "primary genre".
# every tag-based query below builds on it, so the definition lives in one place.
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


def get_streaks(cur, user_id: int, tz: str = "UTC"):
    """Consecutive-day listening runs (gaps-and-islands).

    Gaps-and-islands: day minus row-number is constant while days are
    consecutive, so it groups a run. Days are the listener's, because on UTC days
    a 9pm Monday play and a 9am Wednesday play report a streak that never was.
    """
    day = _LOCAL_DATE.format(col="listened_at")
    cur.execute(
        f"""
        WITH days AS (
            SELECT DISTINCT {day} AS play_day
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
        (tz, user_id),
    )
    return cur.fetchall()


def get_discovery(cur, user_id: int, tz: str = "UTC"):
    """New artists per month: count each artist in the month you first heard it.
    Grouped case-insensitively (Last.fm scrobbles the same artist under mixed
    casing, e.g. "Twenty One Pilots" vs "twenty one pilots"), so a lowercase
    re-scrobble isn't mistaken for a brand-new artist. `tz` makes the months the
    listener's, so a late-evening first play doesn't count as next month's
    discovery."""
    month = _LOCAL_MONTH.format(col="first_play")
    cur.execute(
        f"""
        WITH firsts AS (
            SELECT lower(artist_name) AS akey, MIN(listened_at) AS first_play
            FROM scrobbles
            WHERE user_id = %s
            GROUP BY lower(artist_name)
        )
        SELECT {month} AS month,
               COUNT(*) AS new_artists
        FROM firsts
        GROUP BY month
        ORDER BY month
        """,
        (user_id, tz),
    )
    return cur.fetchall()


def get_loyalty(cur, user_id: int, tz: str = "UTC", days: int | None = None):
    """active_days / days since you first heard them. Near 1.0 = still in
    rotation; near 0 = played a lot once, then dropped. Labelled "Heavy rotation"
    in the UI.

    The denominator runs to the user's MOST RECENT play, not the artist's own
    last play: against their own span every contiguous run scored a perfect 1.0,
    so a one-afternoon binge outranked a year-long favourite. With `days` set the
    whole metric, anchor included, is measured inside that window.
    """
    day = _LOCAL_DATE.format(col="listened_at")
    recent, recent_params = _recent(days)
    # grouped case-insensitively, mode() picks the most common casing to show
    cur.execute(
        f"""
        WITH plays AS (
            SELECT artist_name, {day} AS play_day
            FROM scrobbles
            WHERE user_id = %s{recent}
        ),
        latest AS (SELECT MAX(play_day) AS day FROM plays)
        SELECT mode() WITHIN GROUP (ORDER BY artist_name) AS artist_name,
               COUNT(*)                                   AS plays,
               COUNT(DISTINCT play_day)                   AS active_days,
               ((SELECT day FROM latest) - MIN(play_day)) + 1 AS days_since_first_play,
               ROUND(
                   COUNT(DISTINCT play_day)::numeric
                   / (((SELECT day FROM latest) - MIN(play_day)) + 1),
                   2
               ) AS loyalty
        FROM plays
        GROUP BY lower(artist_name)
        HAVING COUNT(*) >= 5
        ORDER BY loyalty DESC, plays DESC
        """,
        [tz, user_id] + recent_params,
    )
    return cur.fetchall()


# defined once: the clock groups by these and the scrobble filter matches on
# them, so a cell's count always equals the rows a click returns.
# parts are 6-hour blocks: 0=night, 1=morning, 2=afternoon, 3=evening.
_PART_OF_DAY = "(EXTRACT(HOUR FROM {col} AT TIME ZONE %s)::int / 6)"
_WEEKDAY = "EXTRACT(DOW FROM {col} AT TIME ZONE %s)::int"
# bare listened_at::date and date_trunc both resolve in the session timezone, so
# a 9pm play lands on tomorrow and a 9pm play on the 31st lands next month. every
# date bucket in this file goes through one of these three.
_LOCAL_DATE = "({col} AT TIME ZONE %s)::date"  # 1 param: tz
_LOCAL_MONTH = "date_trunc('month', {col} AT TIME ZONE %s)::date"  # 1 param: tz
_LOCAL_PERIOD = "date_trunc(%s, {col} AT TIME ZONE %s)::date"  # 2 params: bucket, tz


def _recent(days: int | None, col: str = "listened_at") -> tuple[str, list]:
    """SQL fragment + params for the range picker, ("", []) when days is falsy so
    callers can splice it in unconditionally. Rolling window from now(), not
    calendar periods: "last 30 days" means 30 days, not "this month so far"."""
    if not days:
        return "", []
    return f" AND {col} >= now() - make_interval(days => %s)", [days]

PART_NAMES = ["night", "morning", "afternoon", "evening"]


def get_listening_clock(cur, user_id: int, tz: str = "UTC", days: int | None = 365):
    """Plays per (calendar day, part of day) over the last `days` days (None =
    all time): one column per real date, like a contributions graph. A cell maps
    to exactly one date and block, so it can be drilled into. Only non-empty
    pairs come back; the caller fills the gaps."""
    day = _LOCAL_DATE.format(col="listened_at")
    part = _PART_OF_DAY.format(col="listened_at")
    recent, recent_params = _recent(days)
    cur.execute(
        f"""
        SELECT {day}    AS day,
               {part}   AS part,
               COUNT(*) AS plays
        FROM scrobbles
        WHERE user_id = %s{recent}
        GROUP BY day, part
        ORDER BY day, part
        """,
        [tz, tz, user_id] + recent_params,
    )
    return cur.fetchall()


def get_genre_clock(cur, user_id: int, tz: str = "UTC", days: int | None = None):
    """Genre heatmap: plays per (weekday, part, primary tag). weekday 0=Sunday,
    part 0-3. Stays an AGGREGATE weekday grid unlike get_listening_clock, because
    per-date tag cells would be too sparse to read. `days` is its range picker
    (None = all time), which is what turns "what a typical week sounds like" into
    a question you can ask of one season."""
    weekday = _WEEKDAY.format(col="s.listened_at")
    part = _PART_OF_DAY.format(col="s.listened_at")
    recent, recent_params = _recent(days, "s.listened_at")
    cur.execute(
        _PRIMARY_TAG_CTE + f"""
        SELECT {weekday} AS weekday,
               {part}    AS part,
               p.tag,
               COUNT(*)  AS plays
        FROM scrobbles s
        JOIN primary_tag p ON p.artist_name = s.artist_name
        WHERE s.user_id = %s{recent}
        GROUP BY weekday, part, p.tag
        ORDER BY weekday, part, plays DESC
        """,
        [tz, tz, user_id] + recent_params,
    )
    return cur.fetchall()


def get_binges(cur, user_id: int, min_plays: int, days: int | None = None):
    """Albums played heavily in a short burst.

    Each play counts its album's plays in the trailing 7 days; the peak is the
    burst size. Album-less plays are excluded.

    Two spans, easy to confuse: the 7-day RANGE is what "binge" means and is
    fixed; `days` is the range picker and limits which plays are considered.
    """
    recent, recent_params = _recent(days)
    cur.execute(
        f"""
        WITH counted AS (
            SELECT artist_name, album_name,
                   COUNT(*) OVER (
                       PARTITION BY artist_name, album_name
                       ORDER BY listened_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS plays_win
            FROM scrobbles
            WHERE user_id = %s AND album_name IS NOT NULL AND album_name <> ''{recent}
        )
        SELECT artist_name, album_name,
               MAX(plays_win) AS peak_plays
        FROM counted
        GROUP BY artist_name, album_name
        HAVING MAX(plays_win) >= %s
        ORDER BY peak_plays DESC
        """,
        [user_id] + recent_params + [min_plays],
    )
    return cur.fetchall()


def get_tag_shift(cur, user_id: int, period: str = "month", tz: str = "UTC",
                  days: int | None = None):
    """Tag mix over time: one row per (period, tag) with plays and
    pct_of_period, raw and chart-ready. `period` is "month" (default) or
    "week" (ISO, Monday start), bucketed in the listener's `tz`. `days` limits
    it to a trailing window (None = all time) and is the Genres range picker,
    which sums these rows client side. Each play maps to its artist's primary tag,
    so a period's percentages sum to ~100 of the tagged plays. Untagged artists'
    plays are just absent."""
    # whitelist the bucket, then bind it as a param. never interpolate.
    bucket = "week" if period == "week" else "month"
    period_start = _LOCAL_PERIOD.format(col="s.listened_at")
    # `days` also drives the Genres panel, which sums these rows client side
    recent, recent_params = _recent(days, "s.listened_at")
    cur.execute(
        _PRIMARY_TAG_CTE + f""",
        bucketed AS (
            SELECT {period_start} AS period_start,
                   p.tag,
                   COUNT(*) AS plays
            FROM scrobbles s
            JOIN primary_tag p ON p.artist_name = s.artist_name
            WHERE s.user_id = %s{recent}
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
        [bucket, tz, user_id] + recent_params,
    )
    return cur.fetchall()


def get_listening_time(cur, user_id: int, period: str = "month", tz: str = "UTC"):
    """Listening time per period, in hours, from the stored track durations.
    `period` is "month" (default) or "week", bucketed in the listener's `tz`.
    LEFT JOIN so a play whose track
    hasn't been backfilled yet (or has no duration on Last.fm) still counts
    toward `plays` but adds 0 to `hours`. Sum is in ms; /3.6e6 to get hours.
    """
    bucket = "week" if period == "week" else "month"
    month = _LOCAL_PERIOD.format(col="s.listened_at")
    cur.execute(
        f"""
        SELECT {month} AS month,
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
        (bucket, tz, user_id),
    )
    return cur.fetchall()


def get_monthly_report(cur, user_id: int, period: str = "month", tz: str = "UTC"):
    """Per-period totals, with play count vs. the previous period (LAG).
    `period` is "month" (default) or "week", bucketed in the listener's `tz`.
    Artists counted case-insensitively.
    Output column stays named `month` (it's the period start) so callers don't
    need to branch on the bucket."""
    bucket = "week" if period == "week" else "month"
    month = _LOCAL_PERIOD.format(col="listened_at")
    cur.execute(
        f"""
        WITH monthly AS (
            SELECT {month} AS month,
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
        (bucket, tz, user_id),
    )
    return cur.fetchall()


def get_monthly_summary(cur, user_id: int, tz: str = "UTC"):
    """One row per month: plays, new_artists (artists first heard that month),
    hours (from stored durations), and top_genre (that month's most-played
    primary tag). A month-over-month digest of the numbers you can't read off the
    other charts at a glance. Months are the listener's via `tz`,
    matching the rest of the monthly analytics. hours is 0 for a month whose tracks aren't
    backfilled yet; top_genre is NULL until that month has a tagged artist."""
    month = _LOCAL_MONTH.format(col="listened_at")
    month_s = _LOCAL_MONTH.format(col="s.listened_at")
    first_ever = _LOCAL_MONTH.format(col="first_ever")
    cur.execute(
        _PRIMARY_TAG_CTE + f""",
        monthly AS (
            SELECT {month} AS month, COUNT(*) AS plays
            FROM scrobbles WHERE user_id = %s GROUP BY month
        ),
        new_per_month AS (  -- an artist counts in the month of its first-ever play
            SELECT {first_ever} AS month, COUNT(*) AS new_artists
            FROM (
                SELECT lower(artist_name) AS akey, MIN(listened_at) AS first_ever
                FROM scrobbles WHERE user_id = %s GROUP BY lower(artist_name)
            ) firsts
            GROUP BY month
        ),
        hours_per_month AS (  -- LEFT JOIN durations: untracked plays add 0 hours
            SELECT {month_s} AS month,
                   ROUND(SUM(COALESCE(d.duration_ms, 0)) / 3600000.0, 1) AS hours
            FROM scrobbles s
            LEFT JOIN track_durations d
                   ON d.artist_name = s.artist_name AND d.track_name = s.track_name
            WHERE s.user_id = %s GROUP BY month
        ),
        -- Split in two so the month expression appears ONCE: it needs a tz
        -- parameter, and repeating it in SELECT, PARTITION BY and GROUP BY meant
        -- three copies of the same bind param to keep in sync.
        genre_plays AS (
            SELECT {month_s} AS month, p.tag, COUNT(*) AS plays
            FROM scrobbles s
            JOIN primary_tag p ON p.artist_name = s.artist_name
            WHERE s.user_id = %s
            GROUP BY 1, p.tag
        ),
        genre_per_month AS (  -- the primary tag with the most plays each month
            SELECT month, tag FROM (
                SELECT month, tag,
                       ROW_NUMBER() OVER (PARTITION BY month
                                          ORDER BY plays DESC, tag) AS rn
                FROM genre_plays
            ) ranked WHERE rn = 1
        )
        SELECT m.month,
               m.plays,
               COALESCE(n.new_artists, 0) AS new_artists,
               COALESCE(h.hours, 0)       AS hours,
               g.tag                      AS top_genre
        FROM monthly m
        LEFT JOIN new_per_month  n ON n.month = m.month
        LEFT JOIN hours_per_month h ON h.month = m.month
        LEFT JOIN genre_per_month g ON g.month = m.month
        ORDER BY m.month
        """,
        # one (tz, user_id) pair per CTE, in the order they appear above
        (tz, user_id, tz, user_id, tz, user_id, tz, user_id),
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
    # GROUP BY collapses mixed-casing artist rows - both variants carry tag/track
    # rows - so tags and tracks aren't listed twice
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


# columns the history table may sort by. whitelisted so `sort` can be safely
# interpolated into ORDER BY, since a bound param can't name a column.
_SCROBBLE_SORT_COLS = {"listened_at", "artist_name", "track_name", "album_name"}

# one search term: optional `field:` prefix, then either a "quoted value"
# (artist names have spaces) or a bare run of non-space characters. not shlex on
# purpose - shlex raises on an unbalanced apostrophe, and "Guns N' Roses" is a
# real artist someone will type.
_TERM_RE = re.compile(r'(?:(\w+):)?(?:"([^"]*)"|(\S+))')

# fields that filter one text column. values are column names, only ever looked
# up here and never taken from user input, so interpolating them is safe.
_SEARCH_TEXT_FIELDS = {"artist": "artist_name", "track": "track_name",
                       "album": "album_name"}

# month:january / month:jan / month:1 all mean the same thing. built from the
# stdlib so the twelve names aren't hand-typed (index 0 is "" in both lists).
_MONTH_NUMBERS = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
_MONTH_NUMBERS.update({a.lower(): n for n, a in enumerate(calendar.month_abbr) if a})

# day:friday / day:fri -> postgres DOW. calendar counts Monday=0, postgres
# counts Sunday=0, hence the shift.
_DAY_NUMBERS = {name.lower(): (n + 1) % 7 for n, name in enumerate(calendar.day_name)}
_DAY_NUMBERS.update({a.lower(): (n + 1) % 7 for n, a in enumerate(calendar.day_abbr)})

# part:night / part:morning / ... -> the same 0-3 buckets the clock draws
_PART_NUMBERS = {name: n for n, name in enumerate(PART_NAMES)}

def _is_iso_date(value: str) -> bool:
    """date:2026-07-15, one calendar day. Fully validated here, not left to
    Postgres: an impossible date like 2026-02-31 would raise on the cast and
    turn a typo into a 500, where every other field just falls back to text."""
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def parse_search(text: str | None) -> dict:
    """Split the search box into structured filters, e.g.

        artist:Logic year:2026 month:january bohemian

    -> {"artist": ["Logic"], "years": [2026], "months": [1], "free": ["bohemian"]}

    Fields: artist:, track:, album:, year:, month:, date: (ISO), day:, part:.

    A term whose field is unknown (`foo:bar`), or whose value doesn't parse as a
    year or month, falls through to free text rather than being dropped. Typing a
    colon shouldn't silently delete part of the query. Free terms keep the
    original behavior: match artist OR track, so searching a song title works.
    """
    found: dict = {"artist": [], "track": [], "album": [], "years": [], "months": [],
                   "dates": [], "days": [], "parts": [], "free": []}
    for field, quoted, bare in _TERM_RE.findall(text or ""):
        value = quoted or bare
        if not value:
            continue
        key, low = field.lower(), value.lower()
        if key in _SEARCH_TEXT_FIELDS:
            found[key].append(value)
        elif key == "year" and value.isdigit():
            found["years"].append(int(value))
        elif key == "month" and low in _MONTH_NUMBERS:
            found["months"].append(_MONTH_NUMBERS[low])
        elif key == "month" and value.isdigit() and 1 <= int(value) <= 12:
            found["months"].append(int(value))
        elif key == "date" and _is_iso_date(value):
            found["dates"].append(value)
        elif key == "day" and low in _DAY_NUMBERS:
            found["days"].append(_DAY_NUMBERS[low])
        elif key == "part" and low in _PART_NUMBERS:
            found["parts"].append(_PART_NUMBERS[low])
        else:
            # unknown field or unparseable value -> search the raw text as typed
            found["free"].append(f"{field}:{value}" if field else value)
    return found


def _scrobble_filters(user_id: int, search: str | None, start, end,
                      tz: str = "UTC") -> tuple[list, list]:
    """Shared WHERE clause for the history table as (conds, params).

    The page query and the count both call this, so "of 1,204" can never disagree
    with the rows under it. `day:`/`part:` reuse the clock's own expressions for
    the same reason.
    """
    conds = ["user_id = %s"]
    params: list = [user_id]
    f = parse_search(search)

    for key, column in _SEARCH_TEXT_FIELDS.items():
        for value in f[key]:
            # AND'd, so `artist:radiohead artist:thom` narrows rather than widens
            conds.append(f"{column} ILIKE %s")
            params.append(f"%{value}%")

    if f["years"]:
        # one "within this year" range per requested year, OR'd together. a date
        # range rather than EXTRACT(YEAR ...) keeps the (user_id, listened_at)
        # index usable. several years widen the match.
        year_conds = []
        for year in f["years"]:
            year_conds.append("(listened_at >= %s AND listened_at < %s)")
            params += [f"{year}-01-01", f"{year + 1}-01-01"]
        conds.append("(" + " OR ".join(year_conds) + ")")

    if f["months"]:
        # no index can help "every January", it's a scan by nature
        conds.append("EXTRACT(MONTH FROM listened_at) = ANY(%s)")
        params.append(f["months"])

    if f["dates"]:
        # same local-date expression the heatmap groups by, so a clicked cell
        # gives back exactly the plays it counted
        conds.append(_LOCAL_DATE.format(col="listened_at") + " = ANY(%s::date[])")
        params += [tz, f["dates"]]

    if f["days"]:
        conds.append(_WEEKDAY.format(col="listened_at") + " = ANY(%s)")
        params += [tz, f["days"]]

    if f["parts"]:
        conds.append(_PART_OF_DAY.format(col="listened_at") + " = ANY(%s)")
        params += [tz, f["parts"]]

    for value in f["free"]:
        conds.append("(artist_name ILIKE %s OR track_name ILIKE %s)")
        params += [f"%{value}%", f"%{value}%"]

    # the week drill-down's explicit range, AND'd on top of anything above
    if start:
        conds.append("listened_at >= %s")
        params.append(start)
    if end:
        conds.append("listened_at < %s")
        params.append(end)
    return conds, params


def get_scrobbles(cur, user_id: int, search: str | None, limit: int, offset: int,
                  sort: str = "listened_at", direction: str = "desc",
                  start=None, end=None, tz: str = "UTC"):
    """A page of a user's scrobbles. `search` takes field terms plus bare text
    (see parse_search); `start`/`end` restrict to [start, end), which is how a
    clicked week drills in. `sort`/`direction` order the WHOLE filtered history
    so paging stays consistent, and both are whitelisted."""
    sort = sort if sort in _SCROBBLE_SORT_COLS else "listened_at"
    direction = "ASC" if str(direction).lower() == "asc" else "DESC"
    tiebreak = "" if sort == "listened_at" else ", listened_at DESC"
    conds, params = _scrobble_filters(user_id, search, start, end, tz)
    cur.execute(
        f"""
        SELECT artist_name, track_name, album_name, listened_at
        FROM scrobbles
        WHERE {" AND ".join(conds)}
        ORDER BY {sort} {direction}{tiebreak}
        LIMIT %s OFFSET %s
        """,
        params + [limit, offset],
    )
    return cur.fetchall()


def count_scrobbles(cur, user_id: int, search: str | None, start=None, end=None,
                    tz: str = "UTC") -> int:
    """Total matching the same filters get_scrobbles pages through, so the table
    can say "1-50 of 1,204" instead of inferring the end from a short page."""
    conds, params = _scrobble_filters(user_id, search, start, end, tz)
    cur.execute(
        f"SELECT COUNT(*) AS total FROM scrobbles WHERE {' AND '.join(conds)}", params
    )
    return cur.fetchone()["total"]


def get_song_binges(cur, user_id: int, min_plays: int, days: int | None = None):
    """Individual tracks played heavily in a short burst (the song-level twin of
    get_binges). Same rolling-7-day-window peak, same `days` range picker; keep
    tracks whose peak hits min_plays."""
    recent, recent_params = _recent(days)
    cur.execute(
        f"""
        WITH counted AS (
            SELECT artist_name, track_name,
                   COUNT(*) OVER (
                       PARTITION BY artist_name, track_name
                       ORDER BY listened_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS plays_win
            FROM scrobbles
            WHERE user_id = %s{recent}
        )
        SELECT artist_name, track_name,
               MAX(plays_win) AS peak_plays
        FROM counted
        GROUP BY artist_name, track_name
        HAVING MAX(plays_win) >= %s
        ORDER BY peak_plays DESC
        LIMIT 25
        """,
        [user_id] + recent_params + [min_plays],
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
    """Taste compatibility between two users. Pulls each user's genre play counts
    with SQL, then does the comparison math in plain Python:
      score          - 0-100 cosine similarity of their primary-tag play vectors
      shared_artists - artists both play, with each user's counts (top 25)
      shared_tags    - genres both listen to, with each user's % share
      divergent_tags - genres where their shares differ most
    """
    # 1) each user's primary-tag play counts as a vector {tag: plays}. one query
    #    covers both, split by user_id right after.
    cur.execute(
        _PRIMARY_TAG_CTE + """
        SELECT s.user_id, p.tag, COUNT(*) AS plays
        FROM scrobbles s
        JOIN primary_tag p ON p.artist_name = s.artist_name
        WHERE s.user_id IN (%s, %s)
        GROUP BY s.user_id, p.tag
        """,
        (a_id, b_id),
    )
    a_plays: dict[str, float] = {}
    b_plays: dict[str, float] = {}
    for row in cur.fetchall():
        target = a_plays if row["user_id"] == a_id else b_plays
        target[row["tag"]] = float(row["plays"])
    if a_id == b_id:
        # comparing someone with themselves. the split above puts every row in
        # a_plays and leaves b_plays empty, scoring 0% next to a full list of
        # shared artists - obvious nonsense, and typing your own handle into the
        # compare box is the first thing anyone does.
        b_plays = dict(a_plays)

    # 2) score = cosine similarity of the two vectors, reusing the recommender's
    #    function, shown as 0-100.
    score = round(100 * recommender.cosine(a_plays, b_plays), 1)

    # 3) genre shares: each tag as a percent of that user's tagged plays. "or 1"
    #    dodges divide-by-zero for someone with no tagged plays, who gets 0%.
    total_a = sum(a_plays.values()) or 1
    total_b = sum(b_plays.values()) or 1
    tag_rows = []
    for tag in set(a_plays) | set(b_plays):
        a_pct = round(100 * a_plays.get(tag, 0) / total_a, 1)
        b_pct = round(100 * b_plays.get(tag, 0) / total_b, 1)
        tag_rows.append({"tag": tag, "a_pct": a_pct, "b_pct": b_pct,
                         "gap": round(abs(a_pct - b_pct), 1)})
    shared_tags = sorted(
        (r for r in tag_rows if r["a_pct"] > 0 and r["b_pct"] > 0),
        key=lambda r: r["a_pct"] + r["b_pct"], reverse=True,
    )[:15]
    divergent_tags = sorted(tag_rows, key=lambda r: r["gap"], reverse=True)[:10]

    # 4) shared artists, with both users' counts. wrapped in a subquery so
    # a_plays/b_plays are real columns to filter and sort on - postgres won't take
    # SELECT aliases in an ORDER BY expression.
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

    return {
        "score": score,
        "shared_artist_count": len(shared_artists),
        "shared_artists": shared_artists[:25],
        "shared_tags": shared_tags,
        "divergent_tags": divergent_tags,
    }
