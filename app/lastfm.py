"""All Last.fm API calls live here -- the only file that talks to the outside."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = "http://ws.audioscrobbler.com/2.0"
API_KEY = os.getenv("LASTFM_API_KEY")


def getrecents(
    username: str, page: int = 1, limit: int = 1000, since: int | None = None
) -> tuple[list, int]:
    """One page of scrobbles as (tracks, total_pages). `since` (unix ts) asks
    only for newer plays, which is what makes incremental sync possible. Big
    pages are safe: pagination follows the totalPages the response reports."""

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
    r.raise_for_status()  # crash codes
    data = r.json()["recenttracks"]  # dig past Last.fm's envelope
    total_pages = int(data.get("@attr", {}).get("totalPages", 1))

    raw = data.get("track", [])  # missing entirely when a page has no scrobbles
    if isinstance(raw, dict):
        raw = [raw]  # Last.fm returns a bare object (not a list) for a single track

    tracks = [
        {
            "name": t["name"],
            "artist": t["artist"]["#text"],
            # Empty album text -> None (not ""), so "no album" is one honest value.
            # Otherwise every album-less single shares the "" album and the binge
            # query would group them as one fake album.
            "album": t.get("album", {}).get("#text") or None,
            "date": t["date"]["#text"],
            "uts": t["date"]["uts"],
        }
        for t in raw
        # The "now playing" track has no `date` key yet -- skip it, or t["date"]
        # would KeyError. It'll be picked up on the next sync once it's logged.
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
    # A missing "track" key means a Last.fm-level error (e.g. track not found).
    # That's not a transport failure -- there's just no duration, so store 0.
    if "track" not in data:
        return 0
    return int(data["track"].get("duration") or 0)


def get_artist_tags(artist: str) -> list[tuple[str, int]]:
    """Top genre/folksonomy tags for an artist from artist.getTopTags.

    Returns a list of (tag, weight) where weight is Last.fm's 0-100 popularity
    "count". Returns [] when Last.fm has no tags for the artist (or doesn't know
    them); the caller stores a sentinel so it isn't re-fetched. Raises only on a
    real network/HTTP transport failure, which the backfill catches and retries.
    Tags are raw here -- blocklist/alias cleaning happens at read time.
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