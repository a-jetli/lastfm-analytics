# Rotation Architecture

This document explains the complete architecture of Rotation, a Last.fm listening-history analytics and recommendation application. It is written as both a system reference and a teaching guide: first understand the end-to-end flow, then study each boundary, invariant, query family, failure mode, and tradeoff.

The implementation is the authority for every behavior described here. Statements about external infrastructure describe the deployment shape encoded by this repository or required by that shape. Runtime state outside the repository, such as the contents of a production database, is not assumed.

## 1. The system in one page

Rotation turns a public Last.fm username into a private-to-the-deployment analytical copy of that account's listening history. It incrementally imports scrobbles, enriches them with duration and genre metadata, calculates SQL-based behavioral insights, precomputes content-based recommendations, and renders everything in a single browser application.

The core design is a small modular monolith:

```text
Browser
  |
  | same-origin HTTP and JSON
  v
FastAPI application
  |-- sync router --------> sync service -------> Last.fm API
  |                              |
  |-- analytics router --> query modules         |
  |                              |               |
  |-- static file server         v               |
  |                         PostgreSQL <---------+
  |
  `-- scheduler thread --> sync + enrichment + recommendations
```

There is one deployable application process and one PostgreSQL service. The Python application serves both the API and the static frontend. PostgreSQL is the durable source of imported data, enrichment caches, feedback, and computed recommendations. In-process threads coordinate background work, while PostgreSQL advisory locks provide the only cross-process sync exclusion.

### 1.1 Architectural thesis

The design is built around five decisions:

1. Persist Last.fm history locally so analytics do not depend on repeated remote API calls.
2. Keep HTTP handling, SQL, external API access, orchestration, and pure recommendation math in separate modules.
3. Make import operations idempotent and incremental so interrupted work can safely be retried.
4. Perform interactive analytics in PostgreSQL, close to the data, while precomputing the more expensive recommendation result.
5. Use a framework-free same-origin frontend so deployment has no asset build pipeline and no separate web tier.

### 1.2 What the system is not

Rotation is not a Last.fm proxy. Most dashboard reads query the local database and only trigger a nonblocking freshness check. It is not a multi-tenant account system: there is no application login, authorization boundary, or private user data model. Any visitor who knows a public Last.fm username can join it, view its imported analytics, compare it, and modify its recommendation feedback. It is also not a distributed job system. Background work lives in daemon threads inside each application process.

## 2. Repository and module map

```text
.
|-- app/
|   |-- main.py                  FastAPI composition and static serving
|   |-- db.py                    PostgreSQL connection factory
|   |-- lastfm.py                sole Last.fm HTTP adapter
|   |-- sync_service.py          import, scheduling, enrichment, recommendation jobs
|   |-- recommender.py           pure TF-IDF, recency, cosine, and ranking math
|   |-- routers/
|   |   |-- sync.py              join, refresh, and progress HTTP endpoints
|   |   `-- analytics.py         analytics, search, recommendation, feedback endpoints
|   |-- queries/
|   |   |-- sync.py              users, scrobbles, and enrichment SQL
|   |   |-- analytics.py         insight, history, drill-down, and comparison SQL
|   |   `-- recommend.py         recommendation corpus, cache, feedback, and songs SQL
|   `-- static/
|       `-- index.html           complete HTML, CSS, and browser JavaScript application
|-- tests/
|   |-- conftest.py              disposable PostgreSQL fixtures and shared corpus
|   |-- test_api.py              HTTP behavior and parameter-boundary tests
|   |-- test_queries.py          SQL semantics, time zones, idempotency, and query plans
|   `-- test_search_parser.py    database-free search grammar tests
|-- schema.sql                   fresh-database schema, seed rules, indexes, and view
|-- Dockerfile                   application image
|-- docker-compose.yml           application and PostgreSQL deployment
|-- .github/workflows/ci-cd.yml  test and SSH deployment pipeline
|-- requirements.txt             pinned runtime dependencies
|-- pyproject.toml               pytest configuration and DB marker
|-- .env.example                 required local environment variables
`-- README.md                    operator-facing entry point
```

### 2.1 Dependency direction

The intended dependency graph is:

```text
main
  -> routers
       -> query modules
       -> sync service
            -> query modules
            -> Last.fm adapter
            -> recommender
       -> database factory

