"""Sync orchestration -- decides WHEN to pull from Last.fm and runs it safely.

Data counts as fresh for a day. A stale user's query (or a join) triggers a
background sync via ensure_fresh(), which waits briefly so the response has
something to show; an hourly daemon thread sweeps everyone else plus the
enrichment backfills. Two guards stop a user syncing twice at once: the
in-process _active map, and a Postgres advisory lock for the cross-process
case (tied to the connection, so a crash can't strand it).
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import db, lastfm, recommender
from app.queries import recommend as recommend_queries
from app.queries import sync as sync_queries

log = logging.getLogger(__name__)

# Data older than this is re-synced on the next query. This is the "once a day".
SYNC_INTERVAL = timedelta(days=1)
# How long the explicit join (POST /sync) blocks waiting for a sync before
# letting it finish in the background. Only paid once now that reads use
# wait=False, so it's kept short: enough for the first page of scrobbles to
# land, then the status pill takes over reporting progress.
WAIT_BUDGET_SECONDS = 4
# Gap between any two Last.fm calls. The ToS publishes no number; serial calls
# at 4/s are polite and have never been throttled.
PAGE_PAUSE_SECONDS = 0.25
# Scheduler wake interval; a stale user gets picked up within the hour.
SCHEDULER_TICK_SECONDS = 60 * 60
# How many recommended artists to cache per user in the overnight recompute.
RECOMMEND_TOP_N = 20

# user_id -> the thread currently syncing that user (in this process).
_active: dict[int, threading.Thread] = {}
# user_id -> which stage the sync is in: "pulling" (scrobbles) or "enriching"
# (durations + genre tags, which is the slow part). Lets the status endpoint say
# what's happening instead of showing a frozen play count. Shares _active_lock.
_phase: dict[int, str] = {}
_active_lock = threading.Lock()


def _set_phase(user_id: int, phase: str | None) -> None:
    with _active_lock:
        if phase is None:
            _phase.pop(user_id, None)
        else:
            _phase[user_id] = phase


def sync_phase(user_id: int) -> str | None:
    """Current sync stage for a user, or None if no sync is running."""
    with _active_lock:
        return _phase.get(user_id)


def _is_stale(last_synced_at) -> bool:
    # Never synced, or the high-water mark is older than SYNC_INTERVAL.
    if last_synced_at is None:
        return True
    return datetime.now(timezone.utc) - last_synced_at > SYNC_INTERVAL


def is_syncing(user_id: int) -> bool:
    """True if a sync thread for this user is running IN THIS PROCESS. Blind to
    other processes -- the advisory lock is the real guard. Accurate on a
    single-process deploy, which is what we run."""
    with _active_lock:
        thread = _active.get(user_id)
        return bool(thread and thread.is_alive())


def ensure_fresh(user_id: int, username: str, last_synced_at, force: bool = False,
                 wait: bool = True) -> None:
    """Kick a sync if the data is new, stale or `force`, then block up to
    WAIT_BUDGET_SECONDS. `force` is an explicit Load press; the sync stays
    incremental so it is cheap anyway.

    `wait=False` never blocks, which is what analytics reads use: the budget is
    paid ONCE on the join, or a page fetching a dozen panels pays it a dozen
    times and the loading screen becomes half a minute."""
    with _active_lock:
        thread = _active.get(user_id)
        if thread and thread.is_alive():
            # A sync is already in flight -- wait on it below, don't start another.
            pass
        elif force or _is_stale(last_synced_at):
            # Incremental if we've synced before (only pull plays after the mark),
            # full backfill if not (since = None).
            since = int(last_synced_at.timestamp()) if last_synced_at else None
            thread = threading.Thread(
                target=_run_sync, args=(user_id, username, since), daemon=True
            )
            _active[user_id] = thread
            thread.start()
        else:
            return  # fresh -- nothing to do
    if not wait:
        return  # sync is running in the background; caller reads what's committed
    # Wait OUTSIDE the lock so other users aren't blocked. If the sync isn't done
    # in WAIT_BUDGET_SECONDS the thread keeps running; we just return what's there.
    thread.join(timeout=WAIT_BUDGET_SECONDS)


def join(username: str, force: bool = False, wait: bool = True) -> tuple[int, bool]:
    """Resolve `username` to a user id, creating and syncing on first sight.
    Returns (user_id, is_new).

    A new handle is validated against Last.fm BEFORE its row is created, so a
    typo leaves no phantom user. A transient outage during that check is
    swallowed rather than blocking the join on a blip.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        row = sync_queries.get_user(cur, username)
    if row:
        user_id, last_synced_at = row
        ensure_fresh(user_id, username, last_synced_at, force=force, wait=wait)
        return user_id, False

    # New handle: confirm it exists before writing a row.
    try:
        lastfm.getrecents(username, page=1, limit=1)
    except lastfm.LastfmUserNotFound:
        raise
    except Exception:
        log.warning("could not pre-validate %s against Last.fm; joining anyway", username)

    with db.get_connection() as conn, conn.cursor() as cur:
        user_id = sync_queries.create_user(cur, username)
    ensure_fresh(user_id, username, None, force=True, wait=wait)
    return user_id, True


