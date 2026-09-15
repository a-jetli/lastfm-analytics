"""insight endpoints. no sql here: each route resolves the username, refreshes
stale data, runs one function from app/queries, and returns the rows. dict_row
makes the rows json-shaped.

all reads except /feedback, which is where a user tells the recommender it got
one wrong. it lives here rather than with the sync writes because it belongs to
the same resource as /recommendations."""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from psycopg.rows import dict_row

from app import db, lastfm, sync_service
from app.queries import analytics as q
from app.queries import recommend as q_recommend

# prefix mounts everything below under /analytics, tags groups it in /docs
router = APIRouter(prefix="/analytics", tags=["analytics"])


def _prepare(username: str) -> int:
    """resolve username to id (404 if unknown) and kick a refresh if stale.

    never blocks. a page load fans out to a dozen of these, so blocking would
    charge the wait budget once per panel. POST /sync pays it once instead.
    """
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        row = q.get_user(cur, username)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{username} not joined yet")
        user_id, last_synced_at = row["id"], row["last_synced_at"]
    # the connection closes above before the handoff, so a sync never holds one idle
    sync_service.ensure_fresh(user_id, username, last_synced_at, wait=False)
    return user_id


def _days(days: int) -> int | None:
    """range-picker days for the query layer. 0, absent or negative mean all
    time. capped at 5 years so a hand-typed ?days=99999999 cannot make postgres
    reject the interval."""
    return min(days, 1826) if days and days > 0 else None


def _tz(name: str) -> str:
    """validate an iana zone from the browser, falling back to utc. not about
    injection, since it is a bound param. postgres raises on an unknown zone,
    which would turn a junk ?tz= into a 500."""
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "UTC"


# every endpoint: _prepare, then a fresh connection, then one query


@router.get("/{username}/streaks")
def streaks(username: str, tz: str = "UTC"):
    # ?tz= because a day has to be the listener's, or utc bucketing invents streaks
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_streaks(cur, user_id, _tz(tz))


@router.get("/{username}/discovery")
def discovery(username: str, tz: str = "UTC"):
    # brand-new artists per month, in the listener's months
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_discovery(cur, user_id, _tz(tz))


@router.get("/{username}/loyalty")
def loyalty(username: str, tz: str = "UTC", days: int = 0):
    # per artist: still in rotation, or binged once and dropped. ?days= scopes
    # the whole metric to a window, anchor included. 0 is all time.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_loyalty(cur, user_id, _tz(tz), _days(days))


@router.get("/{username}/clock")
def clock(username: str, tz: str = "UTC", days: int = 365):
    # contributions-graph heatmap, one column per real date over the last
    # ?days=, split in 4. the same tz plus a date:/part: search reproduces a
    # cell exactly. ?days=0 is all time.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_clock(cur, user_id, _tz(tz), _days(days))


@router.get("/{username}/genre-clock")
def genre_clock(username: str, tz: str = "UTC", days: int = 0):
    # genre heatmap: plays per {weekday, part, tag}, same buckets as /clock so
    # it overlays cell for cell. ?days= scopes it to a window, which turns a
    # typical week into a typical week of this season.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_clock(cur, user_id, _tz(tz), _days(days))


@router.get("/{username}/summary")
def summary(username: str, tz: str = "UTC"):
    # per-month digest: plays, new artists, hours, that month's top genre
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_summary(cur, user_id, _tz(tz))


@router.get("/{username}/compatibility/{other}")
def compatibility(username: str, other: str):
    # taste match between two users. `other` can be brand new: join() pulls them
    # in on demand, waiting briefly for a first page, which is what the "enter
    # any username" promise needs. a typo still gets a clean 404.
    a_id, _ = sync_service.join(username, wait=False)  # current user, already synced
    try:
        b_id, _ = sync_service.join(other, wait=True)  # pull the other in, wait a bit
    except lastfm.LastfmUserNotFound:
        raise HTTPException(status_code=404, detail=f"No Last.fm user named '{other}'.")
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        data = q.get_compatibility(cur, a_id, b_id)
    # genres need the tag backfill, which runs after the first-page wait. tell
    # the page when either side is still syncing, so a thin first result does
    # not read as final.
    pending = sync_service.is_syncing(a_id) or sync_service.is_syncing(b_id)
    return {"user_a": username, "user_b": other, "pending": pending, **data}