query modules -> caller-owned psycopg cursor
recommender   -> Python values only
frontend      -> HTTP API only
```

This direction matters. Routers translate HTTP concepts into application calls. Query modules know SQL but not HTTP. `lastfm.py` knows remote response shapes but not database policy. `sync_service.py` decides when work happens and where transactions end. `recommender.py` can be reasoned about without a database, network, or web framework.

There is one small layering exception: the sync status route executes its own indexed `COUNT(*)` because progress reporting combines a database count with process-local thread state.

### 2.2 Technology stack and dependency roles

| Component | Role |
| --- | --- |
| Python 3.12 | application and job runtime |
| FastAPI | routing, parameter extraction, middleware, lifespan, and generated API docs |
| Uvicorn with standard extras | ASGI server and production process entry point |
| psycopg 3 binary package | synchronous PostgreSQL protocol and row factories |
| Requests | synchronous Last.fm HTTP client |
| python-dotenv | local environment-file loading |
| PostgreSQL 18 | durable store, analytical engine, time-zone engine, and advisory-lock coordinator |
| Browser HTML, CSS, and JavaScript | same-origin user interface without runtime client dependencies |

Runtime packages are exactly pinned so a rebuild does not silently adopt a new library release. CI separately pins pytest and httpx because they are test-only. There is no ORM, migration library, task queue, cache server, template engine, Pydantic response-model layer, or frontend package graph.

## 3. Runtime composition

### 3.1 Application startup

`app.main` constructs one FastAPI application named Rotation. Its lifespan hook calls `sync_service.start_scheduler()` when the process starts. The scheduler is a daemon thread, so it does not prevent process exit. There is no matching shutdown protocol, thread join, or cancellation signal.

The application then installs:

1. CORS middleware for HTTP origins matching `localhost` or `127.0.0.1` on any port.
2. The `/sync` router.
3. The `/analytics` router.
4. `GET /health`.
5. A static-files mount at `/`, registered last.

Mount order is important. The catch-all static mount only receives paths not already claimed by the API, so `/docs`, `/health`, `/sync/...`, and `/analytics/...` continue to work. With `html=True`, `/` resolves to `app/static/index.html`.

### 3.2 Configuration

Two environment variables define the application integration points:

| Variable | Consumer | Meaning |
| --- | --- | --- |
| `DATABASE_URL` | `app.db` | psycopg connection string for the durable store |
| `LASTFM_API_KEY` | `app.lastfm` | Last.fm API credential |

Environment loading occurs when `app.db` imports. `python-dotenv` loads a local `.env`, then `DATABASE_URL` is indexed directly from `os.environ`. A missing database URL therefore fails at import rather than at the first request. The Last.fm key is read with `getenv`, so a missing key is not validated at startup and instead fails through remote API behavior later.

### 3.3 Database connections

`db.get_connection()` opens a new synchronous psycopg connection for every call. The code consistently uses context managers to close connections and cursors. There is no process-local connection pool.

Consequences:

- Connection ownership is explicit and transactions are easy to locate.
- Idle background work does not retain a connection unless its current function keeps one open.
- Every HTTP query and job stage pays connection setup cost.
- Maximum database concurrency follows request concurrency plus background threads, not a configured pool size.
- Database saturation has no application-level backpressure beyond server and worker limits.

All application and external HTTP work is synchronous. FastAPI runs normal `def` route handlers in its threadpool, while sync jobs use explicitly created Python threads.

### 3.4 Health semantics

`GET /health` always returns `{"status": "ok"}` without touching PostgreSQL or Last.fm. This is deliberately a process liveness check, not a readiness or dependency check. It can succeed while the database is unavailable, the API key is invalid, enrichment is stalled, or the scheduler thread has failed.

## 4. Data model

PostgreSQL is both the analytical store and the coordination boundary. The schema has ten tables, one clean-tag view, two scrobble indexes, and one expression uniqueness index.

### 4.1 `users`

| Column | Meaning |
| --- | --- |
| `id` | identity primary key used by every user-scoped relation |
| `lastfm_username` | unique public Last.fm handle |
| `last_synced_at` | completed-import high-water mark, null until a pull finishes |

`last_synced_at` is not simply the time of the newest stored scrobble. It records the start time of a successfully completed import. That distinction closes a race: scrobbles created while a long import is running are newer than the captured start time and will be included on the next import.

Username uniqueness is exact and case-sensitive under the database's normal text semantics. If Last.fm treats differently cased handles as the same identity, Rotation can create logically duplicate users.

### 4.2 `scrobbles`

Each row is one completed listen:

| Column | Meaning |
| --- | --- |
| `id` | identity primary key |
| `user_id` | owner, foreign key to `users` |
| `artist_name` | remote artist spelling |
| `track_name` | remote track spelling |
| `album_name` | nullable remote album spelling |
| `listened_at` | absolute event time as `TIMESTAMPTZ` |

The uniqueness constraint is `(user_id, track_name, listened_at)`. Inserts use conflict-ignore semantics, so overlapping and repeated imports are idempotent. A genuine replay at a different timestamp remains distinct.

The constraint deliberately favors a small remote-event identity, but it omits artist name. Two different artists with the same track title scrobbled by one user at the exact same timestamp collide. Conversely, spelling or Unicode variants remain separate logical values throughout exact-key paths.

Indexes support the dominant access patterns:

- `(user_id, listened_at)` handles per-user time ranges, ordering, and pagination.
- `(user_id, artist_name)` handles per-user artist grouping and filtering.

The foreign key has no `ON DELETE CASCADE`. Removing a user requires explicitly handling all dependent rows first.

### 4.3 `track_durations`

This global cache is keyed by exact `(artist_name, track_name)` and stores `duration_ms`. Last.fm's recent-track feed does not include durations, so the maintenance process fetches them through `track.getInfo`.

A duration of zero is a negative-cache sentinel: Last.fm had no usable duration, and the work-list query should not request it forever. The tradeoff is permanence. There is no cache expiry or refresh policy, so later corrections at Last.fm do not replace an existing value.

Duration joins use exact artist and track strings. Casing and spelling variants can create duplicate cache entries or fail to match. Unknown duration contributes zero to listening-hour totals, making hours a lower bound rather than a complete measurement.

### 4.4 Raw artist tags and cleaning rules

`artist_tags` stores Last.fm tags exactly as enrichment receives them:

- primary key `(artist_name, tag)`
- `weight` is the Last.fm tag count, normally on a 0 to 100 scale
- `tag = ''` is the no-tags sentinel

Raw storage separates acquisition from interpretation. Curation rules can change without re-fetching metadata.

Three small rule tables define interpretation:

- `tag_blocklist(tag)` removes globally unhelpful tags such as platform labels, personal bookkeeping, vague praise, and selected generic nationalities.
- `tag_aliases(alias, canonical)` maps spelling variants such as `hip hop` to `hip-hop`.
- `tag_exclusions(context_tag, excluded_tag)` removes a tag only when another tag appears for the same artist. This handles context-specific ambiguity that a global blocklist cannot express.

### 4.5 `artist_tags_clean`

Every genre query and the recommendation corpus read the clean view instead of raw tags. Its pipeline is:

1. Lowercase and trim each nonempty raw tag.
2. Replace aliases with canonical values.
3. Remove global blocklist matches.
4. Build the set of `(artist, tag)` pairs suppressed by contextual exclusions.
5. Anti-join that set from the allowed tags.
6. Collapse duplicate canonical tags per exact artist and keep their maximum weight.

The suppression step is implemented as a set plus left anti-join. A correlated exclusion subquery would repeatedly scan the allowed set and previously caused a severe query-plan regression. A database test checks that PostgreSQL continues to produce an anti-join plan.

The view normalizes tags but not artist identity. `artist_name` remains exact. Some callers compensate by grouping artists with `lower(artist_name)` and selecting a representative display spelling; others join exact artist strings. This is a deliberate but incomplete boundary between source fidelity and logical identity.

### 4.6 `artist_top_tracks`

This global cache stores Last.fm's ranked popular tracks for an artist:

| Column | Meaning |
| --- | --- |
| `artist_name` | exact cache key component |
| `track_name` | exact cache key component |
| `rank` | Last.fm order, one is best |

An empty `track_name` is the no-results sentinel. The data supplies two song-recommendation strategies and artist drill-downs. Like other enrichment caches, it has no expiry or overwrite policy.

### 4.7 `recommendations`

This table is a materialized application result, not source data:

- key `(user_id, artist_name)`
- `score` is a cosine-based ranking value between zero and one, not a probability
- `rank` is the stable presentation order within the computed result
- `computed_at` records insertion time

The maintenance process replaces a user's entire set inside one transaction. Readers therefore see the old complete set or the new complete set, not a partially rebuilt list.

### 4.8 `artist_feedback`

Feedback is the only explicit user preference signal:

- `seed` means find and favor more music like this artist.
- `block` means exclude this artist and do not use it as a candidate-expansion seed.
- `expanded_at` records when similar artists were looked up for a seed; null means pending.

An expression unique index on `(user_id, lower(artist_name))` makes differently cased feedback spellings one logical record. The table has no primary key and no cascading user deletion.

### 4.9 Schema lifecycle

`schema.sql` is a fresh-database initializer. Docker's PostgreSQL entrypoint runs it only when creating a new data volume. There is no migration framework, schema version table, or automatic upgrade sequence. Schema changes to an existing deployment must be applied manually and in the correct order.

This creates a vital operational distinction:

```text
new volume     -> receives the full current schema automatically
existing volume -> keeps its existing schema until an operator migrates it
```

Application code can therefore deploy successfully while depending on a table or column that an existing database does not yet have.

## 5. Last.fm integration boundary

`app.lastfm` is the sole outbound Last.fm client. Keeping the remote protocol here prevents response-shape handling from leaking into routers and queries.

### 5.1 Remote methods

| Local operation | Last.fm method | Purpose |
| --- | --- | --- |
| `getrecents` | `user.getRecentTracks` | paginated scrobble import |
| `get_track_info` | `track.getInfo` | duration enrichment |
| `get_artist_tags` | `artist.getTopTags` | raw genre and taste features |
| `get_similar_artists` | `artist.getSimilar` | widen the unplayed candidate corpus |
| `get_artist_top_tracks` | `artist.getTopTracks` | song suggestions and artist details |

Requests target `http://ws.audioscrobbler.com/2.0`, not HTTPS. The API key and user-controlled names travel to Last.fm over that configured transport. Production edge TLS does not protect this backend-to-Last.fm hop.

### 5.2 Recent-track normalization

`getrecents` turns remote JSON into internal dictionaries containing artist, track, album, and a Unix timestamp. It handles several irregularities:

- Last.fm error code 6 becomes the specific `LastfmUserNotFound` exception.
- A single track object and a list of track objects become the same iterable form.
- Missing track collections become an empty list.
- Currently playing entries with no completed-play date are skipped.
- An empty album becomes `None`.
- Pagination metadata is returned with normalized tracks.

Skipping now-playing entries avoids inventing an event timestamp. A subsequent refresh will capture the track after Last.fm supplies its completed scrobble date.

### 5.3 Error and timeout behavior

No Last.fm request specifies a timeout. A slow or wedged remote connection can occupy an application or background thread indefinitely.

The recent-track function calls `raise_for_status()` after checking Last.fm's domain error code. The metadata helpers parse JSON without the same HTTP-status enforcement and treat absent result structures as zero or empty lists. Network exceptions still propagate. This produces intentionally tolerant enrichment but inconsistent handling between HTTP errors, domain errors, and structurally empty data.

There is no retry policy, exponential backoff, circuit breaker, response cache outside PostgreSQL, or global rate limiter in this adapter.

## 6. Join and synchronization pipeline

Synchronization is the central write pipeline. Its goal is to make partial progress durable while only advancing the high-water mark after a complete pull.

