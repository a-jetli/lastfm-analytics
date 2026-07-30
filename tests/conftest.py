"""Shared fixtures: a throwaway Postgres built from schema.sql, plus one user
with a hand-checked play history.

The query layer is 800 lines of SQL and there is no way to test it without a real
server. Window functions, AT TIME ZONE, mode() WITHIN GROUP and the
gaps-and-islands streak trick have no stand-in, and every bug this project has
actually shipped was SQL semantics rather than Python.

WHERE THE SERVER COMES FROM

Wherever `psql` in the same shell would find it. Connections are opened with a
libpq conninfo that names only the database, so host, port and user come from
PGHOST / PGPORT / PGUSER and libpq's own defaults. That is the standard Postgres
convention that psql, pg_dump and every other tool in the ecosystem follows, so
there is no project-specific discovery rule to learn: if `psql` works here, the
tests work here. TEST_ADMIN_URL overrides the lot, which is what CI uses to point
at its service container.

HOW THEY BEHAVE WHEN THERE IS NO SERVER

They fail. They do not skip.

Skipping was the first design and it was wrong: it decided on your behalf that
you hadn't meant to run them, and the same silence in CI would leave a green
check on a run that never touched the SQL. A green check that means less than it
appears to is worse than a red one.

If you want to run without a database, say so and it is one flag. Every test that
needs one carries the `db` marker (applied automatically below, so nobody has to
remember it):

    pytest                  # everything, needs Postgres
    pytest -m "not db"      # the 17 pure-function tests only, needs nothing

That way opting out is a decision someone made and can be seen in the command,
rather than an accident of the environment.

SAFETY

The database name is hardcoded. Whatever conninfo it is handed, the only database
this suite can create or drop is `lastfm_test`; it cannot reach the real `lastfm`.
Dropped and rebuilt once per session.
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


def _conninfo(dbname: str) -> str:
    """Connection string for `dbname`.

    With TEST_ADMIN_URL set, swap its database component. Without it, name only
    the database and let libpq fill in host/port/user from PG* or its defaults --
    the same resolution `psql` does, rather than a rule invented here.
    """
    if ADMIN_URL:
        return ADMIN_URL.rsplit("/", 1)[0] + "/" + dbname
    return f"dbname={dbname}"


def pytest_collection_modifyitems(config, items):
    """Tag every test that needs Postgres with `db`, so `-m "not db"` is a
    complete and always-accurate way to exclude them.

    Derived from the fixture graph rather than hand-applied decorators:
    `fixturenames` is transitive, so anything reaching db_url through conn, cur,
    alice or client is caught. A marker you can forget to write is a marker that
    is wrong within a month.
    """
    for item in items:
        if "db_url" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.db)

# app/db.py reads DATABASE_URL at import time and raises KeyError without it.
# conftest is imported before any test module, so setting it here is what lets
# test_api.py import the app at all. The client fixture overrides the value it
# actually connects with; this only has to exist. setdefault, so an exported
# DATABASE_URL is left alone.
os.environ.setdefault("DATABASE_URL", "postgresql:///unused-at-import-time")

# Tables holding test data. tag_blocklist and tag_aliases are seeded by
# schema.sql and deliberately NOT truncated: the cleanup rules are part of the
# schema, not per-test state.
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
    # Loading the real schema.sql, not a copy: a test suite running against a
    # hand-maintained duplicate schema is worse than no test suite.
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
    return url


@pytest.fixture
def conn(db_url):
    """A connection per test, with all test data wiped afterwards.

    No row_factory on purpose, matching production: queries/sync.py reads rows
    positionally and queries/analytics.py needs dict_row, so each test opens the
    cursor its target expects rather than the connection picking one for both.
    """
    c = psycopg.connect(db_url)
    # Pin the session timezone to UTC, because several queries bucket with a
    # bare `listened_at::date` and that cast resolves in the SESSION timezone,
    # not UTC. The deployed Postgres container runs Etc/UTC, so this makes the
    # tests reproduce production instead of whatever the developer's machine is
    # set to. See test_streaks_bucket_in_the_server_timezone.
    c.execute("SET TIME ZONE 'UTC'")
    try:
        yield c
    finally:
        c.rollback()
        with c.cursor() as cur:
            cur.execute(f"TRUNCATE {_DATA_TABLES} RESTART IDENTITY CASCADE")
        c.commit()
        c.close()


# Anchored to a real recent date, not a hardcoded 2023 one, because
# get_listening_clock filters on `now() - interval`: fixed old timestamps would
# fall outside its window and the clock tests would pass while asserting nothing.
BASE = (datetime.now(timezone.utc) - timedelta(days=10)).replace(
    hour=0, minute=0, second=0, microsecond=0
)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC timestamp `day` days after BASE. Tests use this for both the
    fixture rows and their expected values, so the two cannot drift."""
    return BASE + timedelta(days=day, hours=hour, minutes=minute)


# (artist, track, album, day, hour, minute), worked out by hand:
#   Radiohead  6 plays on days 0,1,2,5,6 -> 5 active days over a 7 day span
#   boygenius  5 plays all on day 1      -> 1 active day, 1 day span
#   Nobody     1 play                    -> under the loyalty cutoff of 5
# Day 5's 03:30 UTC play is the timezone probe: it is 23:30 the PREVIOUS day in
# New York. The lowercase "radiohead" row is the casing-bug regression; it must
# fold into Radiohead everywhere instead of reading as a new artist.
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
    """User "alice" with CORPUS loaded, committed. Returns her user id.

    Committed rather than left in the transaction because the API tests drive
    the app through TestClient, and the app opens its own connection which
    cannot see uncommitted rows. The conn fixture truncates on teardown.
    """
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
