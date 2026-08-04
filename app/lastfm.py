"""All Last.fm API calls live here, the only file that talks to the outside."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = "http://ws.audioscrobbler.com/2.0"
API_KEY = os.getenv("LASTFM_API_KEY")


class LastfmUserNotFound(Exception):
    """The handle isn't a real Last.fm user (API error 6). Distinct from a
    transport/HTTP failure: a typo is permanent and should surface as a 404,
    whereas a network blip is transient and should be retried."""


def getrecents(
    username: str, page: int = 1, limit: int = 1000, since: int | None = None
) -> tuple[list, int]:
    """One page of scrobbles as (tracks, total_pages). `since` (unix ts) asks
    only for newer plays, which is what makes incremental sync possible. Big
    pages are safe: pagination follows the totalPages the response reports.
    Raises LastfmUserNotFound for an unknown handle."""

    params = {
        "method": "user.getRecentTracks",
        "limit": limit,
        "page": page,
        "user": username,
        "api_key": API_KEY,
        "format": "json",
    }
    if since is not None:
        params["from"] = since

    r = requests.get(BASE_URL, params=params)
    # Last.fm signals "user not found" as a 404 with a JSON body {"error":6}. read
    # the body before raise_for_status so we can tell it apart from a real
    # transport failure (500, timeout) and raise something non-retryable.
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error") == 6:
        raise LastfmUserNotFound(username)
    r.raise_for_status()  # any other HTTP error is transport-ish -> caller retries
    data = payload["recenttracks"]  # dig past Last.fm's envelope
    total_pages = int(data.get("@attr", {}).get("totalPages", 1))

    raw = data.get("track", [])  # missing entirely when a page has no scrobbles
    if isinstance(raw, dict):
        raw = [raw]  # Last.fm returns a bare object (not a list) for a single track

    tracks = [
        {
            "name": t["name"],
            "artist": t["artist"]["#text"],
            # empty album text -> None, not "", so "no album" is one honest value.
            # otherwise every album-less single shares the "" album and the binge
            # query groups them into one fake album.
            "album": t.get("album", {}).get("#text") or None,
            "date": t["date"]["#text"],
            "uts": t["date"]["uts"],
        }
        for t in raw
        # the "now playing" track has no `date` key yet, so skip it or t["date"]
        # KeyErrors. next sync picks it up once it's logged.
        if "date" in t
    ]
    return tracks, total_pages


def get_track_info(artist: str, track: str) -> int:
    """Track length in milliseconds from track.getInfo.

    Returns 0 when Last.fm has no duration for the track (common, and stored
    as-is so we don't re-ask every night). Raises only on a real network/HTTP
    transport failure, which the backfill catches and retries on the next pass.
    """
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": track,
        "autocorrect": 1,  # let Last.fm fix minor artist/track spelling to match
        "api_key": API_KEY,
        "format": "json",
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()
    # a missing "track" key is a Last.fm-level error, e.g. track not found. not a
    # transport failure, there's just no duration, so store 0.
    if "track" not in data:
        return 0
    return int(data["track"].get("duration") or 0)


def get_artist_tags(artist: str) -> list[tuple[str, int]]:
    """Top genre/folksonomy tags for an artist from artist.getTopTags.

    Returns a list of (tag, weight) where weight is Last.fm's 0-100 popularity
    "count". Returns [] when Last.fm has no tags for the artist (or doesn't know
    them); the caller stores a sentinel so it isn't re-fetched. Raises only on a
    real network/HTTP transport failure, which the backfill catches and retries.
    Tags are raw here, blocklist/alias cleaning happens at read time.
    """
    params = {
        "method": "artist.getTopTags",
        "artist": artist,
        "autocorrect": 1,  # let Last.fm fix minor artist spelling to match
        "api_key": API_KEY,
        "format": "json",
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()
    raw = data.get("toptags", {}).get("tag", [])
    if isinstance(raw, dict):
        raw = [raw]  # a single tag comes back as a bare object, not a list
    return [(t["name"], int(t.get("count") or 0)) for t in raw if t.get("name")]


def get_similar_artists(artist: str, limit: int = 10) -> list[str]:
    """Artist names Last.fm considers similar to `artist`, best match first.

    The only source of artists nobody here has played. Everything else in the
    corpus comes from scrobbles, which means with few users the recommender has
    almost nothing to recommend: candidates are "tagged artists you have NOT
    played", and with one user that set is empty by construction.

    Returns [] if Last.fm has no similars. Raises only on transport failure,
    which the caller catches and retries next pass.
    """
    params = {
        "method": "artist.getSimilar",
        "artist": artist,
        "limit": limit,
        "autocorrect": 1,
        "api_key": API_KEY,
        "format": "json",
    }
    r = requests.get(BASE_URL, params=params)
    raw = r.json().get("similarartists", {}).get("artist", [])
    if isinstance(raw, dict):
        raw = [raw]  # a single result comes back as a bare object, not a list
    return [a["name"] for a in raw if a.get("name")]


def get_artist_top_tracks(artist: str, limit: int = 10) -> list[str]:
    """An artist's most-played tracks (globally, per Last.fm), best first, from
    artist.getTopTracks. Returns [] when Last.fm doesn't know the artist; the
    caller stores a sentinel so it isn't re-fetched. Raises only on a real
    transport failure, which the backfill catches and retries next pass."""
    params = {
        "method": "artist.getTopTracks",
        "artist": artist,
        "limit": limit,
        "autocorrect": 1,
        "api_key": API_KEY,
        "format": "json",
    }
    r = requests.get(BASE_URL, params=params)
    raw = r.json().get("toptracks", {}).get("track", [])
    if isinstance(raw, dict):
        raw = [raw]  # single track comes back as a bare object, not a list
    return [t["name"] for t in raw if t.get("name")]


if __name__ == "__main__":
    output, total_pages = getrecents("i-sleep", limit=10)
    print(f"total pages available: {total_pages}")
    for t in output:
        print(f"{t['uts']}  -  {t['date']}  -  {t['artist']}  -  {t['name']}")