### 6.1 First join

`POST /sync/{username}` calls `sync_service.join(username, force=True, wait=True)`.

For a username absent from `users`:

1. Request one recent track from Last.fm to validate the handle.
2. Translate definite Last.fm user-not-found into HTTP 404.
3. If validation fails for another reason, log it and continue, treating it as transient.
4. Insert the local user and commit.
5. Start a forced background sync.
6. Wait up to four seconds for useful initial progress.
7. Return while the daemon thread may continue importing or enriching.

Transient validation tolerance prevents temporary Last.fm failure from permanently rejecting a real handle. It can also create a local row for a mistyped handle when the failure is not the explicit user-not-found response.

Two concurrent first joins can both observe no row and attempt the unique insert. That race is not caught and normalized, so one request can fail with a database uniqueness error.

### 6.2 Existing user refresh

For an existing user, `join` checks freshness. A user is stale when `last_synced_at` is null or older than one day. Explicit sync uses `force=True`, while ordinary analytics reads only start work when stale.

The service tracks active work in two lock-protected process-local dictionaries:

- `_active[user_id]` holds the current sync thread.
- `_phase[user_id]` is `pulling`, `enriching`, or absent.

If a thread for that user is already alive, later calls reuse it rather than start another. If `wait=True`, the caller joins that thread for at most `WAIT_BUDGET_SECONDS`, currently four seconds. Waiting happens outside the state lock so status checks and unrelated users are not blocked.

Analytics routes call `ensure_fresh(..., wait=False)`. A dashboard fans out across many endpoints, so each panel may request freshness, but the active-thread map coalesces them into one local sync and no panel pays the wait budget.

### 6.3 Cross-process exclusion

Process-local thread state cannot coordinate multiple Uvicorn workers or multiple application containers. `_run_sync` therefore obtains a PostgreSQL session advisory lock keyed by `user_id` before pulling scrobbles. If another process owns the lock, the contender exits.

The two coordination levels solve different problems:

| Mechanism | Scope | Purpose |
| --- | --- | --- |
| `_active` plus mutex | one Python process | coalesce requests and expose status |
| PostgreSQL advisory lock | all processes using the database | prevent concurrent scrobble pulls for one user |

The advisory lock protects only the scrobble-pull section. Enrichment, candidate expansion, and recommendation rebuilding can still run concurrently in multiple processes.

### 6.4 Pagination and transaction boundaries

The importer requests newest pages first. Its safe-update sequence is:

```text
sync_started_at = current UTC time
since = previous last_synced_at, if any

for page 1 through total_pages:
    call Last.fm
    insert every completed scrobble with ON CONFLICT DO NOTHING
    commit this page
    pause 0.25 seconds before later calls

update users.last_synced_at = sync_started_at
commit
```

Page-level commits are the durability choice. A failure on page 80 retains pages 1 through 79. Because the watermark is unchanged, the next run re-requests the overlap, and the unique constraint discards duplicates.

Updating the watermark to the sync start instead of finish time prevents a gap for tracks created during import. Overlap is accepted and made cheap by idempotent inserts.

The 0.25 second pause is per importing thread. It approximates four paginated calls per second for one user but is not a process-wide or deployment-wide limiter. Concurrent users and enrichment workers can exceed that aggregate rate.

### 6.5 Enrichment immediately after a pull

After pagination releases the advisory lock, the same thread moves to `enriching` and fills missing durations and tags for that user. This makes a newly joined dashboard improve without waiting for the global scheduled pass.

It does not immediately widen candidate artists, rebuild recommendations, or fetch top tracks. Those outputs remain empty or stale until maintenance runs.

### 6.6 Failure semantics

The sync thread catches and logs its top-level exceptions so background failure does not crash the web process. Existing committed pages and enrichment rows remain. The active-thread entry is cleared only when it still points at the current thread, preventing an old thread from erasing a replacement's state.

Important failure outcomes:

- Partial pagination: stored pages remain; watermark stays old; retry overlaps safely.
- Last.fm account missing during refresh: the exception is logged; existing data remains.
- Process restart: daemon work stops without cleanup; a later request or scheduler pass can retry.
- PostgreSQL outage: requests and jobs fail; liveness health can still report success.
- Remote call without timeout: a thread and its visible `syncing` state can remain stuck indefinitely.

The hourly scheduler calls `_run_sync` directly rather than registering that work in `_active`. PostgreSQL still protects the pull, but `/sync/{username}/status` can report `syncing: false` for scheduler-initiated work. The direct call can also leave a phase entry behind; the status endpoint ignores the phase when no active thread is reported.

## 7. Scheduled maintenance pipeline

Every application process starts one daemon scheduler. It sleeps one hour before the first pass, then repeats a maintenance pass every hour. The same pass can be run manually through the module entry point.

The order is deliberate:

```text
1. Import stale users
2. Backfill track durations
3. Backfill artist tags
4. Widen the recommendation candidate pool
5. Rebuild cached artist recommendations
6. Backfill top tracks for song recommendations
```

Each stage feeds the next. A recommendation needs clean tags, candidate widening adds unplayed tagged artists, and top-track fetching needs the newly computed recommendation set.

### 7.1 Work-list pattern

Enrichment queries select source values for which no cache row exists. Workers then fetch and insert one item at a time. Exact-key conflict handling makes repeated workers database-safe, and per-item commits preserve progress.

Empty upstream results are stored as sentinel rows:

- unknown duration: `duration_ms = 0`
- no artist tags: `tag = ''`
- no top tracks: `track_name = ''`

This prevents permanent retry loops but turns a temporary empty response into a permanent negative cache. There is no distinction between authoritative absence and transient upstream incompleteness.

### 7.2 Duration backfill

The worker finds distinct scrobbled `(artist, track)` pairs lacking exact duration rows, calls `track.getInfo`, inserts the result, and commits. It can be restricted to a newly synced user or run globally.

### 7.3 Tag backfill

The worker finds distinct scrobbled artists lacking any exact raw tag row, calls `artist.getTopTags`, stores all returned weighted tags, or stores an empty sentinel, then commits. Candidate expansion also uses the same cache for artists the user has never played.

### 7.4 Candidate-pool widening

A content recommender cannot recommend an artist absent from its corpus. Rotation expands that corpus from Last.fm's artist graph:

1. For each user, load pending explicit `seed` feedback.
2. If there is no pending seed and the user already has at least 20 cached recommendations, skip expansion.
3. Combine pending seeds with the user's ten most-played artists.
4. Deduplicate names case-insensitively.
5. Request up to ten similar artists per seed.
6. Drop names already scrobbled or already present in `artist_tags`, case-insensitively.
7. Fetch and cache tags for genuinely unknown candidates.
8. Mark pending seeds as expanded.

Because `artist_tags` is a global cache, candidates discovered for one user can become candidates for every user whose taste vector overlaps them.

There is a retry gap in current behavior. Failures from an individual similar-artist lookup are caught so the pass can continue, but the final update marks all pending seeds expanded, including a seed whose lookup failed. That explicit seed will not automatically retry unless its feedback record becomes pending again.

The endpoint accepts arbitrary artist strings and the application has no authentication or per-user quota. Public feedback can therefore cause external lookups and grow global metadata storage.

### 7.5 Recommendation refresh

The refresh loads the full clean tag corpus once, computes corpus-wide IDF and artist vectors, then iterates through all users. For each viable user vector it computes up to 20 artist recommendations and replaces the cache transactionally.

This is a batch architecture. Interactive API calls read cached recommendations and do not run corpus-wide vector math.

### 7.6 Top-track backfill

The final stage finds artists needed for song suggestions:

- each user's top 15 favorite artists
- every cached recommended artist

It requests Last.fm top tracks for artists lacking cache entries, stores ranked tracks or a sentinel, and commits per artist.

## 8. Recommendation architecture

Rotation uses a transparent content-based recommender. Its features are cleaned Last.fm tags, its user evidence is play history with time decay, and its comparison is cosine similarity.

### 8.1 Corpus construction

