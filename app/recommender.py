"""recommender math: pure functions over sparse {tag: value} dicts. no numpy,
no db. reads and caching live in sync_service and queries/recommend.py.

each artist is a vector over genre tags. a user's taste is the sum of their
played artists' vectors, weighted by how much and how recently they played them
(see get_user_plays). unplayed artists rank by cosine, so direction of taste
rather than volume. tf-idf discounts tags everyone carries, so "shoegaze"
outweighs "rock".
"""

import math
from collections import defaultdict


def compute_idf(corpus: dict[str, dict[str, float]]) -> dict[str, float]:
    """idf per tag = log(total_artists / artists_with_the_tag).

    common tag low, rare tag high, a tag on every artist gets 0 and drops out.
    corpus maps artist -> {tag: weight}."""
    n_artists = len(corpus)
    doc_freq: dict[str, int] = defaultdict(int)
    for tags in corpus.values():
        for tag in tags:
            doc_freq[tag] += 1
    return {tag: math.log(n_artists / freq) for tag, freq in doc_freq.items()}


def build_artist_vectors(
    corpus: dict[str, dict[str, float]], idf: dict[str, float]
) -> dict[str, dict[str, float]]:
    """raw tag weights -> a tf-idf vector {tag: tf * idf}.

    tf is weight over the artist's total weight, so artists with many tags sit
    on the same scale as artists with few."""
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
    """sum a user's played artists into one taste vector, each scaled by
    log(1 + play_score) so a few obsessions don't drown the rest. artists with
    no vector are skipped.

    play_score is a raw count in tests and the recency-weighted score from
    get_user_plays in production. the log tempers heavy values either way."""
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
    """cosine of two sparse vectors, dot / (|a| * |b|). 0 if either is empty."""
    if not a or not b:
        return 0.0
    # dot product over the tags both vectors share
    dot = 0.0
    for tag in a:
        if tag in b:
            dot += a[tag] * b[tag]
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# how much of the ranking a "more like this" pick takes over. half, because a
# seed has to actually move the list: folded in as one more artist it shifted
# scores by 0.002 against a history of hundreds. at 0.5 the list is half taste,
# half the direction you pointed, and dropping the seed puts it straight back.
SEED_WEIGHT = 0.5


def recommend(
    user_vector: dict[str, float],
    artist_vectors: dict[str, dict[str, float]],
    already_played: set[str],
    k: int = 20,
    seed_vector: dict[str, float] | None = None,
) -> list[tuple[str, float]]:
    """top k unplayed artists as (artist, score), best first. zero scores mean
    no tag overlap and are dropped rather than used as filler.

    seed_vector is what the user asked for more of, summed like a taste vector.
    when present a candidate is scored against both directions and blended, so
    the list bends toward the pick without abandoning the history.

    the exclusion is case-insensitive. both sides are canonicalised upstream but
    from different tables (scrobbles vs artist_tags), so matching exact strings
    would let one disagreement recommend an artist the user already plays.
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
