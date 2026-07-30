"""Insight endpoints (read side). No SQL here: each route resolves the
username, refreshes stale data, runs one function from app/queries, and
returns the rows. dict_row makes rows JSON-shaped dicts."""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from app import db, lastfm, sync_service
from app.queries import analytics as q
from app.queries import recommend as q_recommend

# prefix -> every route below is mounted under /analytics; tags -> groups them in /docs
router = APIRouter(prefix="/analytics", tags=["analytics"])


def _prepare(username: str) -> int:
    """Resolve username -> id (404 if unknown) and kick a refresh if data is stale.

    Never blocks: wait=False. A page load fans out to a dozen of these endpoints,
    so blocking here would charge the sync wait budget once PER PANEL (and the
    browser's ~6-connection cap turns that into serial waves). The explicit join
    -- POST /sync -- pays the wait once; these reads return what is committed so
    far and the page polls /sync/{user}/status to show progress.
    """
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = q.get_user(cur, username)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{username} not joined yet")
        user_id, last_synced_at = row["id"], row["last_synced_at"]
    # Connection closed above before handing off, so a sync never holds one idle.
    sync_service.ensure_fresh(user_id, username, last_synced_at, wait=False)
    return user_id


def _days(days: int) -> int | None:
    """Range-picker window in days -> what the query layer wants. 0, absent or
    negative all mean "all time" (None). Capped at 5 years so a hand-typed
    ?days=99999999 can't turn into an interval Postgres rejects."""
    return min(days, 1826) if days and days > 0 else None


def _tz(name: str) -> str:
    """Validate an IANA timezone name from the browser, falling back to UTC.

    It reaches SQL as a bound parameter, so this isn't about injection -- it's
    that Postgres raises on an unknown zone, which would turn a junk ?tz= into
    a 500. zoneinfo is the same tz database Postgres reads, so agreement is free.
    """
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


# Each endpoint: _prepare (resolve + refresh) -> open a fresh conn -> run one query.


@router.get("/{username}/streaks")
def streaks(username: str, tz: str = "UTC"):
    # Longest consecutive-day listening runs. ?tz= because a "day" has to be the
    # listener's day, or UTC bucketing reports streaks that never happened.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_streaks(cur, user_id, _tz(tz))


@router.get("/{username}/discovery")
def discovery(username: str, tz: str = "UTC"):
    # How many brand-new artists appeared each month, in the listener's months.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_discovery(cur, user_id, _tz(tz))


@router.get("/{username}/loyalty")
def loyalty(username: str, tz: str = "UTC"):
    # Per artist: still in rotation vs. binged once and dropped (see the ratio).
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_loyalty(cur, user_id, _tz(tz))


@router.get("/{username}/clock")
def clock(username: str, tz: str = "UTC", days: int = 365):
    # Per-day listening heatmap (a contributions graph): one column per real
    # date over the last ?days=, each split into 4 parts of day. ?tz= is the
    # browser's IANA zone so a play lands on the listener's calendar day and
    # part; the same tz + a date:/part: search reproduce a clicked cell exactly.
    user_id = _prepare(username)
    days = max(1, min(days, 366))
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_clock(cur, user_id, _tz(tz), days)


@router.get("/{username}/genre-clock")
def genre_clock(username: str, tz: str = "UTC"):
    # Genre heatmap: plays per {weekday, part, tag}. Raw numbers for the frontend
    # to color/stack. weekday 0=Sunday, part 0-3; ?tz= as on /clock, and the same
    # buckets, so this overlays the listening clock cell for cell.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_clock(cur, user_id, _tz(tz))


@router.get("/{username}/summary")
def summary(username: str, tz: str = "UTC"):
    # Per-month digest: plays, new artists, hours, and that month's top genre.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_summary(cur, user_id, _tz(tz))


@router.get("/{username}/compatibility/{other}")
def compatibility(username: str, other: str):
    # Taste match between two users. `other` may be brand new: join() pulls them
    # in on demand (waiting briefly for a first page) rather than 404-ing, which
    # is what the "enter any username" promise on the page needs. A typo'd handle
    # still returns a clean 404.
    a_id, _ = sync_service.join(username, wait=False)  # current user, already synced
    try:
        b_id, _ = sync_service.join(other, wait=True)  # pull the other in, wait a bit
    except lastfm.LastfmUserNotFound:
        raise HTTPException(status_code=404, detail=f"No Last.fm user named '{other}'.")
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        data = q.get_compatibility(cur, a_id, b_id)
    # Genres need the tag backfill, which runs after the first-page wait. Tell the
    # page if either side is still syncing so it can say "compare again shortly"
    # instead of the user reading a thin first result as final.
    pending = sync_service.is_syncing(a_id) or sync_service.is_syncing(b_id)
    return {"user_a": username, "user_b": other, "pending": pending, **data}


