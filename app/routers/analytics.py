"""Insight endpoints (read side). No SQL here: each route resolves the
username, refreshes stale data, runs one function from app/queries, and
returns the rows. dict_row makes rows JSON-shaped dicts."""

from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from app import db, sync_service
from app.queries import analytics as q
from app.queries import recommend as q_recommend

# prefix -> every route below is mounted under /analytics; tags -> groups them in /docs
router = APIRouter(prefix="/analytics", tags=["analytics"])


def _prepare(username: str) -> int:
    """Resolve username -> id (404 if unknown) and refresh stale data first.
    The connection is closed before the (possible) sync wait."""
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = q.get_user(cur, username)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{username} not joined yet")
        user_id, last_synced_at = row["id"], row["last_synced_at"]
    # Connection closed above; now (maybe) wait for a sync without tying up a conn.
    sync_service.ensure_fresh(user_id, username, last_synced_at)
    return user_id


# Each endpoint: _prepare (resolve + refresh) -> open a fresh conn -> run one query.


@router.get("/{username}/streaks")
def streaks(username: str):
    # Longest consecutive-day listening runs.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_streaks(cur, user_id)


@router.get("/{username}/discovery")
def discovery(username: str):
    # How many brand-new artists appeared each month.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_discovery(cur, user_id)


@router.get("/{username}/loyalty")
def loyalty(username: str):
    # Per artist: steady favourite vs. short binge (see the loyalty ratio).
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_loyalty(cur, user_id)


@router.get("/{username}/clock")
def clock(username: str):
    # Plays bucketed by weekday + hour (the "when do I listen" heatmap).
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_clock(cur, user_id)


@router.get("/{username}/genre-clock")
def genre_clock(username: str):
    # Genre heatmap: plays per {weekday, hour, tag}. Raw numbers for the frontend
    # to color/stack. weekday 0=Sunday; hours are UTC (see query's TZ caveat).
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_clock(cur, user_id)


@router.get("/{username}/highlights")
def highlights(username: str):
    # Per-month report extras: most-played, top new discovery, revisited count.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_highlights(cur, user_id)


@router.get("/{username}/compatibility/{other}")
def compatibility(username: str, other: str):
    # Taste match between two users. Resolves + refreshes BOTH, then compares.
    a_id = _prepare(username)
    b_id = _prepare(other)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        data = q.get_compatibility(cur, a_id, b_id)
    return {"user_a": username, "user_b": other, **data}


@router.get("/{username}/binges")
def binges(username: str, min_plays: int = 6):
    # Albums played >= min_plays times inside any 3-day window. min_plays is a
    # ?query=param (URL: /binges?min_plays=10), defaulting to 6.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_binges(cur, user_id, min_plays)


@router.get("/{username}/tag-shift")
def tag_shift(username: str, period: str = "month"):
    # Tag mix over time (taste movement). ?period=week or month (default month).
    # Rows of {period_start, tag, plays, pct_of_period} -- raw numbers to chart.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_tag_shift(cur, user_id, period)


@router.get("/{username}/hours")
def hours(username: str, period: str = "month"):
    # Listening time per period ("month" or "week"), in hours, from stored track
    # durations. Durations arrive via the overnight backfill, so a period can
    # read 0 hours until it runs.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_time(cur, user_id, period)


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
def report(username: str, period: str = "month"):
    # Per-period totals ("month" or "week") + change vs. the previous period.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_report(cur, user_id, period)


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
              start: str = "", end: str = ""):
    # Browsable, sortable play history. ?search= matches artist or track;
    # ?start=&end= restrict to a date range (a clicked week drills in here);
    # ?sort=&dir= order the whole filtered history (whitelisted in get_scrobbles);
    # ?limit=&offset= paginate. limit is clamped so a caller can't pull it all.
    user_id = _prepare(username)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_scrobbles(cur, user_id, search or None, limit, offset, sort, dir,
                               start or None, end or None)


@router.get("/{username}/song-binges")
def song_binges(username: str, min_plays: int = 5):
    # Individual tracks played heavily inside any 3-day window (song-level twin
    # of /binges). ?min_plays= defaults to 5.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_song_binges(cur, user_id, min_plays)


@router.get("/{username}/genre")
def genre(username: str, tag: str):
    # The user's tracks in one genre (their artist's primary tag == ?tag=),
    # most played first. Backs the genre drill-down.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_tracks(cur, user_id, tag)
