"""Tests for the recommender math (app/recommender.py). Pure functions, so no
database or network: tiny hand-built corpora with expected values worked out
by hand."""

import math

import pytest

from app import recommender as r

# Two artists, one shared tag: idf(rock) = log(2/2) = 0, idf(shoegaze) = log 2.
CORPUS = {"A": {"rock": 100}, "B": {"rock": 50, "shoegaze": 50}}


def test_idf_common_tag_is_zero():
    idf = r.compute_idf(CORPUS)
    assert idf["rock"] == 0
    assert idf["shoegaze"] == pytest.approx(math.log(2))


def test_idf_empty_corpus():
    assert r.compute_idf({}) == {}


def test_artist_vectors_tfidf():
    vecs = r.build_artist_vectors(CORPUS, r.compute_idf(CORPUS))
    # B: tf(shoegaze) = 50/100 = 0.5, times idf log 2
    assert vecs["B"]["shoegaze"] == pytest.approx(0.5 * math.log(2))
    assert vecs["B"]["rock"] == 0  # idf 0 wipes the universal tag


def test_artist_vectors_skip_weightless():
    # An all-zero-weight artist has no usable signal and gets no vector.
    vecs = r.build_artist_vectors({"S": {"rock": 0}}, {"rock": 1.0})
    assert "S" not in vecs


def test_user_vector_log_play_weighting():
    vecs = {"B": {"shoegaze": 2.0}}
    taste = r.build_user_vector({"B": 9}, vecs)  # log(1+9) = log 10
    assert taste["shoegaze"] == pytest.approx(2.0 * math.log(10))


def test_user_vector_skips_unknown_artists():
    assert r.build_user_vector({"nobody": 5}, {}) == {}


def test_cosine_identical_is_one():
    v = {"a": 1.0, "b": 2.0}
    assert r.cosine(v, v) == pytest.approx(1.0)


def test_cosine_no_overlap_is_zero():
    assert r.cosine({"a": 1.0}, {"b": 1.0}) == 0.0


def test_cosine_known_angle():
    # ({x:1, y:1}, {x:1}): dot 1, norms sqrt(2) and 1 -> 1/sqrt(2)
    assert r.cosine({"x": 1.0, "y": 1.0}, {"x": 1.0}) == pytest.approx(1 / math.sqrt(2))


def test_cosine_empty_or_zero_vector():
    assert r.cosine({}, {"a": 1.0}) == 0.0
    assert r.cosine({"a": 0.0}, {"a": 1.0}) == 0.0


def test_recommend_excludes_played_and_zero_scores():
    vecs = {"match": {"rock": 1.0}, "unrelated": {"jazz": 1.0}, "played": {"rock": 1.0}}
    out = r.recommend({"rock": 1.0}, vecs, already_played={"played"})
    assert out == [("match", pytest.approx(1.0))]  # no "unrelated" (score 0), no "played"


def test_recommend_ranks_and_caps():
    user = {"rock": 1.0}
    vecs = {
        "close": {"rock": 1.0},
        "half": {"rock": 1.0, "jazz": 1.0},  # cosine 1/sqrt(2)
        "far": {"rock": 1.0, "jazz": 3.0},
    }
    out = r.recommend(user, vecs, already_played=set(), k=2)
    assert [a for a, _ in out] == ["close", "half"]  # best first, capped at k
    assert out[0][1] > out[1][1]