@router.get("/{username}/binges")
def binges(username: str, min_plays: int = 6, days: int = 0):
    # Albums played >= min_plays times inside any 7-day window. ?days= limits
    # which plays are considered at all (0 = all time); the 7-day burst window
    # is what "binge" means and is fixed.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_binges(cur, user_id, min_plays, _days(days))


@router.get("/{username}/tag-shift")
def tag_shift(username: str, period: str = "month", tz: str = "UTC", days: int = 0):
    # Tag mix over time (taste movement). ?period=week or month (default month).
    # Rows of {period_start, tag, plays, pct_of_period} -- raw numbers to chart.
    # ?days= restricts to a trailing window (0 = all time); it also backs the
    # Genres panel, which sums these rows.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_tag_shift(cur, user_id, period, _tz(tz), _days(days))


@router.get("/{username}/hours")
def hours(username: str, period: str = "month", tz: str = "UTC"):
    # Listening time per period ("month" or "week"), in hours, from stored track
    # durations. Durations arrive via the overnight backfill, so a period can
    # read 0 hours until it runs.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_time(cur, user_id, period, _tz(tz))


@router.get("/{username}/recommendations")
def recommendations(username: str):
    # Everything the recommender has for this user, served from nightly caches
    # (lists are empty until the overnight pass has run once):
    #   artists        -- unplayed artists ranked by taste-vector similarity
    #   songs_from_favorites -- popular tracks by their top artists, never played
    #   songs_from_new_artists -- entry tracks into the recommended artists
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return {
            "artists": q_recommend.get_recommendations(cur, user_id),
            "songs_from_favorites": q_recommend.get_song_recs_favorites(cur, user_id),
            "songs_from_new_artists": q_recommend.get_song_recs_discovery(cur, user_id),
        }


@router.get("/{username}/report")
def report(username: str, period: str = "month", tz: str = "UTC"):
    # Per-period totals ("month" or "week") + change vs. the previous period.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_report(cur, user_id, period, _tz(tz))


@router.get("/{username}/artist")
def artist_detail(username: str, name: str):
    # Detail for one artist (click-through): the user's play count, the artist's
    # genre tags, and top tracks. ?name= as a query param since artist names can
    # contain slashes and other path-hostile characters.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_artist_detail(cur, user_id, name)


@router.get("/{username}/scrobbles")
def scrobbles(username: str, search: str = "", limit: int = 50, offset: int = 0,
              sort: str = "listened_at", dir: str = "desc",
              start: str = "", end: str = "", tz: str = "UTC"):
    # Browsable, sortable play history. ?search= takes field terms
    # (artist:/track:/album:/year:/month:) and bare text, which still matches
    # artist OR track; ?start=&end= restrict to a date range (a clicked week
    # drills in here); ?sort=&dir= order the whole filtered history (whitelisted
    # in get_scrobbles); ?limit=&offset= paginate, limit clamped.
    # Returns {total, limit, offset, rows}: `total` counts every match, not just
    # this page, so the table can show "1-50 of 1,204" and disable Next exactly.
    user_id = _prepare(username)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    zone = _tz(tz)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        rows = q.get_scrobbles(cur, user_id, search or None, limit, offset, sort, dir,
                               start or None, end or None, zone)
        total = q.count_scrobbles(cur, user_id, search or None, start or None,
                                  end or None, zone)
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


@router.get("/{username}/song-binges")
def song_binges(username: str, min_plays: int = 5, days: int = 0):
    # Individual tracks played heavily inside any 7-day window (song-level twin
    # of /binges). ?min_plays= defaults to 5, ?days= as on /binges.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_song_binges(cur, user_id, min_plays, _days(days))


@router.get("/{username}/genre")
def genre(username: str, tag: str):
    # The user's tracks in one genre (their artist's primary tag == ?tag=),
    # most played first. Backs the genre drill-down.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_tracks(cur, user_id, tag)
