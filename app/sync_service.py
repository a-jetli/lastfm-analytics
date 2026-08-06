"""Sync orchestration - when to pull from Last.fm, and doing it safely.

Data is fresh for a day. A stale user's query (or a join) kicks off a background
sync via ensure_fresh(), which waits a moment so the response has something to
show. An hourly daemon thread sweeps everyone else plus the enrichment
backfills. Two guards stop a user syncing twice at once: the in-process _active
map, and a Postgres advisory lock for the cross-process case (tied to the
connection, so a crash can't strand it).
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import db, lastfm, recommender
from app.queries import recommend as recommend_queries
from app.queries import sync as sync_queries

log = logging.getLogger(__name__)

# older than this and the next query re-syncs. the "once a day".
SYNC_INTERVAL = timedelta(days=1)
# how long an explicit join (POST /sync) blocks before letting the sync finish
# in the background. only paid once now reads use wait=False, so keep it short -
# enough for the first page of scrobbles to land, then the status pill takes over.
WAIT_BUDGET_SECONDS = 4
# gap between any two Last.fm calls. the ToS gives no number, but serial calls
# at 4/s are polite and have never been throttled.
PAGE_PAUSE_SECONDS = 0.25
# scheduler wake interval, so a stale user is picked up within the hour
SCHEDULER_TICK_SECONDS = 60 * 60
# recommended artists cached per user by the maintenance recompute
RECOMMEND_TOP_N = 20

# user_id -> the thread currently syncing them, in this process
_active: dict[int, threading.Thread] = {}
# user_id -> stage: "pulling" (scrobbles) or "enriching" (durations + genre
# tags, the slow part). lets the status endpoint say what's happening rather
# than show a frozen play count. shares _active_lock.
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
    # never synced, or the high-water mark is older than SYNC_INTERVAL
    if last_synced_at is None:
        return True
    return datetime.now(timezone.utc) - last_synced_at > SYNC_INTERVAL


def is_syncing(user_id: int) -> bool:
    """True if a sync thread for this user is running in this process. Blind to
    other processes - the advisory lock is the real guard. Accurate on a
    single-process deploy, which is what we run."""
    with _active_lock:
        thread = _active.get(user_id)
        return bool(thread and thread.is_alive())


def ensure_fresh(user_id: int, username: str, last_synced_at, force: bool = False,
                 wait: bool = True) -> None:
    """Kick a sync if the data is new, stale or `force`, then block up to
    WAIT_BUDGET_SECONDS. `force` is an explicit Load press; the sync stays
    incremental so it's cheap anyway.

    `wait=False` never blocks, which is what analytics reads use. The budget gets
    paid once on the join - otherwise a page fetching a dozen panels pays it a
    dozen times and the loading screen takes half a minute."""
    with _active_lock:
        thread = _active.get(user_id)
        if thread and thread.is_alive():
            # already in flight - wait on it below, don't start a second
            pass
        elif force or _is_stale(last_synced_at):
            # incremental if we've synced before (only plays after the mark),
            # full backfill if not (since = None)
            since = int(last_synced_at.timestamp()) if last_synced_at else None
            thread = threading.Thread(
                target=_run_sync, args=(user_id, username, since), daemon=True
            )
            _active[user_id] = thread
            thread.start()
        else:
            return  # fresh, nothing to do
    if not wait:
        return  # sync is running in the background; caller reads what's committed
    # wait outside the lock so other users aren't blocked. if the sync isn't done
    # in WAIT_BUDGET_SECONDS the thread keeps going, we just return what's there.
    thread.join(timeout=WAIT_BUDGET_SECONDS)


def join(username: str, force: bool = False, wait: bool = True) -> tuple[int, bool]:
    """Resolve `username` to a user id, creating and syncing on first sight.
    Returns (user_id, is_new).

    A new handle is checked against Last.fm before its row is created, so a typo
    leaves no phantom user. A transient outage during that check is swallowed
    rather than blocking the join on a blip.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        row = sync_queries.get_user(cur, username)
    if row:
        user_id, last_synced_at = row
        ensure_fresh(user_id, username, last_synced_at, force=force, wait=wait)
        return user_id, False

    # new handle, so confirm it exists before writing a row
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
            # cross-process guard: one process per user at a time
            cur.execute("SELECT pg_try_advisory_lock(%s)", (user_id,))
            if not cur.fetchone()[0]:
                return  # another process already has it
            synced = False
            try:
                _set_phase(user_id, "pulling")  # fetching scrobble pages
                _paginate(conn, cur, user_id, username, since)
                synced = True
            finally:
                # session-level lock survives the per-page commits, so release it
                # explicitly (closing the connection would do it too)
                cur.execute("SELECT pg_advisory_unlock(%s)", (user_id,))
        # enrich the new tracks right away, still on the background thread, so
        # /hours and genre features work after a fresh sync. slow stage - one
        # Last.fm call per new track/artist - hence its own phase, so the UI can
        # say "adding genres" instead of looking stuck.
        if synced:
            _set_phase(user_id, "enriching")
            _backfill_durations(user_id)
            _backfill_artist_tags(user_id)
    except lastfm.LastfmUserNotFound:
        # handle vanished from Last.fm (deleted, renamed). nothing to pull, leave
        # what's there. typos get caught earlier in join(), so this only happens
        # refreshing a user who used to be valid.
        log.warning("Last.fm no longer knows user_id=%s (%s)", user_id, username)
    except Exception:
        # a failed sync leaves last_synced_at alone -> still stale -> retried on
        # the next query. never let a background error take the app down.
        log.exception("sync failed for user_id=%s", user_id)
    finally:
        # drop out of the active map and clear the phase, but only if we're
        # still the current entry
        with _active_lock:
            if _active.get(user_id) is me:
                del _active[user_id]
                _phase.pop(user_id, None)


