"""Artist recommender math: pure functions over sparse dicts {tag: value}, no
numpy, no DB (reads/caching live in sync_service + queries/recommend.py).

The idea: each artist is a vector over genre tags, a user's taste is the sum
of their played artists' vectors (each weighted by how much and how recently
it's been played, see get_user_plays), and unplayed artists are
ranked by cosine similarity (direction of taste, not volume). TF-IDF discounts
tags that are on every artist so distinctive matches ("shoegaze") outweigh
generic ones ("rock").
"""

import math
from collections import defaultdict


def compute_idf(corpus: dict[str, dict[str, float]]) -> dict[str, float]:
    """idf per tag = log(total_artists / artists_carrying_the_tag). Common tag,
    low idf; rare tag, high; a tag on every artist gets 0 and drops out.
    `corpus` maps artist -> {tag: weight}."""
    n_artists = len(corpus)
    doc_freq: dict[str, int] = defaultdict(int)
    for tags in corpus.values():
        for tag in tags:
            doc_freq[tag] += 1
    return {tag: math.log(n_artists / freq) for tag, freq in doc_freq.items()}


def build_artist_vectors(
    corpus: dict[str, dict[str, float]], idf: dict[str, float]
) -> dict[str, dict[str, float]]:
    """Each artist's raw tag weights -> a TF-IDF vector {tag: tf * idf}, where
    TF = weight / the artist's total weight (so artists with many tags are on
    the same scale as artists with few)."""
    vectors: dict[str, dict[str, float]] = {}
    for artist, tags in corpus.items():
        total = sum(tags.values())
        if total == 0:
            continue  # weightless or sentinel-only artist, nothing to compare
        vectors[artist] = {
            tag: (weight / total) * idf[tag] for tag, weight in tags.items()
        }
    return vectors


def build_user_vector(
    plays: dict[str, float], artist_vectors: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Sum a user's played artists into one taste vector, each scaled by
    log(1 + play_score) so a few obsessions don't drown out the rest. Artists we
    have no vector for are skipped.

    `play_score` is per artist. It's a raw count in the unit tests, but in
    production it's the recency-weighted score from get_user_plays, so the vector
    reflects current taste. The log compression works the same on either, it just
    tempers heavy values."""
    taste: dict[str, float] = defaultdict(float)
    for artist, play_score in plays.items():
        vec = artist_vectors.get(artist)
        if not vec:
            continue
        weight = math.log(1 + play_score)
        for tag, value in vec.items():
            taste[tag] += weight * value
    return dict(taste)


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """Cosine similarity of two sparse vectors: dot / (|a| * |b|). Returns 0
    if either vector is empty or zero-length."""
    if not a or not b:
        return 0.0
    # dot product = sum of a[tag]*b[tag] over the tags both vectors share
    dot = 0.0
    for tag in a:
        if tag in b:
            dot += a[tag] * b[tag]
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# How much of the ranking a "more like this" pick takes over, when there is one.
# Half, because a seed has to actually move the list to mean anything: folded
# into the taste vector as one more artist it moved scores by 0.002 against a
# history of hundreds, which is not a feature. At 0.5 the list is half "your
# taste" and half "the direction you pointed", and dropping the seed puts it
# straight back. A knob.
SEED_WEIGHT = 0.5


def recommend(
    user_vector: dict[str, float],
    artist_vectors: dict[str, dict[str, float]],
    already_played: set[str],
    k: int = 20,
    seed_vector: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """Top k unplayed artists as (artist, cosine score), best first. Zero
    scores (no tag overlap) are dropped rather than used as filler.

    `seed_vector` is the artists the user explicitly asked for more of, summed
    the same way a taste vector is. When present, a candidate is scored against
    both directions and the two are blended, so the list bends toward the pick
    without abandoning the play history. When absent, this is plain cosine
    against taste and nothing changes.

    The exclusion is case-insensitive. Both sides are canonicalised upstream, but
    they are canonicalised from different tables (scrobbles vs artist_tags), so
    matching on the exact string means one disagreement puts an artist the user
    already plays back into their recommendations. Cheap belt and braces.
    """
    played = {name.lower() for name in already_played}
    scored = []
    for artist, vec in artist_vectors.items():
        if artist.lower() in played:
            continue
        score = cosine(user_vector, vec)
        if seed_vector:
            score = ((1 - SEED_WEIGHT) * score
                     + SEED_WEIGHT * cosine(seed_vector, vec))
        if score > 0:
            scored.append((artist, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]