def _run_sync(user_id: int, username: str, since: int | None) -> None:
    """Background worker: fetch pages, store them, advance the high-water mark.
    Runs on its own DB connection so its commits are visible to live queries."""
    me = threading.current_thread()
    try:
        with db.get_connection() as conn, conn.cursor() as cur:
            # Cross-process guard: only one process syncs this user at a time.
            cur.execute("SELECT pg_try_advisory_lock(%s)", (user_id,))
            if not cur.fetchone()[0]:
                return  # another process already has it
            synced = False
            try:
                _set_phase(user_id, "pulling")  # fetching scrobble pages
                _paginate(conn, cur, user_id, username, since)
                synced = True
            finally:
                # Session-level lock survives our per-page commits, so release it
                # explicitly (closing the connection would also release it).
                cur.execute("SELECT pg_advisory_unlock(%s)", (user_id,))
        # Enrich this user's new tracks right away (still in the background
        # thread) so /hours and genre features work after a fresh sync. This is
        # the slow stage (one Last.fm call per new track/artist), hence its own
        # phase so the UI can say "adding genres" rather than look stuck.
        if synced:
            _set_phase(user_id, "enriching")
            _backfill_durations(user_id)
            _backfill_artist_tags(user_id)
    except lastfm.LastfmUserNotFound:
        # The handle vanished from Last.fm (deleted account, rename). Nothing to
        # pull; leave existing data alone. New-user typos are caught earlier in
        # join(), so this only happens on a refresh of a once-valid user.
        log.warning("Last.fm no longer knows user_id=%s (%s)", user_id, username)
    except Exception:
        # A failed sync leaves last_synced_at untouched -> still stale -> retried
        # on the next query. Never let a background error crash the app.
        log.exception("sync failed for user_id=%s", user_id)
    finally:
        # Drop ourselves from the active map + clear the phase (only if we're
        # still the current entry).
        with _active_lock:
            if _active.get(user_id) is me:
                del _active[user_id]
                _phase.pop(user_id, None)


def _paginate(conn, cur, user_id: int, username: str, since: int | None) -> None:
    """Walk Last.fm pages newest-first, committing each page so recent plays
    show up first during the loading window. Advance the mark only at the end
    (to the START time -- see update_last_synced for why)."""
    started_at = datetime.now(timezone.utc)
    tracks, total_pages = lastfm.getrecents(username, page=1, since=since)
    _store_page(cur, user_id, tracks)
    conn.commit()  # page 1 (newest) visible to live queries right away

    for page in range(2, total_pages + 1):
        tracks, _ = lastfm.getrecents(username, page=page, since=since)
        _store_page(cur, user_id, tracks)
        conn.commit()
        time.sleep(PAGE_PAUSE_SECONDS)

    # Only now is the pull complete -- move the high-water mark forward.
    sync_queries.update_last_synced(cur, user_id, started_at)
    conn.commit()


def _store_page(cur, user_id: int, tracks: list) -> None:
    # ON CONFLICT DO NOTHING (in insert_scrobble) makes re-inserts harmless, so
    # overlapping pages or a re-run never duplicate rows.
    for track in tracks:
        sync_queries.insert_scrobble(cur, user_id, track)


# --- Daily background refresh (the "once a day" job; no external cron) --------


def start_scheduler() -> None:
    """Start the hourly maintenance loop (called on app startup). Daemon
    thread, so it only runs while the app process is up."""
    threading.Thread(target=_scheduler_loop, name="daily-sync", daemon=True).start()


def _scheduler_loop() -> None:
    # Sleep first so a fresh boot (and short-lived test runs) don't immediately
    # hammer Last.fm; then run the overnight pass over and over, forever.
    while True:
        time.sleep(SCHEDULER_TICK_SECONDS)
        try:
            _overnight_pass()
        except Exception:
            log.exception("scheduled maintenance pass failed")


def _overnight_pass() -> None:
    """One maintenance pass per scheduler tick.

    Order matters: sync first so new tracks/artists exist before the enrichment
    steps look for them, recommendations after the tags they score against, and
    top tracks last so newly recommended artists get songs too.

    Every step is incremental (work list = rows not yet enriched) and global (one
    lookup per artist/track, shared across users), so the corpus grows on its own
    from what users actually play. No crawler, which is what keeps stored Last.fm
    data under the ToS 100MB cap by construction.
    """
    _sync_all_stale()
    _backfill_durations()
    _backfill_artist_tags()
    _refresh_recommendations()
    _backfill_top_tracks()


def _backfill_durations(user_id: int | None = None) -> None:
    """Durations for tracks not yet in track_durations. Commits per row so an
    interrupted pass keeps progress; 0 ms is stored so we don't re-ask. Pass
    user_id right after a sync, omit for the overnight sweep."""
    with db.get_connection() as conn, conn.cursor() as cur:
        pairs = sync_queries.get_tracks_missing_durations(cur, user_id)
        for artist_name, track_name in pairs:
            try:
                duration_ms = lastfm.get_track_info(artist_name, track_name)
            except Exception:
                # Not stored -> retried on the next pass. One bad lookup (network
                # blip, odd response) must not abort the whole backfill.
                log.exception(
                    "duration fetch failed for %s - %s", artist_name, track_name
                )
                continue
            sync_queries.insert_track_duration(cur, artist_name, track_name, duration_ms)
            conn.commit()
            time.sleep(PAGE_PAUSE_SECONDS)


