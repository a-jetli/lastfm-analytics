"""HTTP-layer tests: status codes and hostile query strings.

Not data correctness, which test_queries.py covers. What this catches is the
router contract: an unknown user is a 404 and not a 500, and junk in a query
parameter degrades instead of reaching Postgres and blowing up.

Sync is stubbed out. Every read calls ensure_fresh, and app startup starts the
scheduler thread; both talk to Last.fm, and a test suite that made real API
calls would be slow, flaky and rate-limited against a single shared key.
"""

import pytest

# TestClient is backed by httpx, which is a test-only tool and deliberately not
# in requirements.txt. Skip rather than error if it isn't installed.
pytest.importorskip("httpx", reason="fastapi's TestClient needs httpx")
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client(db_url, alice, monkeypatch):
    from app import db, sync_service

    # get_connection() reads this module global per call, so pointing it at the
    # throwaway database is enough; no need for the app to have imported with it.
    monkeypatch.setattr(db, "DATABASE_URL", db_url)
    monkeypatch.setattr(sync_service, "start_scheduler", lambda: None)
    monkeypatch.setattr(sync_service, "ensure_fresh", lambda *a, **k: None)

    from app.main import app

    with TestClient(app) as c:
        yield c


ENDPOINTS = [
    "streaks",
    "discovery",
    "loyalty",
    "clock",
    "summary",
    "hours",
    "report",
    "binges",
    "tag-shift",
    "recommendations",
]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_known_user_returns_200(client, endpoint):
    assert client.get(f"/analytics/alice/{endpoint}").status_code == 200


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_unknown_user_is_404_not_500(client, endpoint):
    r = client.get(f"/analytics/definitely-not-a-user/{endpoint}")
    assert r.status_code == 404
    assert "not joined" in r.json()["detail"]


def test_health_does_not_touch_the_database(client, monkeypatch):
    """/health is liveness only, on purpose. It has to stay green while Postgres
    is unreachable, or a load balancer will kill a perfectly healthy app over a
    DB blip. The compose healthcheck depends on this."""
    from app import db

    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://nope@127.0.0.1:1/nope")
    assert client.get("/health").status_code == 200


# --- hostile query parameters ------------------------------------------------


# Every endpoint that buckets by day, week or month takes ?tz=. Junk has to
# degrade on all of them, not just the clock: Postgres raises on an unknown zone
# name, which would turn a bad ?tz= into a 500. _tz validates with zoneinfo.
TZ_AWARE = ["clock", "streaks", "discovery", "loyalty", "summary", "hours", "report", "tag-shift"]


@pytest.mark.parametrize("endpoint", TZ_AWARE)
@pytest.mark.parametrize("tz", ["Mars/Phobos", "", "'; DROP TABLE users --", "UTC+9"])
def test_junk_timezone_falls_back_to_utc(client, endpoint, tz):
    url = f"/analytics/alice/{endpoint}"
    junk = client.get(url, params={"tz": tz})
    assert junk.status_code == 200
    assert junk.json() == client.get(url, params={"tz": "UTC"}).json()


@pytest.mark.parametrize("endpoint", TZ_AWARE)
def test_a_real_timezone_is_accepted(client, endpoint):
    # Guards the plumbing: a missing tz parameter on the query function would be
    # a TypeError and a 500, which a UTC-only test would never notice.
    r = client.get(f"/analytics/alice/{endpoint}", params={"tz": "America/New_York"})
    assert r.status_code == 200


def test_scrobbles_limit_is_clamped(client):
    body = client.get("/analytics/alice/scrobbles", params={"limit": 9999}).json()
    assert body["limit"] == 200


def test_negative_offset_is_clamped(client):
    body = client.get("/analytics/alice/scrobbles", params={"offset": -5}).json()
    assert body["offset"] == 0


@pytest.mark.parametrize(
    "search",
    [
        "date:2026-02-31",  # impossible date: must not reach the ::date cast
        "date:july",
        "month:13",
        "year:abc",
        "artist:Guns N' Roses",  # shlex raises on this; the regex tokenizer must not
        "foo:bar",
        "'; DROP TABLE scrobbles; --",
        "%",  # bare LIKE wildcard
    ],
)
def test_malformed_search_does_not_500(client, search):
    r = client.get("/analytics/alice/scrobbles", params={"search": search})
    assert r.status_code == 200


def test_scrobbles_total_and_rows_agree_over_http(client):
    body = client.get("/analytics/alice/scrobbles", params={"limit": 200}).json()
    assert body["total"] == len(body["rows"]) == 12


def test_paging_reports_a_stable_total(client):
    # The total counts every match, not the page, so it must not change as you
    # page. That number is what disables Next.
    first = client.get("/analytics/alice/scrobbles", params={"limit": 5}).json()
    second = client.get("/analytics/alice/scrobbles", params={"limit": 5, "offset": 5}).json()
    assert first["total"] == second["total"] == 12
    assert len(second["rows"]) == 5


def test_artist_detail_requires_its_parameter(client):
    # ?name= has no default, so FastAPI should reject the request outright.
    assert client.get("/analytics/alice/artist").status_code == 422


def test_unknown_artist_detail_is_not_an_error(client):
    r = client.get("/analytics/alice/artist", params={"name": "Nobody At All"})
    assert r.status_code in (200, 404)
