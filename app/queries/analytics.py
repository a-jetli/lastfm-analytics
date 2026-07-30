"""SQL for the insight endpoints -- the SQL layer (read side).

One function per feature: takes a cursor (+ params), runs one query, returns
rows. No HTTP here; routers/analytics.py serves the rows as JSON. All genre
work reads the artist_tags_clean view, so blocklist/alias/case rules live in
one place (schema.sql) and never need a refetch to change.
"""

import calendar
import re
from datetime import date

from app import recommender  # reuse the cosine function for taste compatibility

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


def get_streaks(cur, user_id: int, tz: str = "UTC"):
    """Consecutive-day listening runs (gaps-and-islands).

    1. Reduce scrobbles to distinct listening days, in the LISTENER'S zone.
    2. Number the days in order; day minus row-number is constant while days
       are consecutive and jumps on a gap -- that constant is the streak id.
    3. Group by it: MIN/MAX day = start/end, COUNT = length.

    `tz` matters more here than anywhere else: on UTC days a 9pm Monday play and
    a 9am Wednesday play both fall on Tue/Wed and report a 2-day streak that
    never happened.
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
    re-scrobble isn't mistaken for a brand-new artist. Months are the listener's,
    so a late-evening first play doesn't count as next month's discovery."""
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


def get_loyalty(cur, user_id: int, tz: str = "UTC"):
    """Steady favorite vs. binge-then-dropped, as a "do you still come back"
    rate: active_days / days since you first heard them.

    The denominator runs from the artist's first play to the user's MOST RECENT
    play overall, not to that artist's own last play. Measuring against their own
    span made every contiguous run score a perfect 1.0 -- five plays inside one
    afternoon came out "more loyal" than a favourite spread over a year, which is
    backwards. Anchoring to the user's latest activity means abandoning an artist
    keeps pushing their score down, which is what the number is supposed to say.

    So: near 1.0 = in rotation ever since you found them; near 0 = you played
    them a lot once and moved on.
    """
    day = _LOCAL_DATE.format(col="listened_at")
    # Grouped case-insensitively; mode() picks the most common casing to display.
    cur.execute(
        f"""
        WITH plays AS (
            SELECT artist_name, {day} AS play_day
            FROM scrobbles
            WHERE user_id = %s
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
        (tz, user_id),
    )
    return cur.fetchall()


# Part of day, defined ONCE. The clock groups by it and the scrobble filter
# matches on it, so a cell's count always equals the rows clicking it returns --
# same reason _PRIMARY_TAG_CTE exists. 6-hour blocks: 0=night (00-06),
# 1=morning (06-12), 2=afternoon (12-18), 3=evening (18-24). The %s is an IANA
# timezone name; `AT TIME ZONE` converts each timestamptz to local wall-clock
# time, so Postgres resolves DST per row rather than applying one fixed offset.
_PART_OF_DAY = "(EXTRACT(HOUR FROM {col} AT TIME ZONE %s)::int / 6)"
_WEEKDAY = "EXTRACT(DOW FROM {col} AT TIME ZONE %s)::int"
# The calendar day a play belongs to IN THE LISTENER'S ZONE. Not the same as
# listened_at::date, which would cut the day at UTC midnight and push a late
# evening play onto tomorrow for anyone west of Greenwich.
_LOCAL_DATE = "({col} AT TIME ZONE %s)::date"  # 1 param: tz
# Same problem one level up: date_trunc on a timestamptz ALSO resolves in the
# session timezone, so month and week buckets need converting too or a 9pm
# July 31st play lands in August. Every date bucket in this file goes through
# one of these three, so there is one day-boundary rule, not two.
_LOCAL_MONTH = "date_trunc('month', {col} AT TIME ZONE %s)::date"  # 1 param: tz
_LOCAL_PERIOD = "date_trunc(%s, {col} AT TIME ZONE %s)::date"  # 2 params: bucket, tz

PART_NAMES = ["night", "morning", "afternoon", "evening"]


def get_listening_clock(cur, user_id: int, tz: str = "UTC", days: int = 365):
    """Plays per (calendar day, part of day) for the last `days` days -- one
    column per real date, like a contributions graph, NOT a 7-day average.

    An aggregate weekday grid answers "when do I usually listen"; this answers
    "what did I do on the 14th", which is the one that can be drilled into: a
    cell maps to exactly one date and one 6-hour block. Rows come back only for
    (day, part) pairs that have plays; the caller fills the gaps so empty days
    still occupy a column.
    """
    day = _LOCAL_DATE.format(col="listened_at")
    part = _PART_OF_DAY.format(col="listened_at")
    cur.execute(
        f"""
        SELECT {day}    AS day,
               {part}   AS part,
               COUNT(*) AS plays
        FROM scrobbles
        WHERE user_id = %s
          AND listened_at >= now() - make_interval(days => %s)
        GROUP BY day, part
        ORDER BY day, part
        """,
        (tz, tz, user_id, days),
    )
    return cur.fetchall()


def get_genre_clock(cur, user_id: int, tz: str = "UTC"):
    """Genre heatmap: plays per (weekday, part, primary tag) in the caller's
    timezone. weekday 0=Sunday, part 0-3 (see PART_NAMES). `tz` is an IANA name
    from the browser; no stored per-user timezone needed. Unlike
    get_listening_clock (now one column per real date), this stays an AGGREGATE
    weekday grid -- "which genres fill a typical Friday night" is the question a
    tag breakdown answers, and per-date tag cells would be too sparse to read.
    Nothing renders it yet."""
    weekday = _WEEKDAY.format(col="s.listened_at")
    part = _PART_OF_DAY.format(col="s.listened_at")
    cur.execute(
        _PRIMARY_TAG_CTE + f"""
        SELECT {weekday} AS weekday,
               {part}    AS part,
               p.tag,
               COUNT(*)  AS plays
        FROM scrobbles s
        JOIN primary_tag p ON p.artist_name = s.artist_name
        WHERE s.user_id = %s
        GROUP BY weekday, part, p.tag
        ORDER BY weekday, part, plays DESC
        """,
        (tz, tz, user_id),
    )
    return cur.fetchall()


def get_binges(cur, user_id: int, min_plays: int):
    """Albums played heavily in a short burst.

    The window counts, for each play, how many plays of that same album fall in
    the trailing 7-day span. The album's peak of that count is its burst size;
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
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS plays_win
            FROM scrobbles
            WHERE user_id = %s AND album_name IS NOT NULL AND album_name <> ''
        )
        SELECT artist_name, album_name,
               MAX(plays_win) AS peak_plays
        FROM counted
        GROUP BY artist_name, album_name
        HAVING MAX(plays_win) >= %s
        ORDER BY peak_plays DESC
        """,
        (user_id, min_plays),
    )
    return cur.fetchall()


def get_tag_shift(cur, user_id: int, period: str = "month", tz: str = "UTC"):
    """Tag mix over time: one row per (period, tag) with plays and
    pct_of_period, raw and chart-ready. `period` is "month" (default) or
    "week" (ISO, Monday start). Each play maps to its artist's primary tag, so
    a period's percentages sum to ~100 -- of TAGGED plays; untagged artists'
    plays are simply absent."""
    # Whitelist the bucket, then pass it as a bound param (never interpolate).
    bucket = "week" if period == "week" else "month"
    period_start = _LOCAL_PERIOD.format(col="s.listened_at")
    cur.execute(
        _PRIMARY_TAG_CTE + f""",
        bucketed AS (
            SELECT {period_start} AS period_start,
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
        (bucket, tz, user_id),
    )
    return cur.fetchall()