`get_tag_corpus` reads every row in `artist_tags_clean`, folds artist names by lowercase identity, chooses a representative display spelling, and builds:

```text
artist -> {tag -> weight}
```

The corpus contains both played artists and unplayed candidates added by widening. Without the latter, a pure local-history corpus would only contain artists the user already knows, all of which are excluded from recommendations.

### 8.2 Inverse document frequency

For each tag `t`:

```text
IDF(t) = ln(total artists / artists carrying t)
```

A tag attached to nearly every artist contributes little. A rarer tag contributes more. This prevents generic labels from dominating similarity even after curation.

### 8.3 Artist vectors

Within an artist, raw tag weights become term frequency:

```text
TF(artist, tag) = tag weight / sum of that artist's tag weights
artist_vector[tag] = TF * IDF
```

Normalizing by each artist's total tag weight makes artists comparable even when Last.fm supplies different absolute tag counts.

### 8.4 Time-decayed user evidence

Scrobbles are folded by lowercase artist identity. Each play contributes:

```text
play_weight = 0.5 ^ (age_in_days / 90)
```

The half-life is 90 days. A play today weighs 1, a play 90 days old weighs 0.5, and a play 180 days old weighs 0.25.

An artist's accumulated play weight is compressed before entering the taste vector:

```text
artist_preference = ln(1 + accumulated_play_weight)
user_vector += artist_vector * artist_preference
```

Log compression lets repeated plays strengthen a signal without allowing one heavily played artist to overwhelm every other preference.

### 8.5 Cosine ranking

For user vector `u` and candidate artist vector `a`:

```text
cosine(u, a) = dot(u, a) / (magnitude(u) * magnitude(a))
```

Candidates with no shared weighted features score zero and are discarded. Artists the user has played, explicit blocks, and explicit seeds are excluded from output using case-insensitive identity. Remaining candidates sort descending and the first 20 are cached.

The score is geometric similarity, not confidence, predicted enjoyment, or probability. Frontend labels and meter scaling are presentation policy layered over this value.

### 8.6 Explicit seeds

Seed artists produce a second vector using equal per-seed evidence. Recommendation scoring blends the two similarities:

```text
final_score = 0.5 * taste_similarity + 0.5 * seed_similarity
```

The seed vector changes ranking only when the next maintenance refresh occurs. The candidate widening stage also explores Last.fm similars for pending seeds, so a seed affects both the available corpus and the score.

### 8.7 Blocks

A block is removed from the cached recommendation table immediately when feedback is submitted. It is also excluded from later rebuilds and from expansion seeds. Undoing a block removes the feedback record, but the artist can return only after a later recommendation refresh.

### 8.8 Empty-vector and determinism behavior

If a user has no usable tagged play evidence, refresh skips replacement and leaves any old cache intact. A nonempty mapping whose weights are all zero passes the truthiness check, can produce no positive candidates, and can clear the cache.

Scores are sorted without an explicit secondary tie key, and the corpus query has no ordering contract. Python's stable sort preserves its input order for exact ties, so equal-score rank can depend on PostgreSQL row order. The scores remain correct, but tie presentation is not strictly deterministic.

### 8.9 Song recommendations

Song recommendations are SQL views over the top-track cache, not another ML model.

#### Songs from favorites

1. Select the user's top 15 artists by play count.
2. Join cached top tracks.
3. Exclude exact `(artist, track)` values already scrobbled.
4. Keep at most two tracks per favorite artist.
5. Round-robin by per-artist track position, then favor more-played artists.
6. Return at most 25 tracks.

This spreading rule prevents the result from becoming a block of songs from only the single favorite artist.

#### Songs from new artists

1. Traverse cached artist recommendations by recommendation rank.
2. Join the first three top tracks for each artist.
3. Return them in artist-rank then track-rank order.

There is no final flat SQL limit, so the result size is up to three times the cached artist recommendation count.

Already-played filtering uses exact strings. Track remasters, punctuation variants, and casing differences can appear as apparently new songs.

## 9. Analytics query architecture

All main insights run in PostgreSQL and return JSON-shaped rows through psycopg's `dict_row`. Query functions accept a caller-owned cursor, which keeps transaction and connection policy outside the query layer.

### 9.1 Shared time semantics

Scrobbles are stored as absolute `TIMESTAMPTZ` values. Human concepts such as day, month, weekday, and part of day must use the listener's browser-reported IANA time zone.

The router validates the zone with Python `ZoneInfo`. Unknown or malformed zones fall back to UTC before reaching PostgreSQL. Query helpers use `AT TIME ZONE` for local date, month, period, weekday, and six-hour day-part bucketing.

The four day parts are:

| Part | Local hours |
| --- | --- |
| `night` | 00:00 through 05:59 |
| `morning` | 06:00 through 11:59 |
| `afternoon` | 12:00 through 17:59 |
| `evening` | 18:00 through 23:59 |

Range-picker `days` values follow one router policy:

- zero, negative, or absent means all time
- positive values mean a trailing duration from database `now()`
- positive values are capped at 1,826 days

The range is an exact timestamp interval, not a count of complete local calendar days.

### 9.2 Primary genre convention

Charts that must assign one genre to one play choose a primary tag per exact artist:

1. Highest clean tag weight wins.
2. Alphabetical tag order breaks equal-weight ties.

This prevents a single scrobble from being counted once for every tag. The recommender intentionally does the opposite and uses the full weighted tag vector.

Artists without a clean tag disappear from primary-genre metrics. Genre totals therefore do not necessarily equal total play counts.

### 9.3 Streaks

Streaks reduce scrobbles to distinct listener-local dates. The gaps-and-islands technique subtracts `row_number()` from each date so consecutive dates share a group key. Each group returns its start date, end date, and day count.

Results order by longest run and then most recent. The first row is the longest streak, not necessarily a currently active streak.

### 9.4 Discovery

Discovery finds each case-folded artist's earliest scrobble, converts it to the listener's local month, and counts first appearances per month. Differently cased spellings are one artist for this metric.

This measures first appearance in imported history, not necessarily when the listener first encountered the artist outside Last.fm.

### 9.5 Loyalty and heavy rotation

For artists with at least five plays in the selected range, loyalty measures:

```text
active local days / days from the artist's first play to the user's latest play
```

The denominator is anchored to the user's latest scrobble inside the same range, not wall-clock today. This keeps an old static archive internally meaningful. Results sort by ratio and then play count.

The metric distinguishes an artist played repeatedly across time from an equally large one-day binge. It is not a retention probability, and a narrow selected range changes both numerator and denominator.

### 9.6 Listening clock

The listening clock groups real listener-local dates by the four day parts and counts plays. SQL returns nonempty cells only. The frontend inserts visual zero cells from the first returned date through today, paginates visible date windows, and pins the latest range by default.

For a selected SQL cell, the frontend drills into scrobble history with `date:` and `part:` filters using the same time zone. Database tests enforce that each heatmap count equals the count returned by its drill-down filter.

When a trailing window begins before the first returned play, the frontend does not draw leading inactive dates. It starts from the first actual row.

### 9.7 Genre clock

The genre clock groups primary-tagged plays by local weekday, local day part, and primary tag. The frontend renders a 7 by 4 typical-week matrix, selects the dominant tag for each cell, and exposes the tagged play count.

A click drills into all scrobbles matching that weekday and day part. Because the cell count covers primary-tagged plays while the history filter does not require a tag, the drill-down row count may exceed the displayed genre-cell count.

### 9.8 Album and song binges

Both binge queries use a rolling seven-day PostgreSQL `RANGE` window. They find the maximum number of plays for an item inside any such window, then require a configurable minimum.

- Album binges exclude null and empty album names.
- Song binges operate on artist and track and return at most 25 rows.
- Album results are not limited in SQL; the current interface presents a subset.
- Optional `days` first restricts the source population, while the definition of a binge remains a fixed seven-day window.

### 9.9 Tag shift

Tag shift groups primary-tagged plays by listener-local week or month and tag. Each row includes play count and the tag's percentage of all tagged plays in that period.

The percentage denominator excludes untagged plays. A rise can mean more plays of the tag, fewer plays of other tagged genres, or both. The genres ranking reuses these rows by summing over the selected range.

