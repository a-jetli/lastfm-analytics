"""SQL-layer tests against a real Postgres (see conftest for how to run them).

These cover the three things that have actually broken in this project: artist
name casing, the UTC vs local day boundary, and counts that disagree with the
rows they claim to count. Expected values are worked out by hand from
conftest.CORPUS, never by running the query and pasting what it said.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from psycopg.rows import dict_row

from app.queries import analytics as q
from app.queries import sync as qsync
from tests.conftest import CORPUS_PLAYS, at


@pytest.fixture
def cur(conn):
    """dict_row cursor: what routers/analytics.py hands the query layer."""
    with conn.cursor(row_factory=dict_row) as c:
        yield c


# --- streaks -----------------------------------------------------------------


def test_streaks_finds_consecutive_runs(cur, alice):
    # UTC play days are 0,1,2 then a gap at 3, then 5,6. So: a 3 day run and a
    # 2 day run, longest first.
    rows = q.get_streaks(cur, alice, "UTC")
    assert [r["length_days"] for r in rows] == [3, 2]
    assert rows[0]["start_day"] == at(0, 0).date()
    assert rows[0]["end_day"] == at(2, 0).date()


def test_streaks_ignore_the_server_timezone(conn, cur, alice):
    """The regression guard for the bug these tests originally caught.

    Every date bucket in this file used to be a bare `listened_at::date` or
    date_trunc, both of which resolve in the SESSION timezone. So the answer
    depended on where the server ran, and a 9pm Monday play plus a 9am Wednesday
    play reported a 2-day streak that never happened. They go through
    _LOCAL_DATE now, so the explicit tz argument is the only thing that decides.
    """
    conn.execute("SET TIME ZONE 'Pacific/Kiritimati'")  # UTC+14
    try:
        shifted_server = [r["length_days"] for r in q.get_streaks(cur, alice, "UTC")]
    finally:
        conn.execute("SET TIME ZONE 'UTC'")
    assert shifted_server == [3, 2]


def test_streaks_follow_the_requested_timezone(cur, alice):
    # Day 5's play is 03:30 UTC, so in New York it belongs to day 4. That turns
    # the UTC [5,6] pair into [4] and [6], two separate single days.
    assert [r["length_days"] for r in q.get_streaks(cur, alice, "UTC")] == [3, 2]
    # NY days are -1,0,1,2,4,6 (three plays cross backwards over midnight), so
    # the runs become a 4 day one plus two isolated days.
    ny = q.get_streaks(cur, alice, "America/New_York")
    assert [r["length_days"] for r in ny] == [4, 1, 1]


# --- casing (the bug that made lowercase re-scrobbles look like discoveries) --


def test_discovery_folds_artist_casing(cur, alice):
    # Three artists: Radiohead (both casings are ONE artist), boygenius, Nobody.
    rows = q.get_discovery(cur, alice, "UTC")
    assert sum(r["new_artists"] for r in rows) == 3


def test_loyalty_folds_casing_and_shows_the_common_spelling(cur, alice):
    rows = {r["artist_name"]: r for r in q.get_loyalty(cur, alice, "UTC")}
    assert "radiohead" not in rows  # not a separate row
    assert rows["Radiohead"]["plays"] == 6  # 5 + the lowercase one
    assert rows["Radiohead"]["active_days"] == 5


def test_loyalty_excludes_artists_under_five_plays(cur, alice):
    # HAVING COUNT(*) >= 5. "Nobody" has one play.
    assert "Nobody" not in {r["artist_name"] for r in q.get_loyalty(cur, alice, "UTC")}


def test_loyalty_ranks_a_steady_favourite_above_a_one_day_binge(cur, alice):
    """The metric fix. The denominator runs from an artist's first play to the
    user's LAST play overall, so a burst you never returned to keeps decaying.

    Radiohead: 5 active days, first play day 0, user's last play day 6
               -> 5 / 7 = 0.71
    boygenius: 5 plays all on day 1, so 1 active day
               -> 1 / 6 = 0.17
    Against each artist's OWN span (the old denominator) boygenius scored 1.00
    and came first, which is what made the number meaningless.
    """
    rows = q.get_loyalty(cur, alice, "UTC")
    by_name = {r["artist_name"]: r for r in rows}
    assert float(by_name["Radiohead"]["loyalty"]) == pytest.approx(0.71)
    assert float(by_name["boygenius"]["loyalty"]) == pytest.approx(0.17)
    assert [r["artist_name"] for r in rows] == ["Radiohead", "boygenius"]


def test_loyalty_window_is_measured_to_the_users_last_play(cur, alice):
    # days_since_first_play, not the artist's own span: boygenius only ever
    # played on day 1, but the window still runs to the user's day 6.
    rows = {r["artist_name"]: r for r in q.get_loyalty(cur, alice, "UTC")}
    assert rows["Radiohead"]["days_since_first_play"] == 7  # day 0 through day 6
    assert rows["boygenius"]["days_since_first_play"] == 6  # day 1 through day 6


# --- month boundaries --------------------------------------------------------


def _one_play_at_a_month_boundary(conn) -> int:
    """One play at 2025-08-01 01:00 UTC, which is 2025-07-31 21:00 in New York.
    Tagged, so the genre-aware queries have something to join against."""
    with conn.cursor() as c:
        user_id = qsync.create_user(c, "bob")
        qsync.insert_scrobble(
            c,
            user_id,
            {
                "artist": "A",
                "name": "T",
                "album": None,
                "uts": int(datetime(2025, 8, 1, 1, 0, tzinfo=timezone.utc).timestamp()),
            },
        )
        qsync.insert_artist_tag(c, "A", "rock", 100)
    conn.commit()
    return user_id


@pytest.mark.parametrize(
    "call,key",
    [
        (lambda cur, uid, tz: q.get_monthly_report(cur, uid, "month", tz), "month"),
        (lambda cur, uid, tz: q.get_listening_time(cur, uid, "month", tz), "month"),
        (lambda cur, uid, tz: q.get_monthly_summary(cur, uid, tz), "month"),
        (lambda cur, uid, tz: q.get_discovery(cur, uid, tz), "month"),
        (lambda cur, uid, tz: q.get_tag_shift(cur, uid, "month", tz), "period_start"),
    ],
)
def test_month_buckets_follow_the_requested_timezone(conn, cur, call, key):
    """date_trunc on a timestamptz resolves in the session timezone too, so a
    9pm July 31st play in New York used to be counted in August. Every monthly
    bucket goes through _LOCAL_MONTH / _LOCAL_PERIOD now."""
    user_id = _one_play_at_a_month_boundary(conn)
    assert call(cur, user_id, "UTC")[0][key] == date(2025, 8, 1)
    assert call(cur, user_id, "America/New_York")[0][key] == date(2025, 7, 1)


# --- the clock invariant -----------------------------------------------------
# Every heatmap cell is clickable and drills into a date:/part: search. If the
# clock's bucketing and the search filter's bucketing ever drift, a cell shows
# one number and returns a different set of rows. They share _LOCAL_DATE and
# _PART_OF_DAY to stop that, and these tests are what keeps it true.


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_every_clock_cell_equals_the_rows_its_filter_returns(cur, alice, tz):
    cells = q.get_listening_clock(cur, alice, tz)
    assert cells, "fixture data fell outside the clock's time window"
    assert sum(c["plays"] for c in cells) == CORPUS_PLAYS
    for cell in cells:
        search = f'date:{cell["day"].isoformat()} part:{q.PART_NAMES[cell["part"]]}'
        assert q.count_scrobbles(cur, alice, search, tz=tz) == cell["plays"], search


def test_clock_buckets_by_local_day_not_utc_day(cur, alice):
    # Day 5's play is 03:30 UTC, which is 23:30 on day 4 in New York. It must
    # move one column left AND from night into evening.
    day5, day4 = at(5, 0).date(), at(5, 0).date() - timedelta(days=1)
    utc = {(c["day"], c["part"]): c["plays"] for c in q.get_listening_clock(cur, alice, "UTC")}
    ny = {(c["day"], c["part"]): c["plays"] for c in q.get_listening_clock(cur, alice, "America/New_York")}

    assert utc[(day5, 0)] == 1  # night of day 5 in UTC
    assert (day5, 0) not in ny  # nothing there in New York
    assert ny[(day4, 3)] == 1  # evening of day 4 instead


# --- paged table: the total can never disagree with the rows ------------------


@pytest.mark.parametrize(
    "search",
    [
        None,
        "artist:radiohead",
        "boygenius",
        'album:"In Rainbows"',
        "year:1999",
        "date:2026-02-31",  # impossible date, falls through to free text
        "month:13",  # out of range, same
        "foo:bar",  # unknown field, same
        "no such thing",
    ],
)
def test_scrobble_total_always_matches_the_page(cur, alice, search):
    # limit above the corpus size, so total and len(rows) must be equal for the
    # shared WHERE clause to be doing its job.
    rows = q.get_scrobbles(cur, alice, search, limit=200, offset=0)
    assert q.count_scrobbles(cur, alice, search) == len(rows)


def test_artist_search_is_case_insensitive(cur, alice):
    # ILIKE, and both casings belong to the same artist.
    assert q.count_scrobbles(cur, alice, "artist:RADIOHEAD") == 6


def test_offset_pages_without_repeating_rows(cur, alice):
    first = q.get_scrobbles(cur, alice, None, limit=5, offset=0)
    second = q.get_scrobbles(cur, alice, None, limit=5, offset=5)
    assert len(first) == len(second) == 5
    assert not {r["track_name"] for r in first} & {r["track_name"] for r in second}


def test_unknown_sort_column_falls_back_instead_of_raising(cur, alice):
    # sort is whitelisted, so a junk value must not reach the ORDER BY.
    rows = q.get_scrobbles(cur, alice, None, limit=5, offset=0, sort="; DROP TABLE users")
    assert len(rows) == 5


# --- regression guards for the three bugs found in the 2026-07-30 sweep -------
# All three were invisible from the UI with one account, which is exactly the
# kind of thing tests are for.


def test_comparing_a_user_with_themselves_scores_100(conn, cur, alice):
    """Was 0% next to a full list of shared artists, because the row-splitting
    loop put everything in one side and cosine against {} is 0. Typing your own
    handle into the compare box is the first thing anyone tries."""
    # Compatibility is a genre-vector comparison, so it needs tagged artists.
    with conn.cursor() as c:
        qsync.insert_artist_tag(c, "Radiohead", "alternative rock", 100)
        qsync.insert_artist_tag(c, "boygenius", "indie rock", 100)
    conn.commit()
    with_self = q.get_compatibility(cur, alice, alice)
    assert with_self["score"] == 100.0
    assert with_self["shared_artist_count"] > 0


# The recommender path runs on a plain tuple cursor in production
# (_refresh_recommendations uses conn.cursor()), so these use one too rather
# than the dict_row cursor the analytics layer needs.


def test_recommendations_exclude_played_artists_across_casing(conn, alice):
    """The exclusion set came from scrobbles and the candidate list from
    artist_tags, each keyed on its own raw spelling. A user with 22 plays of
    "Charli xcx" was recommended "Charli XCX"."""
    from app import recommender
    from app.queries import recommend as qrec

    with conn.cursor() as c:
        # alice plays Radiohead/radiohead, boygenius and Nobody. Tag Radiohead
        # under a THIRD casing so the corpus and the play list disagree.
        # Two tags over four artists so idf is non-zero: with a single shared
        # tag every vector is empty, nothing is recommended, and this test would
        # pass no matter what the exclusion does.
        qsync.insert_artist_tag(c, "RADIOHEAD", "shoegaze", 100)
        qsync.insert_artist_tag(c, "boygenius", "shoegaze", 100)
        qsync.insert_artist_tag(c, "Nobody", "jazz", 100)
        qsync.insert_artist_tag(c, "Some Stranger", "jazz", 100)
        conn.commit()

        corpus: dict[str, dict[str, float]] = {}
        for artist, tag, weight in qrec.get_tag_corpus(c):
            corpus.setdefault(artist, {})[tag] = float(weight)
        plays = dict(qrec.get_user_plays(c, alice))

    vectors = recommender.build_artist_vectors(corpus, recommender.compute_idf(corpus))
    ranked = recommender.recommend(
        recommender.build_user_vector(plays, vectors), vectors, set(plays)
    )
    assert ranked, "nothing scored; the assertion below would be vacuous"
    assert "radiohead" not in {a.lower() for a, _ in ranked}


def test_tag_corpus_gives_one_vector_per_artist(conn, alice):
    """Two casings meant two vectors, so the same act could win two slots and
    appear twice in one recommendation list."""
    from app.queries import recommend as qrec

    with conn.cursor() as c:
        qsync.insert_artist_tag(c, "Radiohead", "alternative rock", 100)
        qsync.insert_artist_tag(c, "radiohead", "alternative rock", 90)
        conn.commit()
        artists = [artist for artist, _, _ in qrec.get_tag_corpus(c)]
    assert artists, "corpus came back empty; the test would pass vacuously"
    assert len(artists) == len({a.lower() for a in artists})


# --- the tag exclusion rule ---------------------------------------------------


def test_exclusion_view_stays_cheap(conn, cur, alice):
    """The first version of this rule used a correlated NOT EXISTS that
    re-scanned the `allowed` CTE once per row: 10ms became 1.3s on a 6k-row
    corpus, and every genre query reads this view.

    Asserts the suppression is planned as an anti-join, i.e. the excluded set is
    built once and hashed. Note this deliberately does NOT assert "no SubPlan
    anywhere": the blocklist NOT IN is a hashed subplan, evaluated once, and has
    always been there.
    """
    with conn.cursor() as c:
        qsync.insert_artist_tag(c, "Arijit Singh", "bollywood", 100)
        qsync.insert_artist_tag(c, "Arijit Singh", "pop", 90)
        c.execute("EXPLAIN SELECT count(*) FROM artist_tags_clean")
        plan = "\n".join(row[0] for row in c.fetchall())
    assert "Anti Join" in plan, f"suppression is no longer an anti-join:\n{plan}"


def test_context_tags_suppress_pop_and_hiphop(conn, cur, alice):
    """"pop" on a Bollywood playback singer is crowd shorthand for "popular",
    not the Western genre, and it swamped the real tag. The blocklist can't
    express this because these tags are only wrong in combination."""
    with conn.cursor() as c:
        qsync.insert_artist_tag(c, "Arijit Singh", "bollywood", 100)
        qsync.insert_artist_tag(c, "Arijit Singh", "pop", 90)
        qsync.insert_artist_tag(c, "Arijit Singh", "hip hop", 80)  # aliases to hip-hop
        qsync.insert_artist_tag(c, "Arijit Singh", "hindi", 70)
        # A control: no context tag, so pop survives untouched.
        qsync.insert_artist_tag(c, "Carly Rae Jepsen", "pop", 100)
    conn.commit()

    cur.execute(
        "SELECT tag FROM artist_tags_clean WHERE artist_name = 'Arijit Singh'"
    )
    tags = {r["tag"] for r in cur.fetchall()}
    assert tags == {"bollywood", "hindi"}, tags
    cur.execute("SELECT tag FROM artist_tags_clean WHERE artist_name = 'Carly Rae Jepsen'")
    assert {r["tag"] for r in cur.fetchall()} == {"pop"}


# --- the range picker ---------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda cur, uid, days: q.get_tag_shift(cur, uid, "month", "UTC", days),
        lambda cur, uid, days: q.get_binges(cur, uid, 1, days),
        lambda cur, uid, days: q.get_song_binges(cur, uid, 1, days),
        lambda cur, uid, days: q.get_loyalty(cur, uid, "UTC", days),
    ],
)
def test_range_picker_narrows_and_never_widens(conn, cur, alice, call):
    """A wider window can never return fewer rows than a narrower one. Cheap
    invariant that catches a params list spliced in the wrong order, which is
    the realistic way these three break."""
    with conn.cursor() as c:
        qsync.insert_artist_tag(c, "Radiohead", "alternative rock", 100)
        qsync.insert_artist_tag(c, "boygenius", "indie rock", 100)
    conn.commit()
    # Fixture plays span days 0-6 starting 10 days ago, so a 3 day window is
    # empty, 30 days holds all of them, and all-time must match 30 days.
    counts = [len(call(cur, alice, d)) for d in (3, 30, None)]
    assert counts[0] == 0
    assert counts[1] == counts[2] > 0


# --- idempotency -------------------------------------------------------------
# The sync high-water mark is the moment a sync STARTED, so every sync re-offers
# rows it already inserted. ON CONFLICT DO NOTHING is the only thing making that
# safe, and nothing verified it. Testing the query layer rather than
# sync_service.join is deliberate: the guarantee lives in the SQL, and a test
# that reached Last.fm would be slow and rate-limited.


def _count(conn, table: str) -> int:
    with conn.cursor() as c:
        c.execute(f"SELECT count(*) FROM {table}")
        return c.fetchone()[0]


def test_reinserting_the_same_scrobble_changes_nothing(conn, alice):
    replay = {
        "artist": "Radiohead",
        "name": "Creep",
        "album": "Pablo Honey",
        "uts": int(at(0, 2).timestamp()),
    }
    with conn.cursor() as c:
        for _ in range(3):
            qsync.insert_scrobble(c, alice, replay)
    assert _count(conn, "scrobbles") == CORPUS_PLAYS


def test_a_genuine_replay_at_a_new_time_does_insert(conn, alice):
    # Positive control: proves the test above is catching the conflict clause
    # and not just a broken insert.
    with conn.cursor() as c:
        qsync.insert_scrobble(
            c,
            alice,
            {
                "artist": "Radiohead",
                "name": "Creep",
                "album": "Pablo Honey",
                "uts": int(at(7, 9).timestamp()),
            },
        )
    assert _count(conn, "scrobbles") == CORPUS_PLAYS + 1


def test_reinserting_enrichment_rows_changes_nothing(conn, alice):
    with conn.cursor() as c:
        for _ in range(2):
            qsync.insert_track_duration(c, "Radiohead", "Creep", 238000)
            qsync.insert_artist_tag(c, "Radiohead", "alternative rock", 100)
    assert _count(conn, "track_durations") == 1
    assert _count(conn, "artist_tags") == 1

    # A second pass must not overwrite either, so the first value stands.
    with conn.cursor() as c:
        qsync.insert_track_duration(c, "Radiohead", "Creep", 999)
        c.execute("SELECT duration_ms FROM track_durations")
        assert c.fetchone()[0] == 238000


def test_backfill_work_lists_empty_out_as_they_are_filled(conn, alice):
    """The incremental guarantee: a NOT EXISTS work list must shrink as rows
    land, or the nightly pass re-fetches the same tracks from Last.fm forever."""
    with conn.cursor() as c:
        missing = qsync.get_tracks_missing_durations(c, alice)
        assert len(missing) == CORPUS_PLAYS  # every track is unknown at first
        for artist, track in missing:
            qsync.insert_track_duration(c, artist, track, 200000)
        assert qsync.get_tracks_missing_durations(c, alice) == []

        artists = qsync.get_artists_missing_tags(c, alice)
        assert {a for (a,) in artists} == {"Radiohead", "radiohead", "boygenius", "Nobody"}
        for (artist,) in artists:
            qsync.insert_artist_tag(c, artist, "rock", 100)
        assert qsync.get_artists_missing_tags(c, alice) == []