def _paginate(conn, cur, user_id: int, username: str, since: int | None) -> None:
    """Walk Last.fm pages newest first, committing each one so recent plays show
    up first during the loading window. The mark only moves at the end, and to
    the start time - see update_last_synced for why."""
    started_at = datetime.now(timezone.utc)
    tracks, total_pages = lastfm.getrecents(username, page=1, since=since)
    _store_page(cur, user_id, tracks)
    conn.commit()  # page 1 (newest) hits live queries right away

    for page in range(2, total_pages + 1):
        tracks, _ = lastfm.getrecents(username, page=page, since=since)
        _store_page(cur, user_id, tracks)
        conn.commit()
        time.sleep(PAGE_PAUSE_SECONDS)

    # pull is complete, so move the high-water mark forward
    sync_queries.update_last_synced(cur, user_id, started_at)
    conn.commit()


def _store_page(cur, user_id: int, tracks: list) -> None:
    # ON CONFLICT DO NOTHING in insert_scrobble makes re-inserts harmless, so
    # overlapping pages or a re-run never duplicate rows
    for track in tracks:
        sync_queries.insert_scrobble(cur, user_id, track)


# --- daily background refresh (the "once a day" job, no external cron) -------


def start_scheduler() -> None:
    """Start the hourly maintenance loop, called on app startup. Daemon thread,
    so it only runs while the app process is up."""
    threading.Thread(target=_scheduler_loop, name="daily-sync", daemon=True).start()


def _scheduler_loop() -> None:
    # sleep first so a fresh boot (and short test runs) don't immediately hammer
    # Last.fm, then run the pass over and over
    while True:
        time.sleep(SCHEDULER_TICK_SECONDS)
        try:
            _maintenance_pass()
        except Exception:
            log.exception("scheduled maintenance pass failed")


def _maintenance_pass() -> None:
    """One maintenance pass per scheduler tick.

    Order matters: sync first so new tracks/artists exist before the enrichment
    steps look for them, recommendations after the tags they score against, top
    tracks last so newly recommended artists get songs too.

    Every step is incremental (work list = rows not yet enriched) and global (one
    lookup per artist/track, shared across users), so the corpus grows on its own
    from what people actually play. No crawler, which is what keeps stored
    Last.fm data under the ToS 100MB cap by construction.
    """
    _sync_all_stale()
    _backfill_durations()
    _backfill_artist_tags()
    _widen_candidate_pool()  # before recommendations - it's what they score against
    _refresh_recommendations()
    _backfill_top_tracks()


