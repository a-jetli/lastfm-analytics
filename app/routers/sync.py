"""POST /sync/{username}: the "join / catch me up" trigger. Validates and (on
first join) creates the user, then hands off to sync_service, which pulls from
Last.fm in the background and blocks briefly so a loading screen has data."""

from fastapi import APIRouter, HTTPException

from app import db, lastfm, sync_service
from app.queries import sync as sync_queries

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/{username}")
def sync_user(username: str):
    # join() validates a new handle against Last.fm before creating any row, so a
    # typo returns a clean 404 instead of a phantom user + endless spinner.
    # force=True: pressing Load is an explicit "refresh me now".
    try:
        _, is_new = sync_service.join(username, force=True, wait=True)
    except lastfm.LastfmUserNotFound:
        raise HTTPException(status_code=404, detail=f"No Last.fm user named '{username}'.")
    return {"username": username, "status": "syncing" if is_new else "refreshing"}


@router.get("/{username}/status")
def sync_status(username: str):
    """Is a sync still running, in which stage, and how much has landed?

    The page polls this while the background pull continues, so a first-time
    user sees a rising play count and then "adding genres" instead of a frozen
    spinner. Cheap: one indexed COUNT plus in-memory checks, no Last.fm call.
    `phase` is "pulling" (scrobbles) or "enriching" (durations + tags), or null
    when idle. `last_synced_at` is null until a full pull has completed once, so
    the page can tell "still going / didn't finish" from "done". 404 for an
    unknown user, matching the analytics routes.
    """
    with db.get_connection() as conn, conn.cursor() as cur:
        row = sync_queries.get_user(cur, username)
        if row is None:
            raise HTTPException(status_code=404, detail=f"{username} not joined yet")
        user_id, last_synced_at = row
        cur.execute("SELECT COUNT(*) FROM scrobbles WHERE user_id = %s", (user_id,))
        scrobbles = cur.fetchone()[0]
    return {
        "username": username,
        "syncing": sync_service.is_syncing(user_id),
        "phase": sync_service.sync_phase(user_id),
        "scrobbles": scrobbles,
        "last_synced_at": last_synced_at,
    }