@router.get("/{username}/binges")
def binges(username: str, min_plays: int = 6, days: int = 0):
    # albums played >= min_plays times inside any 7-day window. ?days= limits
    # which plays count at all. the 7-day burst window is what binge means, so
    # it is fixed.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_binges(cur, user_id, min_plays, _days(days))


@router.get("/{username}/tag-shift")
def tag_shift(username: str, period: str = "month", tz: str = "UTC", days: int = 0):
    # tag mix over time, so taste movement. ?period=week or month. rows of
    # {period_start, tag, plays, pct_of_period}, raw numbers to chart. ?days=
    # restricts to a trailing window, and also backs the Genres panel.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_tag_shift(cur, user_id, period, _tz(tz), _days(days))


@router.get("/{username}/hours")
def hours(username: str, period: str = "month", tz: str = "UTC"):
    # listening time per period in hours, from stored durations. those arrive
    # through the periodic backfill, so a period reads 0 hours until it runs.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_listening_time(cur, user_id, period, _tz(tz))


@router.get("/{username}/recommendations")
def recommendations(username: str):
    # everything the recommender has for this user, from precomputed caches.
    # all empty until the maintenance pass has run once.
    #   artists                unplayed artists ranked by taste similarity
    #   songs_from_favorites   popular tracks by their top artists, never played
    #   songs_from_new_artists entry tracks into the recommended artists
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return {
            "artists": q_recommend.get_recommendations(cur, user_id),
            "songs_from_favorites": q_recommend.get_song_recs_favorites(cur, user_id),
            "songs_from_new_artists": q_recommend.get_song_recs_discovery(cur, user_id),
            "feedback": q_recommend.get_feedback(cur, user_id),
        }


@router.get("/{username}/artists")
def artists(username: str, limit: int = 500):
    # every artist this user has played, most played first. backs the datalist
    # on the "more like this" box, so a seed is picked from your own history
    # rather than typed blind.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor() as cur:
        return q_recommend.get_top_artists(cur, user_id, max(1, min(limit, 2000)))


@router.post("/{username}/feedback")
def set_feedback(username: str, artist: str, verdict: str):
    # ?verdict=seed ("more like this") or block ("not interested"). ?artist= is
    # a query param for the same reason as /artist: names contain slashes.
    if verdict not in ("seed", "block"):
        raise HTTPException(status_code=400, detail="verdict must be seed or block")
    if not artist.strip():
        raise HTTPException(status_code=400, detail="artist is required")
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor() as cur:
        q_recommend.set_feedback(cur, user_id, artist, verdict)
        # the next pass keeps a blocked artist out of the rebuild. evicting it
        # here is what makes the click take effect now.
        if verdict == "block":
            q_recommend.drop_recommendation(cur, user_id, artist)
        conn.commit()
    return {"artist_name": artist, "verdict": verdict}


@router.delete("/{username}/feedback")
def unset_feedback(username: str, artist: str):
    # undo a seed or block. the artist goes back to being judged on plays, and
    # a blocked one can reappear after the next pass.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor() as cur:
        q_recommend.clear_feedback(cur, user_id, artist)
        conn.commit()
    return {"artist_name": artist, "verdict": None}


@router.get("/{username}/report")
def report(username: str, period: str = "month", tz: str = "UTC"):
    # per-period totals plus the change against the previous one
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_monthly_report(cur, user_id, period, _tz(tz))


@router.get("/{username}/artist")
def artist_detail(username: str, name: str):
    # detail for one artist on click-through: play count, tags, top tracks.
    # ?name= is a query param because artist names contain slashes and other
    # path-hostile characters.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_artist_detail(cur, user_id, name)


@router.get("/{username}/scrobbles")
def scrobbles(username: str, search: str = "", limit: int = 50, offset: int = 0,
              sort: str = "listened_at", dir: str = "desc",
              start: str = "", end: str = "", tz: str = "UTC"):
    # browsable, sortable play history. ?search= takes field terms plus bare
    # text, ?start=&end= is the week drill-down, ?sort=&dir= are whitelisted
    # downstream. total counts every match, not this page, so Next disables exactly.
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
    # individual tracks played hard inside any 7-day window, the song-level
    # twin of /binges. ?days= behaves the same.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_song_binges(cur, user_id, min_plays, _days(days))


@router.get("/{username}/genre")
def genre(username: str, tag: str):
    # the user's tracks in one genre (their artist's primary tag), most played
    # first. backs the genre drill-down.
    user_id = _prepare(username)
    with db.get_connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return q.get_genre_tracks(cur, user_id, tag)