def get_listening_time(cur, user_id: int, period: str = "month", tz: str = "UTC"):
    """Listening time per period, in hours, from the stored track durations.
    `period` is "month" (default) or "week". LEFT JOIN so a play whose track
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
    `period` is "month" (default) or "week". Artists counted case-insensitively.
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
    primary tag). A real month-over-month digest -- the numbers you can't read
    off the other charts at a glance. Months are the LISTENER'S, matching the
    rest of the monthly analytics. hours is 0 for a month whose tracks aren't
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
        # One (tz, user_id) pair per CTE, in the order the CTEs appear above.
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

# One search term: an optional `field:` prefix, then either a "quoted value"
# (artist names have spaces) or a bare run of non-space characters. Deliberately
# not shlex -- shlex raises on an unbalanced apostrophe, and "Guns N' Roses" is a
# real artist a user will type.
_TERM_RE = re.compile(r'(?:(\w+):)?(?:"([^"]*)"|(\S+))')

# Fields that filter one text column. Values are the column names -- never taken
# from user input, only looked up here, so interpolating them is safe.
_SEARCH_TEXT_FIELDS = {"artist": "artist_name", "track": "track_name",
                       "album": "album_name"}

# month:january / month:jan / month:1 all mean the same thing. Built from the
# stdlib so the twelve names aren't hand-typed (index 0 is "" in both lists).
_MONTH_NUMBERS = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
_MONTH_NUMBERS.update({a.lower(): n for n, a in enumerate(calendar.month_abbr) if a})

# day:friday / day:fri -> Postgres DOW. calendar counts Monday=0, Postgres
# counts Sunday=0, hence the shift.
_DAY_NUMBERS = {name.lower(): (n + 1) % 7 for n, name in enumerate(calendar.day_name)}
_DAY_NUMBERS.update({a.lower(): (n + 1) % 7 for n, a in enumerate(calendar.day_abbr)})

# part:night / part:morning / ... -> the same 0-3 buckets the clock draws.
_PART_NUMBERS = {name: n for n, name in enumerate(PART_NAMES)}

def _is_iso_date(value: str) -> bool:
    """date:2026-07-15 -- one calendar day. Fully validated here, not left to
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
    year or month, falls through to free text rather than being dropped -- typing
    a colon shouldn't silently delete part of the query. Free terms keep the
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
            # Unknown field or unparseable value -> search the raw text as typed.
            found["free"].append(f"{field}:{value}" if field else value)
    return found


