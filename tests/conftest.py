"""Shared fixtures: a throwaway Postgres built from schema.sql, plus one user
with a hand-checked play history.

WHERE THE SERVER COMES FROM
Wherever `psql` in the same shell would find it: connections name only the
database, so host/port/user come from PGHOST/PGPORT/PGUSER and libpq's defaults.
TEST_ADMIN_URL overrides the lot, which is what CI uses for its container.

NO SERVER = FAILURE, NOT A SKIP
Skipping decides on your behalf that you didn't mean to run them, and the same
silence in CI leaves a green check on a run that never touched the SQL. Opting
out is one flag instead: `pytest -m "not db"` (the marker is applied
automatically below, from the fixture graph).

SAFETY
The database name is hardcoded, so whatever conninfo it is handed the only
database this suite can create or drop is `lastfm_test`.
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

from app.queries import sync as qsync

ADMIN_URL = os.getenv("TEST_ADMIN_URL")
TEST_DB = "lastfm_test"
SCHEMA = Path(__file__).resolve().parent.parent / "schema.sql"

# app/db.py reads DATABASE_URL at import time. conftest is imported first, so
# this is what lets test_api.py import the app at all; the client fixture
# overrides the value actually connected with.
os.environ.setdefault("DATABASE_URL", "postgresql:///unused-at-import-time")


def _conninfo(dbname: str) -> str:
    """TEST_ADMIN_URL with its database swapped, or a bare `dbname=` so libpq
    resolves the rest the way psql does."""
    if ADMIN_URL:
        return ADMIN_URL.rsplit("/", 1)[0] + "/" + dbname
    return f"dbname={dbname}"


def pytest_collection_modifyitems(config, items):
    """Tag anything reaching db_url with `db`. Derived from the fixture graph,
    not decorators: a marker you can forget to write is wrong within a month."""
    for item in items:
        if "db_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)

# tag_blocklist/tag_aliases/tag_exclusions are seeded by schema.sql and NOT
# truncated: the curation rules are part of the schema, not per-test state.
_DATA_TABLES = (
    "scrobbles, users, track_durations, artist_tags, artist_top_tracks, "
    "recommendations"
)


@pytest.fixture(scope="session")
def db_url():
    admin_conninfo = _conninfo("postgres")
    # autocommit: CREATE/DROP DATABASE cannot run inside a transaction block.
    try:
        admin = psycopg.connect(admin_conninfo, autocommit=True)
    except psycopg.OperationalError as exc:
        # pytrace=False: the traceback through psycopg's connection machinery
        # tells you nothing. What to do about it does.
        pytest.fail(
            f"No Postgres reachable at '{admin_conninfo}'.\n"
            f"  {str(exc).strip().splitlines()[0]}\n\n"
            f"These tests exercise the SQL layer, so they need a real server:\n"
            f"  start one          brew services start postgresql@18\n"
            f"  or point elsewhere TEST_ADMIN_URL=postgresql://user@host:5432/postgres\n"
            f"  or run without one pytest -m 'not db'",
            pytrace=False,
        )
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    url = _conninfo(TEST_DB)
    # The real schema.sql, not a copy: testing against a duplicate schema is
    # worse than not testing.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
    return url


@pytest.fixture
def conn(db_url):
    """A connection per test, test data wiped afterwards. No row_factory on
    purpose: sync.py reads positionally and analytics.py needs dict_row, so each
    test opens the cursor its target expects."""
    c = psycopg.connect(db_url)
    # Match the deployed container (Etc/UTC) rather than the developer's machine.
    c.execute("SET TIME ZONE 'UTC'")
    try:
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"TRUNCATE {_DATA_TABLES} RESTART IDENTITY CASCADE")
        c.commit()
        c.close()


# Anchored to now, not a fixed date: get_listening_clock filters on
# `now() - interval`, so old timestamps would fall outside its window and the
# clock tests would pass while asserting nothing.
BASE = (datetime.now(timezone.utc) - timedelta(days=10)).replace(
    hour=0, minute=0, second=0, microsecond=0
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """UTC timestamp `day` days after BASE. Used for both the fixture rows and
    the expected values, so the two cannot drift."""
    return BASE + timedelta(days=day, hours=hour, minutes=minute)


# (artist, track, album, day, hour, minute), expected values worked out by hand:
#   Radiohead  6 plays on days 0,1,2,5,6   boygenius  5 plays all on day 1
#   Nobody     1 play, under the 5-play loyalty cutoff
# Day 5's 03:30 UTC play is the timezone probe (23:30 the previous day in New
# York); the lowercase "radiohead" row is the casing regression.
CORPUS = [
    ("Radiohead", "Creep", "Pablo Honey", 0, 2, 0),
    ("Radiohead", "Idioteque", "Kid A", 1, 8, 0),
    ("Radiohead", "Reckoner", "In Rainbows", 2, 14, 0),
    ("Radiohead", "Nude", "In Rainbows", 5, 3, 30),
    ("Radiohead", "Bodysnatchers", "In Rainbows", 6, 20, 0),
    ("radiohead", "Let Down", "OK Computer", 6, 21, 0),
    ("boygenius", "Not Strong Enough", "The Record", 1, 18, 0),
    ("boygenius", "True Blue", "The Record", 1, 19, 0),
    ("boygenius", "Cool About It", "The Record", 1, 20, 0),
    ("boygenius", "Satanist", "The Record", 1, 21, 0),
    ("boygenius", "Anti-Curse", "The Record", 1, 22, 0),
    ("Nobody", "One Hit Wonder", None, 0, 12, 0),
]
CORPUS_PLAYS = len(CORPUS)


@pytest.fixture
def alice(conn):
    """User "alice" with CORPUS loaded, committed so the API tests' own
    connection can see it. The conn fixture truncates on teardown."""
    with conn.cursor() as cur:
        user_id = qsync.create_user(cur, "alice")
        for artist, track, album, day, hour, minute in CORPUS:
            qsync.insert_scrobble(
                cur,
                user_id,
                {
                    "artist": artist,
                    "name": track,
                    "album": album,
                    "uts": int(at(day, hour, minute).timestamp()),
                },
            )
    conn.commit()
    return user_id
