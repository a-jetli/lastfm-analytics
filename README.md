# Rotation

Rotation works by pulling your listening history from Last.fm's API, allowing you to dive deeper into your listening history, trends and binges than most streaming services allow. This requires you to sign up for last.fm and link your streaming services of choice, using a common API. 

It works by pulling your scrobble history into a common PostgreSQL database, allowing it to answer questions about your streaks, discovery rate, listening hours, when you most listen, what genres you listen to, comparing it to someone else, and even some recommendations for what to play next. 

FastAPI + Postgres backend, one static HTML page as the frontend, served by the
same app.

<!-- TODO: screenshot here -->

## Run it

Needs Docker and a Last.fm API key (free, from
https://www.last.fm/api/account/create).

```
cp .env.example .env        # put your LASTFM_API_KEY in it
docker compose up --build
```

Then open http://localhost:8000 and type in a Last.fm username. The API docs are
at /docs.

First load kicks off a background sync. Scrobbles show up as they land, then
genres and track lengths fill in after. Recommendations are computed by the
nightly pass, so they stay empty until it runs. To force one now:

```
docker compose exec app python -m app.sync_service
```

## Run it without Docker

Needs Postgres 18 running locally.

```
createdb lastfm && psql lastfm -f schema.sql
pip install -r requirements.txt
cp .env.example .env        # set LASTFM_API_KEY and DATABASE_URL
uvicorn app.main:app --reload
```

## What it does

Sync (`POST /sync/{user}`) pulls the full scrobble history in the background,
newest pages first, committing as it goes so the page has something to show
straight away. A second pass fills in track durations and artist genre tags.
Both are cached globally, so any given track or artist is only ever fetched
once. After that a thread wakes hourly to re-sync stale users, backfill anything
new, and rebuild recommendations.

Reads never wait on a sync. They return whatever is committed and let the
frontend poll `/sync/{user}/status` for progress.

Analytics endpoints, all under `/analytics/{user}`:

```
/streaks          longest and current listening streak
/discovery        new artists per month
/loyalty          how often you come back to an artist ("Heavy rotation" in the UI)
/clock            per-day x part-of-day grid, one cell per real date
/genre-clock      genre breakdown of a typical Friday night, Sunday morning, etc
/summary          month by month digest
/hours            listening hours, weekly or monthly
/report           plays over time
/binges           albums you hammered in a week
/song-binges      same for tracks
/tag-shift        how your genres moved over time
/genre            your tracks in one genre
/artist           one artist's plays, genres, top tracks
/scrobbles        searchable, paged play history
/recommendations  artists to try, plus tracks
/compatibility/{other}   taste overlap with another user
```

`/loyalty`, `/tag-shift`, `/binges` and `/song-binges` take `?days=` to limit the
window (0 = all time). Anything that buckets by date takes `?tz=`.

Scrobble search takes field terms: `artist:`, `track:`, `album:`, `year:`,
`month:`, `day:`, `date:`, `part:`, plus bare text. Quote values with spaces,
like `artist:"Tyler, the Creator"`. Anything it can't parse falls through to
free text instead of getting dropped.

## Recommendations

Each artist becomes a vector over genre tags
weighted by TF-IDF, so rare tags like shoegaze count for much more than
universal ones like rock. Your taste vector is the sum of the artists you play,
scaled by log(1 + play score), where play score decays on a 90 day half life so
recent listening drives the result. Everything you haven't played gets scored by
cosine similarity. Top 20 get cached.

Track recommendations skip the vector math, because tags describe artists and
not songs. They come from SQL over cached top tracks: popular songs by artists
you already love but have never played, and entry points into each recommended
artist.

Genre tags are user defined, so they can be misleading, messy and might get spammed.
Tags get stored raw and cleaned at read time in one view (`artist_tags_clean`): lowercase, map spelling
variants, drop junk like "seen live". The blocklist and alias tables are
hand curated, and editing them changes every result with no refetch.

## Layout

```
app/
  main.py            builds the app, mounts routers, starts the hourly loop
  db.py              Postgres connections
  lastfm.py          every outbound Last.fm call, nothing else touches it
  sync_service.py    when things run: staleness, locks, threads
  recommender.py     the vector math, no DB, no HTTP
  routers/           URLs and JSON, no SQL in here
  queries/           SQL, no HTTP in here
  static/index.html  the frontend
schema.sql           tables, indexes, views, curation seed rows
```

Dependencies point one way. Routers call queries and sync_service, queries only
touch a cursor, lastfm.py is the only thing talking to the outside.

## Tests

```
pytest                  # everything (needs Postgres)
pytest -m "not db"      # just the pure-function tests (needs nothing)
```

Most of the suite is SQL tests against a real Postgres, because that's where the
bugs have actually been: artist name casing, UTC vs local day boundaries, and
paged counts disagreeing with the rows they count.

They connect the same way `psql` does, so host, port and user come from `PGHOST`
/ `PGPORT` / `PGUSER` and libpq's defaults. If `psql` works in your shell,
`pytest` works. To point somewhere else, set `TEST_ADMIN_URL`:

```
TEST_ADMIN_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

The tests create and drop their own `lastfm_test` database and can never touch
`lastfm`, whatever they're pointed at. With no server reachable they fail rather
than skip, and tell you the three ways to fix it; `-m "not db"` is the supported
way to run without one.

Also covered: every heatmap cell's count equals the rows its drill-down filter
returns, re-running a sync inserts nothing twice, unknown users get a 404 rather
than a 500, and malformed search terms never reach a SQL cast.

CI runs the whole thing on every push against a Postgres service container.

## Notes

Times are stored in UTC, but every day, week and month bucket is the listener's,
not the server's. The browser sends its timezone and Postgres resolves DST per
play, because a play at 03:30 UTC belongs to the previous day in New York, and a
9pm play on the 31st belongs to that month and not the next one.

Roughly 14% of tracks come back from Last.fm with no duration, so listening
hours is a slight undercount.


Powered by AudioScrobbler. Artist, album and track names link back to Last.fm.
