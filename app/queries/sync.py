def get_user(cur, username: str):
    """Returns (user_id, last_synced_at) for an existing user, or None if new."""
    cur.execute(
        "SELECT id, last_synced_at FROM users WHERE lastfm_username = %s", (username,)
    )
    return cur.fetchone()


def create_user(cur, username: str) -> int:
    cur.execute(
        "INSERT INTO users (lastfm_username) VALUES (%s) RETURNING id",
        (username,),
    )
    return cur.fetchone()[0]


def insert_scrobble(cur, user_id: int, track: dict) -> None:
    # ON CONFLICT DO NOTHING relies on the UNIQUE(user_id, track_name, listened_at)
    # constraint in schema.sql -- duplicate scrobbles are silently skipped, not errors.
    cur.execute(
        """
        INSERT INTO scrobbles (user_id, artist_name, track_name, album_name, listened_at)
        VALUES (%s, %s, %s, %s, to_timestamp(%s))
        ON CONFLICT (user_id, track_name, listened_at) DO NOTHING
        """,
        (user_id, track["artist"], track["name"], track["album"], int(track["uts"])),
    )


def update_last_synced(cur, user_id: int, synced_at) -> None:
    # Stamped with the sync's START time, not now(): pages are fetched newest-
    # first, so a play scrobbled mid-sync sits on a page we already read. A
    # start-time mark re-fetches that overlap next sync (ON CONFLICT dedups);
    # an end-time mark would skip those plays forever.
    cur.execute(
        "UPDATE users SET last_synced_at = %s WHERE id = %s", (synced_at, user_id)
    )


def get_tracks_missing_durations(cur, user_id: int | None = None):
    """Work list for the duration backfill: (artist, track) pairs in scrobbles
    but not yet in track_durations. NOT EXISTS makes it incremental. Pass
    user_id to limit to one user's tracks; omit for the global sweep."""
    if user_id is None:
        cur.execute(
            """
            SELECT DISTINCT s.artist_name, s.track_name
            FROM scrobbles s
            WHERE NOT EXISTS (
                SELECT 1 FROM track_durations d
                WHERE d.artist_name = s.artist_name
                  AND d.track_name  = s.track_name
            )
            """
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT s.artist_name, s.track_name
            FROM scrobbles s
            WHERE s.user_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM track_durations d
                WHERE d.artist_name = s.artist_name
                  AND d.track_name  = s.track_name
            )
            """,
            (user_id,),
        )
    return cur.fetchall()


def insert_track_duration(cur, artist_name: str, track_name: str, duration_ms: int) -> None:
    # ON CONFLICT DO NOTHING: two overlapping passes can't double-insert, and a
    # re-run is harmless (durations don't change).
    cur.execute(
        """
        INSERT INTO track_durations (artist_name, track_name, duration_ms)
        VALUES (%s, %s, %s)
        ON CONFLICT (artist_name, track_name) DO NOTHING
        """,
        (artist_name, track_name, duration_ms),
    )


def get_artists_missing_tags(cur, user_id: int | None = None):
    """Work list for the tag backfill: artists in scrobbles with no rows yet
    in artist_tags. Same shape and user_id semantics as durations above."""
    if user_id is None:
        cur.execute(
            """
            SELECT DISTINCT s.artist_name
            FROM scrobbles s
            WHERE NOT EXISTS (
                SELECT 1 FROM artist_tags a WHERE a.artist_name = s.artist_name
            )
            """
        )
    else:
        cur.execute(
            """
            SELECT DISTINCT s.artist_name
            FROM scrobbles s
            WHERE s.user_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM artist_tags a WHERE a.artist_name = s.artist_name
            )
            """,
            (user_id,),
        )
    return cur.fetchall()


def insert_artist_tag(cur, artist_name: str, tag: str, weight: int) -> None:
    # ON CONFLICT DO NOTHING: idempotent across overlapping/re-run passes.
    cur.execute(
        """
        INSERT INTO artist_tags (artist_name, tag, weight)
        VALUES (%s, %s, %s)
        ON CONFLICT (artist_name, tag) DO NOTHING
        """,
        (artist_name, tag, weight),
    )