### 9.10 Listening hours

Hours groups scrobbles by local week or month, left joins exact duration-cache keys, sums milliseconds, and converts to hours. Unknown or unmatched durations contribute zero.

This makes the result monotonic as enrichment fills in, but it is an estimate and undercounts incomplete metadata. It does not infer duration from adjacent scrobble timestamps.

### 9.11 Period report

The report groups by local week or month and returns plays, distinct track names, distinct artists, and change from the preceding nonempty result row via `LAG`.

The comparison is not guaranteed to be the immediately adjacent calendar period. If a user has an empty month, the next nonempty month compares with the prior nonempty month. Distinct tracks use track name alone, so identically named tracks by different artists merge in that count.

### 9.12 Monthly summary

The summary combines per-local-month:

- total plays
- newly discovered artists
- known listening hours
- top primary genre

Top-genre ties resolve alphabetically. Because hours and genre depend on enrichment, summary dimensions can be incomplete while raw play totals are complete.

### 9.13 Artist detail

Artist lookup is case-insensitive for the user's scrobbles. It returns play count, representative artist spelling, clean tags, and cached top tracks. The artist name is a query parameter because slashes and other path-hostile characters are valid name content.

### 9.14 Genre drill-down

Genre detail returns the user's tracks whose exact artist has the requested primary clean tag, ranked by play count. It answers the same one-primary-tag interpretation used by the genre charts, not any-tag membership.

### 9.15 Compatibility

Compatibility constructs one raw primary-tag play-count vector per user and computes cosine similarity times 100. It does not use recommendation TF-IDF, time decay, or the full multi-tag artist vector.

The response also derives:

- shared genres and each user's share
- divergent genre emphasis
- shared artists, case-folded for identity
- a `pending` flag while either import is active in the current process

Self-comparison explicitly copies the vector and yields 100 when the vector has magnitude. Shared-artist rows are fetched before Python applies its presentation count, which can do unnecessary database and transfer work for large histories.

Unlike ordinary analytics reads, comparison can join and begin importing a previously unknown username. It is a mutating `GET`. The secondary username's explicit not-found error becomes 404. A direct request with an unknown primary also calls `join`, but its not-found exception is not caught at that point and can become a server error.

## 10. Scrobble search and pagination

History search is a small query language parsed in Python and compiled into bound SQL fragments.

### 10.1 Search grammar

Supported field terms are:

| Form | Meaning |
| --- | --- |
| `artist:value` | artist contains value |
| `track:value` | track contains value |
| `album:value` | album contains value |
| `year:YYYY` | listened inside that year |
| `month:1..12` | timestamp month number |
| `date:YYYY-MM-DD` | listener-local date |
| `day:name` | listener-local weekday |
| `part:name` | listener-local day part |

Quoted values can contain spaces. Bare text searches artist or track. Unknown fields, malformed recognized values, and unrecognized tokens degrade to free text instead of causing a parser error.

Repeated textual terms use `AND`. Multiple values within year, month, date, day, or part use `OR` inside that field. The final categories are combined with `AND`.

### 10.2 SQL safety and semantics

User values are bound parameters. Sort columns and direction are selected from explicit whitelists, preventing them from becoming SQL syntax.

The parser has several semantic edges:

- `%` and `_` are not escaped before `ILIKE`, so users can intentionally or accidentally use SQL wildcard semantics.
- An unquoted apostrophe-containing name such as `artist:Guns N' Roses` does not crash, but tokenizes in a surprising way.
- A syntactically numeric but database-invalid extreme year can still cause a PostgreSQL date construction error.
- `start` and `end` are accepted as strings and rely on database casting.
- `date`, `day`, and `part` explicitly use listener-local time.
- `month`, year ranges, and raw start/end boundaries do not all share that same explicit local-time conversion, so records near UTC day, month, or period boundaries can disagree with local-time charts.

### 10.3 Page contract

The endpoint clamps:

- `limit` to 1 through 200
- `offset` to zero or greater
- unknown sort columns to the default
- direction to an allowed value

Page and count queries reuse the same filter builder, keeping `total` consistent with returned rows. Pagination is offset-based and ordered primarily by a selected column. New inserts between requests and ties without a unique final ordering can move rows between pages.

## 11. HTTP API surface

The API uses JSON and has no version prefix. Most responses are inferred directly from dictionaries and query rows rather than declared FastAPI response models.

### 11.1 Sync and service routes

| Method and path | Inputs | Behavior |
| --- | --- | --- |
| `GET /health` | none | process liveness only |
| `POST /sync/{username}` | username | validate or create, force background refresh, wait up to four seconds |
| `GET /sync/{username}/status` | username | local active state, phase, stored scrobble count, high-water mark |

### 11.2 Analytics routes

All normal analytics endpoints first resolve an existing local user, return 404 if absent, and trigger a nonblocking refresh if stale.

| Method and path | Important parameters | Result or side effect |
| --- | --- | --- |
| `GET /analytics/{u}/streaks` | `tz` | consecutive listening-day runs |
| `GET /analytics/{u}/discovery` | `tz` | first-seen artists by month |
| `GET /analytics/{u}/loyalty` | `tz`, `days` | artist rotation ratio |
| `GET /analytics/{u}/clock` | `tz`, `days=365` | date and day-part counts |
| `GET /analytics/{u}/genre-clock` | `tz`, `days` | weekday, day-part, primary-tag counts |
| `GET /analytics/{u}/summary` | `tz` | monthly combined digest |
| `GET /analytics/{u}/compatibility/{other}` | second username | compare users and possibly join either one |
| `GET /analytics/{u}/binges` | `min_plays=6`, `days` | rolling seven-day album peaks |
| `GET /analytics/{u}/tag-shift` | `period`, `tz`, `days` | genre composition by period |
| `GET /analytics/{u}/hours` | `period`, `tz` | known listening hours by period |
| `GET /analytics/{u}/recommendations` | none | cached artists, two song lists, feedback |
| `GET /analytics/{u}/artists` | `limit` | most-played artists for seed selection |
| `POST /analytics/{u}/feedback` | `artist`, `verdict` | upsert seed or block; block evicts immediately |
| `DELETE /analytics/{u}/feedback` | `artist` | remove explicit preference |
| `GET /analytics/{u}/report` | `period`, `tz` | period totals and changes |
| `GET /analytics/{u}/artist` | `name` | artist detail modal data |
| `GET /analytics/{u}/scrobbles` | search, page, sort, range, tz | filtered history plus total |
| `GET /analytics/{u}/song-binges` | `min_plays=5`, `days` | rolling seven-day song peaks |
| `GET /analytics/{u}/genre` | `tag` | primary-genre track detail |

`period` is interpreted as week only when it equals the supported week value; other values generally follow the month branch. Most free text inputs have no explicit maximum length.

### 11.3 Freshness model visible to clients

A successful analytics response means the query ran against a consistent local database snapshot. It does not guarantee the user was fully refreshed immediately before that query. Reads favor latency and availability of existing data:

```text
read request -> trigger stale refresh in background -> return current local result
```

The explicit sync endpoint provides a bounded initial wait, and status polling communicates eventual completion. This is eventual freshness, not read-after-refresh consistency.

## 12. Frontend architecture

The complete browser application is one `index.html` containing semantic markup, responsive CSS, and plain JavaScript. There is no frontend framework, module bundler, compiler, package manifest, client router, or asset pipeline.

### 12.1 UI structure

The application has six in-page tabs:

1. Overview
2. Scrobbles
3. Taste
4. Trends
5. For you
6. Compare

The page begins with a Last.fm username form. After joining, it exposes headline totals, bar charts, a date heatmap, a genre routine heatmap, listening hours, period summaries, binge lists, genre and loyalty rankings, artist and song recommendations, feedback controls, comparison, searchable history, and artist or genre modals.

Tabs are display state, not URL routes. Reloading loses the selected tab, ranges, periods, history page, open modal, and loaded user. Theme is the only durable browser preference, stored in `localStorage`.

### 12.2 API base selection

