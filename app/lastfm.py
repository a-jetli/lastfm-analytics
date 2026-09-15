"""every Last.fm call lives here. the only file that talks to the outside."""

import os
import requests
from dotenv import load_dotenv

load_dotenv()
BASE_URL = "http://ws.audioscrobbler.com/2.0"
API_KEY = os.getenv("LASTFM_API_KEY")


class LastfmUserNotFound(Exception):
    """the handle is not a real user (api error 6). kept distinct from a
    transport failure: a typo is permanent and becomes a 404, a network blip is
    transient and gets retried."""


def getrecents(
    username: str, page: int = 1, limit: int = 1000, since: int | None = None
) -> tuple[list, int]:
    """one page of scrobbles as (tracks, total_pages). `since` (unix ts) asks
    only for newer plays, which is what makes the sync incremental. big pages
    are safe: paging follows the totalPages the response reports."""

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
    # "user not found" comes back as a 404 with body {"error":6}. read the body
    # before raise_for_status so it can be told apart from a real transport
    # failure and raised as something non-retryable.
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("error") == 6:
        raise LastfmUserNotFound(username)
    r.raise_for_status()  # anything else is transport, so the caller retries
    data = payload["recenttracks"]  # past the envelope
    total_pages = int(data.get("@attr", {}).get("totalPages", 1))

    raw = data.get("track", [])  # missing entirely when a page has no scrobbles
    if isinstance(raw, dict):
        raw = [raw]  # a single track comes back as a bare object, not a list

    tracks = [
        {
            "name": t["name"],
            "artist": t["artist"]["#text"],
            # empty album -> None, not "". otherwise every album-less single
            # shares "" and the binge query groups them into one fake album.
            "album": t.get("album", {}).get("#text") or None,
            "date": t["date"]["#text"],
            "uts": t["date"]["uts"],
        }
        for t in raw
        # the now-playing track has no date key yet. skip it, the next sync
        # picks it up once it is logged.
        if "date" in t
    ]
    return tracks, total_pages


def get_track_info(artist: str, track: str) -> int:
    """track length in ms from track.getInfo.

    returns 0 when Last.fm has no duration, which is common. stored as-is so we
    do not re-ask. raises only on a transport failure, which the backfill
    catches and retries next pass.
    """
    params = {
        "method": "track.getInfo",
        "artist": artist,
        "track": track,
        "autocorrect": 1,  # let Last.fm fix minor spelling to match
        "api_key": API_KEY,
        "format": "json",
    }
    r = requests.get(BASE_URL, params=params)
    data = r.json()
    # a missing "track" key means Last.fm does not know it. not a transport
    # failure, there is just no duration, so store 0.
    if "track" not in data:
        return 0
    return int(data["track"].get("duration") or 0)


def get_artist_tags(artist: str) -> list[tuple[str, int]]:
    """genre tags for an artist from artist.getTopTags.

    (tag, weight) pairs, where weight is Last.fm's 0-100 count. [] when there
    are none or the artist is unknown, and the caller stores a sentinel so it is
    not re-fetched. tags are raw here; blocklist and alias cleaning happen at
    read time.
    """
    params = {
        "method": "artist.getTopTags",
        "artist": artist,
        "autocorrect": 1,  # let Last.fm fix minor spelling to match
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
    """artists Last.fm considers similar to `artist`, best match first.

    the only source of artists nobody here has played. everything else in the
    corpus arrives through scrobbles, so at low user counts candidates ("tagged
    artists you have not played") are empty by construction.

    [] if there are no similars. raises only on transport failure.
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
    """an artist's globally most-played tracks, best first, from
    artist.getTopTracks. [] when the artist is unknown, and the caller stores a
    sentinel so it is not re-fetched. raises only on transport failure."""
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