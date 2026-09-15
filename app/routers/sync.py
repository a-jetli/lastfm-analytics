"""POST /sync/{username}: the join / catch-me-up trigger. validates the handle,
creates the user on first sight, then hands off to sync_service, which pulls in
the background and blocks briefly so the page has something to show."""

from fastapi import APIRouter, HTTPException

from app import db, lastfm, sync_service
from app.queries import sync as sync_queries

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/{username}")
def sync_user(username: str):
    # join() checks a new handle against Last.fm before writing a row, so a typo
    # gets a 404 instead of a phantom user. force=True: Load means refresh now.
    try:
        _, is_new = sync_service.join(username, force=True, wait=True)
    except lastfm.LastfmUserNotFound:
        raise HTTPException(status_code=404, detail=f"No Last.fm user named '{username}'.")
    return {"username": username, "status": "syncing" if is_new else "refreshing"}


@router.get("/{username}/status")
def sync_status(username: str):
    """is a sync running, in which stage, and how much has landed?

    polled while the background pull runs, so the page shows a rising count and
    then "adding genres" instead of a frozen spinner. one indexed COUNT plus
    in-memory checks, no Last.fm call. phase is "pulling" or "enriching", null
    when idle. last_synced_at stays null until a full pull finishes once, which
    is how the page tells "still going" from "done".
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