def _widen_candidate_pool() -> None:
    """Pull in artists nobody here has played, so there's something to recommend.

    Every other artist in the corpus arrives through scrobbles, which makes the
    recommender useless at low user counts: candidates are "tagged artists you
    haven't played", and with one user that set is empty by construction. On the
    live box it produced exactly one recommendation.

    Per user: take their top SEED_ARTISTS plus any artist they explicitly asked
    for more of, ask Last.fm for SIMILAR_PER_ARTIST similar artists each, drop
    everything already known, fetch tags for the rest. The new rows are
    artist_tags only with no scrobbles attached, which is what makes them
    recommendable rather than already played.

    Cost is bounded and shrinks. The work list is filtered by NOT EXISTS, so a
    user's first pass fetches up to SEED_ARTISTS * SIMILAR_PER_ARTIST names and
    later ones fetch almost none as the corpus converges. Tag rows are tiny (a
    name, a tag, an int), so this stays well inside the ToS 100MB cap. The real
    cost is call volume, which the pause below keeps polite.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        user_ids = recommend_queries.get_all_user_ids(cur)

    for user_id in user_ids:
        with db.get_connection() as conn, conn.cursor() as cur:
            # a "more like this" the pass hasn't looked up yet is always worth a
            # call, however full the list already is - it's the user asking for a
            # direction the play history doesn't point in.
            pending = recommend_queries.get_pending_seeds(cur, user_id)
            # otherwise skip users the pool already serves. without this the 10
            # getSimilar calls below repeat every tick, per user, forever,
            # learning nothing: the tag fetches stop (NOT EXISTS) but the seed
            # lookups don't.
            if not pending and (
                len(recommend_queries.get_recommendations(cur, user_id)) >= RECOMMEND_TOP_N
            ):
                continue
            # dict.fromkeys-style dedupe on lowercase: a seed that's also a top
            # artist is one lookup, not two
            seeds = list({
                name.lower(): name
                for name in pending + recommend_queries.get_top_artists(cur, user_id)
            }.values())

        similar: list[str] = []
        for seed in seeds:
            try:
                similar += lastfm.get_similar_artists(
                    seed, limit=recommend_queries.SIMILAR_PER_ARTIST
                )
            except Exception:
                # one bad lookup mustn't abort the pass, retried next tick
                log.exception("similar-artist fetch failed for %s", seed)
            time.sleep(PAGE_PAUSE_SECONDS)

        # dedupe the batch before asking the DB, so a name several seeds suggest
        # costs one filter check and one tag fetch instead of five
        seen: dict[str, str] = {}
        for name in similar:
            seen.setdefault(name.lower(), name)

        # one connection for the filter and the whole insert loop, committing per
        # artist, same as _backfill_artist_tags. one per artist meant ~90 connects
        # on a user's first pass for nothing.
        with db.get_connection() as conn, conn.cursor() as cur:
            unknown = recommend_queries.filter_unknown_artists(cur, list(seen.values()))
            for artist_name in unknown:
                try:
                    tags = lastfm.get_artist_tags(artist_name)
                except Exception:
                    log.exception("tag fetch failed for candidate %s", artist_name)
                    continue
                if tags:
                    for tag, weight in tags:
                        sync_queries.insert_artist_tag(cur, artist_name, tag, weight)
                else:
                    # same sentinel the scrobble-driven backfill uses: asked,
                    # nothing there. filter_unknown_artists stops returning it.
                    sync_queries.insert_artist_tag(cur, artist_name, "", 0)
                conn.commit()
                time.sleep(PAGE_PAUSE_SECONDS)
            # stamped after the lookups, so a crash mid-pass retries them
            recommend_queries.mark_seeds_expanded(cur, user_id)
            conn.commit()


def _backfill_durations(user_id: int | None = None) -> None:
    """Durations for tracks not yet in track_durations. Commits per row so an
    interrupted pass keeps its progress, and stores 0 ms so we don't re-ask.
    Pass user_id right after a sync, omit it for the periodic sweep."""
    with db.get_connection() as conn, conn.cursor() as cur:
        pairs = sync_queries.get_tracks_missing_durations(cur, user_id)
        for artist_name, track_name in pairs:
            try:
                duration_ms = lastfm.get_track_info(artist_name, track_name)
            except Exception:
                # not stored -> retried next pass. one bad lookup (network blip,
                # odd response) mustn't take down the whole backfill.
                log.exception(
                    "duration fetch failed for %s - %s", artist_name, track_name
                )
                continue
            sync_queries.insert_track_duration(cur, artist_name, track_name, duration_ms)
            conn.commit()
            time.sleep(PAGE_PAUSE_SECONDS)


def _backfill_artist_tags(user_id: int | None = None) -> None:
    """Fetch genre tags for artists not yet in artist_tags, raw - cleaning happens
    at read time. An artist with no tags gets a sentinel row (tag='') so it isn't
    re-fetched. Same user_id semantics as _backfill_durations."""
    with db.get_connection() as conn, conn.cursor() as cur:
        artists = sync_queries.get_artists_missing_tags(cur, user_id)
        for (artist_name,) in artists:
            try:
                tags = lastfm.get_artist_tags(artist_name)
            except Exception:
                # not stored -> retried next pass. one bad lookup mustn't abort.
                log.exception("tag fetch failed for %s", artist_name)
                continue
            if tags:
                for tag, weight in tags:
                    sync_queries.insert_artist_tag(cur, artist_name, tag, weight)
            else:
                # "asked, none found" marker so NOT EXISTS stops returning it
                sync_queries.insert_artist_tag(cur, artist_name, "", 0)
            conn.commit()
            time.sleep(PAGE_PAUSE_SECONDS)


def _refresh_recommendations() -> None:
    """Recompute every user's cached artist recommendations.

    The shared work (corpus load, idf, all artist vectors) happens once and gets
    reused for every user. Only the taste vector and ranking are per-user. Math
    lives in app/recommender.py.

    Explicit feedback folds into the two inputs the math already takes: a seed
    joins the play scores, a block joins the exclusion set. No special case in
    the recommender itself."""
    with db.get_connection() as conn, conn.cursor() as cur:
        corpus_rows = recommend_queries.get_tag_corpus(cur)
        if not corpus_rows:
            return  # no tags yet, nothing to recommend against
        # fold flat (artist, tag, weight) rows into {artist: {tag: weight}}
        corpus: dict[str, dict[str, float]] = {}
        for artist_name, tag, weight in corpus_rows:
            corpus.setdefault(artist_name, {})[tag] = weight

        idf = recommender.compute_idf(corpus)
        artist_vectors = recommender.build_artist_vectors(corpus, idf)

        for user_id in recommend_queries.get_all_user_ids(cur):
            # {artist: recency-weighted play score}. keys are every artist they
            # have played (the exclusion set), values weight recent plays up.
            plays = dict(recommend_queries.get_user_plays(cur, user_id))
            user_vector = recommender.build_user_vector(plays, artist_vectors)
            if not user_vector:
                continue  # no tagged artists yet, skip and leave the cache
            seeds = recommend_queries.get_feedback_names(cur, user_id, "seed")
            blocked = recommend_queries.get_feedback_names(cur, user_id, "block")
            # seeds are picks, not plays, so they carry equal weight rather than
            # a count. same builder as the taste vector, so recommend() is
            # comparing like with like.
            seed_vector = (
                recommender.build_user_vector({name: 1.0 for name in seeds}, artist_vectors)
                if seeds else None
            )
            ranked = recommender.recommend(
                user_vector,
                artist_vectors,
                # a seeded artist is excluded from its own results: they've
                # already told us they like it
                already_played=set(plays) | set(seeds) | set(blocked),
                k=RECOMMEND_TOP_N,
                seed_vector=seed_vector,
            )
            recommend_queries.replace_recommendations(cur, user_id, ranked)
            conn.commit()  # per user, so a crash keeps earlier users' results


def _backfill_top_tracks() -> None:
    """Fetch and store each wanted artist's top tracks - the work-list query says
    which artists qualify. Same loop shape as the other backfills: per-row commit,
    per-call pause, one bad lookup never aborts the pass. An unknown artist gets a
    sentinel row (track_name='') so it isn't re-fetched."""
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
    """Sync every user whose data is over a day old, one at a time. Reuses the
    same incremental sync and advisory lock as on-demand syncs, so a scheduled
    pass and a live query can't double-fetch each other."""
    with db.get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, lastfm_username, last_synced_at FROM users")
        users = cur.fetchall()
    for user_id, username, last_synced_at in users:
        if _is_stale(last_synced_at):
            since = int(last_synced_at.timestamp()) if last_synced_at else None
            _run_sync(user_id, username, since)


if __name__ == "__main__":
    # one maintenance pass now instead of waiting an hour for the scheduler. use
    # it after a deploy or a fresh join so recommendations and genre data don't
    # sit empty. safe to re-run any time, every step is incremental and
    # idempotent. `python -m app.sync_service`, or inside the container
    # `docker compose exec app python -m app.sync_service`.
    logging.basicConfig(level=logging.INFO)
    log.info("running one maintenance pass on demand")
    _maintenance_pass()
    log.info("maintenance pass complete")