The frontend uses same-origin requests when the page runs on an empty port or port 80, 443, or 8000. On other ports it points to `http://localhost:8000`, supporting a common local static-server workflow.

This heuristic is deployment-specific. A legitimate nonstandard hosted port can silently route its API traffic to the viewer's local machine.

### 12.3 Initial load sequence

```text
submit username
  -> POST /sync/{username}
  -> start polling /sync/{username}/status every 2.5 seconds
  -> concurrently request all dashboard panels
  -> render fulfilled panels
  -> report partial failures without discarding successful panels
  -> redraw when sync phase changes or finishes
```

The backend waits at most four seconds on the explicit sync. The UI then treats background progress as normal. Status includes stored scrobble count and `pulling` or `enriching`, so a first import can show movement instead of a frozen loading state.

The polling callback is asynchronous inside `setInterval`. A slow request can overlap the next tick because there is no in-flight guard.

### 12.4 Panel fan-out and partial failure

The dashboard requests its panel endpoints concurrently with `Promise.allSettled`. Successful responses render even when other panels fail. If all fail, the load rejects. The status text can later be replaced by sync progress messaging, so a partial-panel failure count is not permanently visible.

Independent controls scope different metrics:

- range choices include 7, 30, 90, 365 days and all time
- genres, binges, loyalty, clock, and routine keep separate range state
- report and hours keep independent weekly or monthly period state

A serialized picker-state key is captured around refreshes. If the user changes controls before a dashboard response arrives, that stale response is discarded. Direct range and period handlers apply the same latest-selection principle.

History-page requests do not use an equivalent generation token, so a slower earlier search or sort request can overwrite a later one.

### 12.5 Rendering and interaction

The browser renders charts with DOM elements and CSS rather than a charting library:

- period bars use proportional element heights
- listening history uses a scrollable calendar-style date heatmap
- genre routine uses a weekday by day-part matrix
- recommendation scores use custom horizontal meters
- tables support sortable headers and resizable columns

Heatmap clicks translate visual buckets into the history search grammar. Period bars set raw start and end filters. Artist and genre clicks load JSON into a reusable modal. Recommendation controls use event delegation so dynamically rendered buttons work without per-row handlers.

### 12.6 Escaping and browser security

An HTML-escaping helper protects most remote and database strings before insertion through `innerHTML`. One current exception is the visible scrobble filter label: the raw search string is interpolated into `innerHTML`. A crafted search value can therefore produce client-side markup or script behavior in the user's own browser context.

The application has no Content Security Policy configured in its code or container. It also has no client authentication token, CSRF token, or origin-bound authorization because the API itself has no account ownership model.

### 12.7 Accessibility and responsive behavior

The page uses real forms, buttons, table elements, labels, a live status region, a dialog-like modal surface, keyboard-reachable controls, and responsive CSS. Heatmap cells and custom visual interactions still rely heavily on dynamically generated elements and tooltip behavior, so they do not provide the same semantics as native tabular or chart alternatives. There is no automated accessibility test suite.

## 13. End-to-end execution traces

These traces connect modules and transaction boundaries. They are the fastest way to explain the application in an interview.

### 13.1 New user opens Rotation

```text
Browser submits username
  -> POST /sync/{username}
  -> sync router calls sync_service.join
  -> sync query finds no local user
  -> Last.fm adapter validates with one recent-track request
  -> users row inserted and committed
  -> daemon sync thread registered in process-local state
  -> request waits at most four seconds

Sync thread
  -> opens PostgreSQL connection
  -> obtains advisory lock for user id
  -> pages Last.fm recent tracks newest first
  -> inserts conflict-safe scrobbles and commits each page
  -> writes sync-start high-water mark after all pages
  -> releases advisory lock
  -> backfills this user's durations and tags
  -> clears active state

Browser in parallel
  -> polls status
  -> fetches analytics panels
  -> each panel reads whatever has committed so far
  -> rerenders after progress or completion
```

### 13.2 Existing user opens a dashboard

```text
Browser requests many analytics endpoints concurrently
  -> each route resolves username locally
  -> each calls ensure_fresh without waiting
  -> first stale check starts one local thread
  -> later checks observe the same active thread
  -> every route opens its own query connection
  -> current stored results return immediately
```

The page can therefore be fast while a refresh proceeds, but panels in one fan-out are not enclosed in one shared database transaction. They may observe slightly different committed states as sync pages land.

### 13.3 Scheduled recommendation build

```text
scheduler wakes after one-hour interval
  -> sync stale accounts
  -> fill global duration cache
  -> fill raw tag cache
  -> expand similar artists and tag new candidates
  -> load clean global corpus
  -> compute IDF and artist vectors in Python
  -> compute each user vector and ranked candidates
  -> atomically replace each user's cached artist list
  -> fetch top tracks required by favorites and new artists
```

An artist can appear in `artist_tags` without appearing in `scrobbles`. That is the intended bridge from known listening history to genuinely unplayed recommendations.

### 13.4 Feedback lifecycle

```text
User clicks "not interested"
  -> POST feedback with verdict=block
  -> feedback upsert and cached-row deletion commit together
  -> current list drops the artist
  -> later rebuild keeps it excluded

User submits "more like this"
  -> POST feedback with verdict=seed
  -> record is pending because expanded_at is null
  -> later maintenance fetches similar artists and tags
  -> later rebuild blends taste and seed similarity equally
```

### 13.5 Chart drill-down

```text
Clock query groups timestamps in browser IANA zone
  -> browser renders local date and part cell
  -> click emits date:YYYY-MM-DD plus part:name
  -> history parser validates both tokens
  -> history SQL applies the same local conversions
  -> matching scrobbles appear in the table
```

This is an example of an end-to-end semantic invariant spanning browser state, route validation, query helpers, and tests.

## 14. Consistency, concurrency, and ownership

### 14.1 Sources of truth

| Concern | Source of truth |
| --- | --- |
| imported play event | `scrobbles` |
| completed import position | `users.last_synced_at` |
| raw remote metadata | enrichment cache tables |
| tag interpretation | curation tables plus `artist_tags_clean` |
| explicit taste preference | `artist_feedback` |
| current served artist ranking | `recommendations` |
| active sync phase | current Python process memory |
| selected UI controls | current browser memory |

Process-local sync state is observational and temporary. Database state is durable. A process restart loses status knowledge but not committed progress.

### 14.2 Transaction granularity

| Operation | Commit unit | Reason |
| --- | --- | --- |
| new user creation | one user row | make identity available before background work |
| scrobble import | one Last.fm page | preserve partial progress |
| high-water update | after all pages | never claim incomplete import |
| duration/tag/top-track enrichment | one work item | preserve slow remote progress |
| recommendation replacement | whole user result | readers never see partial rank sets |
| feedback mutation | one request | preference and immediate eviction stay aligned |

### 14.3 Idempotency boundaries

Database constraints make these repeats safe:

- scrobble page replay
- duplicate duration insertion
- duplicate raw tag insertion
- duplicate top-track insertion
- recommendation replacement
- feedback upsert by case-folded artist identity

Idempotent storage does not make repeated external calls free. Multiple schedulers and per-process worker state can duplicate Last.fm traffic even when final rows are correct.

### 14.4 Multi-worker behavior

Running multiple application workers changes the architecture materially:

- each worker starts its own scheduler
- each worker owns a separate `_active` and `_phase` map
- status only sees its current worker's map
- advisory locking prevents simultaneous scrobble import for a user
- other maintenance work can overlap across workers
- no global Last.fm request budget is enforced

The application is database-correct under much of this duplication because of conflicts and transactions, but progress reporting and external-call efficiency are not distributed-system safe.

## 15. Deployment architecture

### 15.1 Container image

The application image uses Python 3.12 slim, installs exact runtime dependencies from `requirements.txt`, copies only `app/`, exposes port 8000, and starts:

```text
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

It runs as the image's default root user. The base image is pinned to a major and minor Python family rather than an immutable digest. There is no build stage because there are no compiled frontend assets.

### 15.2 Compose topology

Docker Compose defines:

- `db`: PostgreSQL 18, named persistent volume, fresh-volume schema initialization, TCP health check
- `app`: locally built Python image, database URL and Last.fm key, dependency on healthy PostgreSQL, loopback-only host publication, HTTP liveness check, restart policy

The database is reachable on the private Compose network and is not published to the host. The application is published on host loopback rather than every host interface. This expects an edge tunnel or reverse proxy on the host to reach `127.0.0.1:8000`.

The configured database credential is suitable for a small controlled deployment but is embedded in Compose configuration rather than managed as a rotated secret. PostgreSQL data durability depends on the named volume. No backup, restore test, retention, or disaster-recovery process is encoded.

### 15.3 Public request path

The intended request path is:

```text
public client
  -> TLS edge and tunnel
  -> host loopback port 8000
  -> Uvicorn/FastAPI
  -> private Compose PostgreSQL network
```

The tunnel runs outside Compose, so an application rebuild does not replace it. The repository does not define the external edge account, DNS, access policy, or host service installation. Those are operational dependencies beyond the container graph.

### 15.4 CI pipeline

Pushes and pull requests run a test job with:

1. Python 3.12.
2. A PostgreSQL 18 service.
3. Exact runtime dependencies.
4. Explicitly pinned pytest and httpx test versions.
5. The complete pytest suite.

This validates application SQL against the same PostgreSQL major version used by Compose.

### 15.5 Deployment pipeline

Deployment runs only after tests pass, on a push to the main branch, and when a repository setting enables deployment. The job:

1. Configures SSH credentials.
2. Connects to the deployment host.
3. Fast-forwards the checkout.
4. Rebuilds and recreates Compose services.
5. Prunes dangling images.
6. Retries the public `/health` endpoint as a smoke test.

The production image is rebuilt on the destination host rather than promoted as the exact artifact tested in CI. Mutable base-image and package-source changes can therefore make the deployed build differ from the tested environment despite the same commit and pinned Python packages.

Rollback is source checkout plus rebuild, not an atomic image switch. Recreating the application can cause a short interruption. Database migrations are not part of the job. The public smoke test validates DNS, edge tunnel, Uvicorn, routing, and the health handler, but because health does not query PostgreSQL it does not validate analytical readiness.

## 16. Test architecture

The suite currently collects 138 tests. Seventeen search-parser tests are database-free. The remaining tests use a real PostgreSQL database because PostgreSQL-specific behavior is part of the product, including `TIMESTAMPTZ`, intervals, window frames, query plans, expression indexes, and `AT TIME ZONE`.

### 16.1 Database fixture lifecycle

The fixture layer:

1. Connects to an administrative database through `TEST_ADMIN_URL` or a local default.
2. Drops and recreates a database named `lastfm_test`.
3. Applies `schema.sql`.
4. Points the imported application at that database.
5. Supplies transaction-aware connections and a representative shared corpus.
6. Drops the test database after the session.

Tests reached through a database fixture are automatically marked `db`. `pytest -m 'not db'` is the intentional no-PostgreSQL subset.

### 16.2 What is strongly covered

The suite exercises:

- unknown and known-user API behavior
- time-zone fallback and real IANA zones
- local-day, month, weekday, and day-part semantics
- heatmap count to drill-down equality
- case folding in discovery, loyalty, compatibility, and recommendations
- loyalty thresholds and window anchors
- tag cleaning and context suppression
- the clean-tag anti-join query plan
- range narrowing
- search parsing, malformed tokens, bound values, total/page agreement, and clamps
- offset pages in a fixed dataset
- scrobble and enrichment idempotency
- work-list exhaustion after cache insertion
- candidate filtering and recommendation exclusion of already-played artists

### 16.3 What is not covered end to end

There are no automated tests for:

- live Last.fm request construction, timeouts, or all response/error shapes
- thread registration, phase cleanup, wait budgets, and advisory-lock contention
- scheduler timing and duplicate schedulers
- candidate-expansion failure retry behavior
- complete recommendation seed blending through the database job
- top-track fetch orchestration
- browser rendering, request races, XSS, responsiveness, or accessibility
- Docker image startup, Compose health dependencies, SSH deployment, or rollback
- applying schema changes to an existing database

One artist-detail API assertion permits either 200 or 404, so it does not enforce a single contract for that edge. The test client currently emits a dependency deprecation warning, which is not a test failure but signals future compatibility work.

The full local suite requires a reachable PostgreSQL server. Without it, the database-free subset passes while database tests fail during fixture setup rather than exercising application behavior.

## 17. Security, privacy, and trust boundaries

### 17.1 Public identity model

Last.fm usernames and listening histories are treated as public data. Rotation adds persistence, analysis, comparison, and recommendations, which can increase the sensitivity of otherwise public events. The application defines no consent, deletion endpoint, retention period, or ownership proof.

### 17.2 Authentication and authorization

There is none. Any client can:

- trigger import for a username
- read any locally joined user's analytics
- join comparison users
- submit or remove recommendation feedback for any user
- cause some external API work and database growth

This is acceptable only if the deployment intentionally offers a public shared service with those semantics. It is not sufficient for private preferences or per-user controls.

### 17.3 Input and query safety

The application binds SQL data values and whitelists dynamic sort syntax. IANA zones are validated before PostgreSQL sees them. Route-level clamps limit history page size, artist-list size, and range days.

Remaining input risks include unbounded username and artist strings, arbitrary feedback-triggered candidate expansion, SQL wildcard behavior in search, partially validated date boundaries, extreme numeric years, and the browser filter-label injection path.

### 17.4 Network trust

Public TLS can terminate at an edge tunnel, while the application listens on host loopback. PostgreSQL stays on the private Compose network. The Last.fm adapter itself uses plain HTTP. Secrets enter containers as environment variables. There is no application-layer encryption of stored history or field-level secret store.

### 17.5 Availability abuse

There is no rate limiting, queue bound, request timeout, job timeout, or external-call quota. Public callers can create many users, request forced syncs, compare new accounts, and add seed artists. Process memory limits duplicate same-user work locally, and the database lock limits cross-process scrobble pulls, but neither limits distinct-user work.

## 18. Performance and scaling model

### 18.1 Current strengths

- Local persistence removes Last.fm latency from normal analytical reads.
- Scrobble time and artist indexes match core filters.
- Page commits avoid losing long imports.
- Background refresh keeps reads responsive.
- Query-side aggregation avoids transferring raw histories for charts.
- Recommendation results and metadata are cached.
- Global metadata is shared across users.
- Parallel frontend requests minimize total dashboard latency and tolerate partial failure.

### 18.2 Main growth dimensions

Let:

- `U` be joined users
- `S` be stored scrobbles
- `A` be distinct tagged artists
- `T` be distinct clean tags
- `C` be recommendation candidates per user

Interactive SQL generally scans one user's indexed subset, so it grows with that user's history. The recommendation refresh loads the global tag corpus and compares each user's vector against unplayed artists, roughly growing with `U * A * vector overlap`. Candidate widening and top-track fetching also grow external calls with users and corpus churn.

### 18.3 Likely bottlenecks

1. Opening one PostgreSQL connection for every panel in a wide dashboard fan-out.
2. Many concurrent synchronous query handlers occupying FastAPI threadpool workers.
3. Global recommendation rebuilds in every application scheduler process.
4. Full corpus materialization in Python.
5. No global outbound rate limit or timeout.
6. Offset pagination over increasingly deep histories.
7. Case-insensitive calculations that cannot always use exact text indexes.
8. Query families repeatedly recomputing primary tags and period aggregates.
9. One-file frontend complexity as independent UI state and races grow.

### 18.4 Scaling boundaries and natural next architecture

The current modular monolith is appropriate while one host and one database comfortably serve the workload. The first durable scaling boundary should be background work, not HTTP microservices.

A robust evolution would:

1. Keep the query and API application together.
2. Move scheduler work to one explicit worker service or durable queue.
3. Store job status in PostgreSQL or the queue rather than process memory.
4. Add global Last.fm rate limiting, deadlines, retries, and backoff.
5. Add connection pooling with measured limits.
6. Add schema migrations before scaling replicas.
7. Precompute only aggregates proven expensive by query plans and production measurements.

Splitting every module into a network service would add failure modes without solving the present bottlenecks.

## 19. Important invariants

These statements should remain true through future changes:

1. Only `lastfm.py` speaks the Last.fm protocol.
2. Query modules do not own HTTP policy or connection lifetimes.
3. Routers do not contain analytical SQL.
4. A scrobble page can be imported repeatedly without duplicate stored events under the chosen identity key.
5. The sync watermark advances only after complete pagination.
6. The watermark uses sync start time so mid-sync events are not skipped.
7. Raw tags remain recoverable and cleaning is centralized in one database view.
8. Any metric assigning one genre per play uses a deterministic primary-tag rule.
9. Local calendar metrics and their drill-downs use the same listener time zone.
10. Recommendation candidates exclude played, blocked, and seed artists case-insensitively.
11. A recommendation cache replacement is atomic per user.
12. Background failure cannot erase already committed import or enrichment progress.
13. Static serving is registered after API routes.
14. Production browser requests are same-origin.
15. The health endpoint is interpreted only as liveness unless its implementation changes.

## 20. Design decisions and tradeoffs

### 20.1 PostgreSQL instead of in-memory analytics

The data is durable, naturally relational, ordered in time, and queried through aggregation, windows, and time-zone conversion. PostgreSQL supplies those operations directly and gives retry safety through constraints and transactions. The cost is dependence on a real PostgreSQL environment for meaningful tests and local development.

### 20.2 SQL analytics instead of Python aggregation

SQL keeps filtering and grouping beside indexed data and returns compact chart results. PostgreSQL-specific expressions are accepted because the production data model already depends on PostgreSQL. Query complexity and test setup are the main costs.

### 20.3 Background refresh instead of blocking reads

Serving existing data keeps dashboards responsive during Last.fm slowness or a large initial history. The client must understand eventual freshness, and a single page fan-out can see different committed stages.

### 20.4 Page commits instead of one import transaction

The importer preserves minutes of remote work when a later page fails. Atomic whole-history visibility is sacrificed, but the UI already models incremental progress.

### 20.5 Raw metadata plus a clean view

Storage preserves upstream evidence while the view centralizes evolving product interpretation. Every consumer receives consistent curation. View complexity and runtime work increase, so plan regression tests matter.

### 20.6 Precomputed recommendations instead of request-time scoring

Corpus-wide vector work moves off the latency-sensitive request path. Results can be served cheaply and atomically. They are stale between maintenance passes, and feedback seeds do not affect ranks immediately.

### 20.7 Content-based recommendations instead of collaborative filtering

The system can explain matches through shared tags and does not need a large internal population of users. It inherits Last.fm tag noise, metadata sparsity, and limited novelty. Candidate widening partially breaks the closed-corpus problem without introducing a full collaborative model.

### 20.8 One static document instead of a frontend build system

Deployment is extremely small: copy the application and run it. There are no dependency audits or bundler failures on the client. The cost is a large shared JavaScript scope, limited component isolation, and no existing automated UI test harness.

### 20.9 Liveness-only health

A static health route makes orchestration independent from brief dependency failures and verifies the process can answer HTTP. It cannot protect deployments from a broken database schema or credential, so readiness and public smoke-test claims must remain narrow.

## 21. Known architectural limitations

These are current properties of the implementation, not hypothetical concerns:

### Correctness and semantics

- Scrobble uniqueness omits artist identity.
- Username and several metadata keys use inconsistent exact versus case-folded identity.
- Some search period boundaries do not use the same local-time semantics as charts.
- Primary-genre charts omit untagged plays by definition.
- Listening hours count unknown durations as zero.
- Period change compares consecutive nonempty rows, not consecutive calendar periods.
- Recommendation tie order is not fully deterministic.
- Failed explicit-seed expansion can still be marked complete.
- Direct compatibility with an unknown primary can expose an uncaught not-found exception.

### Reliability and operations

- Last.fm requests have no timeout or retry policy.
- The scheduler has no immediate boot pass, shutdown signal, or durable job state.
- Every application worker starts an independent scheduler.
- Status is process-local and can miss scheduler or other-worker activity.
- The advisory lock covers scrobble pulling but not all maintenance.
- Cache sentinels and successful metadata never expire.
- There is no migration system.
- Health does not test database readiness.
- There is no encoded backup and restore process.
- Deployments rebuild rather than promote an immutable tested artifact.

### Security and product boundaries

- No authentication, authorization, rate limiting, or ownership proof exists.
- The service persists and derives insights from public listening histories without a deletion workflow.
- The Last.fm transport is plain HTTP.
- One frontend path inserts an unescaped search label into HTML.
- Containers run as root and Compose includes a static database credential.

### Test and observability boundaries

- Core SQL is well tested, but external HTTP, background concurrency, frontend behavior, migrations, and deployment are not tested end to end.
- Logs are primarily exception output. There are no structured metrics, traces, job histories, alert definitions, or service-level objectives.
- The liveness endpoint cannot localize dependency failures.

## 22. Interview teaching sequence

Study the system in this order. Each stage depends on the previous one.

### Level 1: Product and request flow

Be able to explain:

- why the application copies Last.fm data locally
- why this is a modular monolith
- how the same FastAPI process serves JSON and the browser application
- why dashboard reads are fast even when refresh is slow
- which data is durable and which state is process-local

### Level 2: Import correctness

Be able to derive:

- why page commits and a final watermark work together
- why the watermark is the sync start time
- how conflict-ignore inserts make overlap safe
- what process-local coalescing solves
- what PostgreSQL advisory locking solves
- what neither mechanism solves across maintenance workers

### Level 3: Analytical semantics

Be able to explain:

- why timestamps stay absolute in storage
- where listener time zones enter the pipeline
- how gaps and islands produce streaks
- how rolling seven-day windows differ from calendar weeks
- why primary-tag charts and full-vector recommendations intentionally use tags differently
- why hours and genre totals may not reconcile with raw plays

### Level 4: Recommendation math

Be able to calculate:

- tag IDF
- normalized artist TF-IDF
- 90-day half-life play weight
- log-compressed artist preference
- cosine similarity
- the equal taste and seed blend

Then explain why unplayed candidates must first exist in the global tag corpus and how Last.fm similar artists create that bridge.

### Level 5: Failure and scaling analysis

Be able to walk through:

- an import failing halfway through pagination
- a process dying during enrichment
- two workers syncing the same user
- two schedulers rebuilding recommendations
- Last.fm hanging without a timeout
- a stale schema surviving a code deploy
- why static health can pass during database failure
- why moving jobs to a durable worker is a better first split than creating many HTTP services

### Level 6: Critical architectural judgment

Be ready to defend both sides of the main choices:

- synchronous framework code versus async clients
- one connection per operation versus pooling
- SQL versus Python analytics
- page durability versus atomic imports
- raw metadata plus read-time cleaning versus destructive cleanup
- scheduled recommendation caches versus live scoring
- framework-free frontend versus component tooling
- public shared analytics versus authenticated ownership

A strong explanation states the current requirement, the invariant protected by the choice, the accepted cost, and the condition that would justify changing it.

## 23. Final mental model

Rotation has three data speeds:

```text
Immediate
  scrobble and feedback writes committed to PostgreSQL

Eventually fresh
  imported history, durations, tags, and top tracks

Batch derived
  candidate corpus effects and cached recommendations
```

It has three major consistency techniques:

```text
uniqueness constraints -> safe replay
transaction boundaries -> visible complete units
advisory locks          -> cross-process pull exclusion
```

It has three intentional interpretation layers:

```text
Last.fm source data
  -> local normalized persistence
  -> clean tags and SQL behavioral metrics
  -> browser presentation and drill-downs
```

The architecture succeeds because each layer has a clear role and the import pipeline treats remote data as fallible, slow, and replayable. Its main future pressure is not a lack of services. It is the absence of durable job coordination, schema migrations, consistent identity normalization, strict remote-call controls, and an ownership boundary for a public write-capable API.