def _backfill_artist_tags(user_id: int | None = None) -> None:
    """Fetch genre tags for artists not yet in artist_tags (raw; cleaning is
    at read time). An artist with no tags gets a sentinel row (tag='') so it
    isn't re-fetched. Same user_id semantics as _backfill_durations."""
    with db.get_connection() as conn, conn.cursor() as cur:
        artists = sync_queries.get_artists_missing_tags(cur, user_id)
        for (artist_name,) in artists:
            try:
                tags = lastfm.get_artist_tags(artist_name)
            except Exception:
                # Not stored -> retried next pass. One bad lookup mustn't abort.
                log.exception("tag fetch failed for %s", artist_name)
                continue
            if tags:
                for tag, weight in tags:
                    sync_queries.insert_artist_tag(cur, artist_name, tag, weight)
            else:
                # "Fetched, none found" marker so NOT EXISTS stops returning it.
                sync_queries.insert_artist_tag(cur, artist_name, "", 0)
            conn.commit()
            time.sleep(PAGE_PAUSE_SECONDS)


def _refresh_recommendations() -> None:
    """Recompute every user's cached artist recommendations.

    The shared work (corpus load, idf, all artist vectors) happens once and is
    reused for every user; only the taste vector + ranking is per-user. Math
    lives in app/recommender.py."""
    with db.get_connection() as conn, conn.cursor() as cur:
        corpus_rows = recommend_queries.get_tag_corpus(cur)
        if not corpus_rows:
            return  # no tags yet -- nothing to recommend against
        # Fold flat (artist, tag, weight) rows into {artist: {tag: weight}}.
        corpus: dict[str, dict[str, float]] = {}
        for artist_name, tag, weight in corpus_rows:
            corpus.setdefault(artist_name, {})[tag] = weight

        idf = recommender.compute_idf(corpus)
        artist_vectors = recommender.build_artist_vectors(corpus, idf)

        for user_id in recommend_queries.get_all_user_ids(cur):
            # {artist: recency-weighted play score}. Keys are every artist the
            # user has played (the exclusion set); values weight recent plays up.
            plays = dict(recommend_queries.get_user_plays(cur, user_id))
            user_vector = recommender.build_user_vector(plays, artist_vectors)
            if not user_vector:
                continue  # user has no tagged artists yet -- skip, leave cache
            ranked = recommender.recommend(
                user_vector,
                artist_vectors,
                already_played=set(plays),
                k=RECOMMEND_TOP_N,
            )
            recommend_queries.replace_recommendations(cur, user_id, ranked)
            conn.commit()  # per-user, so a crash keeps earlier users' results


def _backfill_top_tracks() -> None:
    """Fetch and store each wanted artist's top tracks (see the work-list query
    for which artists qualify). Same loop shape as the other backfills: per-row
    commit, per-call pause, one bad lookup never aborts the pass. An unknown
    artist gets a sentinel row (track_name='') so it isn't re-fetched."""
    with db.get_connection() as conn, conn.cursor() as cur:
        artists = recommend_queries.get_artists_missing_top_tracks(cur)
        for (artist_name,) in artists:
            try:
                tracks = lastfm.get_artist_top_tracks(artist_name)
            except Exception:
                log.exception("top-tracks fetch failed for %s", artist_name)
                continue
            if tracks:
                for rank, track_name in enumerate(tracks, start=1):
                    recommend_queries.insert_top_track(cur, artist_name, track_name, rank)
            else:
                recommend_queries.insert_top_track(cur, artist_name, "", 0)
            conn.commit()
            time.sleep(PAGE_PAUSE_SECONDS)


def _sync_all_stale() -> None:
    """One pass: sync every user whose data is over a day old, one at a time.
    Reuses the same incremental sync + advisory lock as on-demand syncs, so a
    scheduled pass and a user's live query can never double-fetch each other."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, lastfm_username, last_synced_at FROM users")
        users = cur.fetchall()
    for user_id, username, last_synced_at in users:
        if _is_stale(last_synced_at):
            since = int(last_synced_at.timestamp()) if last_synced_at else None
            _run_sync(user_id, username, since)


if __name__ == "__main__":
    # Run one maintenance pass now instead of waiting for the hourly scheduler.
    # Use it right after a deploy or a fresh join so recommendations and genre
    # data don't sit empty for up to an hour. Safe to re-run any time: every step
    # is incremental and idempotent. Invoke with `python -m app.sync_service`
    # (inside the container: `docker compose exec app python -m app.sync_service`).
    logging.basicConfig(level=logging.INFO)
    log.info("running one maintenance pass on demand")
    _overnight_pass()
    log.info("maintenance pass complete")
