"""POST /sync/{username}: the "join / catch me up" trigger. Creates the user
row if new, then hands off to sync_service, which pulls from Last.fm in the
background and blocks briefly so a loading screen has data to show."""

from fastapi import APIRouter

from app import db, sync_service
from app.queries import sync as sync_queries

router = APIRouter(prefix="/sync", tags=["sync"])


@router.post("/{username}")
def sync_user(username: str):
    # Look up the user (or create them on first join). Committed on block exit,
    # BEFORE the sync thread starts, so its FK to users(id) is valid.
    with db.get_connection() as conn, conn.cursor() as cur:
        row = sync_queries.get_user(cur, username)
        if row:
            user_id, last_synced_at = row
        else:
            user_id, last_synced_at = sync_queries.create_user(cur, username), None

    # Kick the sync (background thread) and wait up to the loading-screen budget.
    # force=True: pressing Load is an explicit "refresh me now", so it bypasses the
    # once-a-day freshness threshold and always pulls plays since the last sync.
    sync_service.ensure_fresh(user_id, username, last_synced_at, force=True)

    return {"username": username, "status": "syncing" if last_synced_at is None else "refreshing"}