def _scrobble_filters(user_id: int, search: str | None, start, end,
                      tz: str = "UTC") -> tuple[list, list]:
    """Build the shared WHERE clause for the history table as (conds, params).

    Both the page query and the total count call this, so the number shown
    ("of 1,204") can never disagree with the rows underneath it -- the classic
    way a paged, filtered table goes wrong is two hand-written WHERE clauses
    drifting apart. `day:`/`part:` reuse the clock's own expressions for the
    same reason: clicking a heatmap cell must return exactly the plays it counted.
    """
    conds = ["user_id = %s"]
    params: list = [user_id]
    f = parse_search(search)

    for key, column in _SEARCH_TEXT_FIELDS.items():
        for value in f[key]:
            # AND'd: `artist:radiohead artist:thom` narrows, it doesn't widen.
            conds.append(f"{column} ILIKE %s")
            params.append(f"%{value}%")

    if f["years"]:
        # One "within this year" range per requested year, OR'd together. Using a
        # date range (not EXTRACT(YEAR ...)) keeps the (user_id, listened_at)
        # index usable. Several years widen the match.
        year_conds = []
        for year in f["years"]:
            year_conds.append("(listened_at >= %s AND listened_at < %s)")
            params += [f"{year}-01-01", f"{year + 1}-01-01"]
        conds.append("(" + " OR ".join(year_conds) + ")")

    if f["months"]:
        # No index help possible for "every January" -- it's a scan by nature.
        conds.append("EXTRACT(MONTH FROM listened_at) = ANY(%s)")
        params.append(f["months"])

    if f["dates"]:
        # Same local-date expression the heatmap groups by, so clicking a cell
        # returns exactly the plays that cell counted.
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

    # The week drill-down's explicit range, AND'd on top of anything above.
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
    """A page of a user's scrobbles for the browsable, sortable history table.
    `search` (optional) supports `artist:`/`track:`/`album:`/`year:`/`month:`/
    `day:`/`part:` terms plus bare text -- see parse_search. `start`/`end`
    (optional dates) restrict to a range [start, end), which is how a clicked
    week's bar drills into its scrobbles. `tz` is the IANA zone `day:`/`part:`
    are interpreted in. `sort` + `direction` order the WHOLE filtered history
    (not just the page) so paging stays consistent; both are whitelisted.
    limit/offset paginate; caller clamps limit.
    """
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
    """How many scrobbles match the same filters get_scrobbles pages through.
    Lets the table say "1-50 of 1,204" and know when Next is exhausted, instead
    of inferring the end from a short page."""
    conds, params = _scrobble_filters(user_id, search, start, end, tz)
    cur.execute(
        f"SELECT COUNT(*) AS total FROM scrobbles WHERE {' AND '.join(conds)}", params
    )
    return cur.fetchone()["total"]


def get_song_binges(cur, user_id: int, min_plays: int):
    """Individual tracks played heavily in a short burst (the song-level twin of
    get_binges). Same rolling-7-day-window peak; keep tracks whose peak hits
    min_plays."""
    cur.execute(
        """
        WITH counted AS (
            SELECT artist_name, track_name,
                   COUNT(*) OVER (
                       PARTITION BY artist_name, track_name
                       ORDER BY listened_at
                       RANGE BETWEEN INTERVAL '7 days' PRECEDING AND CURRENT ROW
                   ) AS plays_win
            FROM scrobbles
            WHERE user_id = %s
        )
        SELECT artist_name, track_name,
               MAX(plays_win) AS peak_plays
        FROM counted
        GROUP BY artist_name, track_name
        HAVING MAX(plays_win) >= %s
        ORDER BY peak_plays DESC
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
    """Taste compatibility between two users. Pulls each user's genre play counts
    with SQL, then does the comparison math in plain Python:
      score          -- 0-100 cosine similarity of their primary-tag play vectors
      shared_artists -- artists both play, with each user's counts (top 25)
      shared_tags    -- genres both listen to, with each user's % share
      divergent_tags -- genres where their shares differ most
    """
    # 1) Each user's primary-tag play counts as a vector {tag: plays}. One query
    #    covers both users; we split the rows by user_id right after.
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

    # 2) Score = cosine similarity of the two vectors (reusing the recommender's
    #    function), shown as a 0-100 percentage.
    score = round(100 * recommender.cosine(a_plays, b_plays), 1)

    # 3) Genre shares: each tag as a percent of that user's tagged plays. "or 1"
    #    avoids divide-by-zero for a user with no tagged plays (they get 0%).
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

    # 4) Shared artists: those both users have played, with each user's counts.
    # Wrapped in a subquery so a_plays/b_plays are real columns we can filter and
    # sort by (Postgres won't allow SELECT aliases in an ORDER BY expression).
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
