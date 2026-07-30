"""Tests for the scrobble search parser (app/queries/analytics.py). Pure string
handling, so no database: parse_search only turns text into a filter dict."""

from app.queries.analytics import parse_search


def test_bare_text_is_free():
    assert parse_search("bohemian")["free"] == ["bohemian"]


def test_field_terms():
    f = parse_search("artist:Logic")
    assert f["artist"] == ["Logic"]
    assert f["free"] == []


def test_quoted_value_keeps_spaces():
    # The whole point of quoting: artist names have spaces.
    assert parse_search('artist:"Tyler, the Creator"')["artist"] == ["Tyler, the Creator"]


def test_apostrophe_does_not_blow_up():
    # shlex raises "No closing quotation" here; the regex tokenizer must not.
    assert parse_search("artist:Guns N' Roses")["artist"] == ["Guns"]


def test_year_parsed_as_int():
    assert parse_search("year:2026")["years"] == [2026]


def test_month_name_abbrev_and_number_all_map_to_one():
    for text in ("month:january", "month:January", "month:jan", "month:1"):
        assert parse_search(text)["months"] == [1], text


def test_combined_terms():
    f = parse_search("artist:Logic year:2026 month:january")
    assert (f["artist"], f["years"], f["months"]) == (["Logic"], [2026], [1])


def test_repeated_fields_accumulate():
    f = parse_search("year:2025 year:2026")
    assert f["years"] == [2025, 2026]


def test_unknown_field_falls_through_to_free_text():
    # Typing a colon must not silently delete part of the query.
    assert parse_search("foo:bar")["free"] == ["foo:bar"]


def test_unparseable_year_falls_through_to_free_text():
    f = parse_search("year:abc")
    assert f["years"] == []
    assert f["free"] == ["year:abc"]


def test_month_out_of_range_falls_through():
    assert parse_search("month:13")["free"] == ["month:13"]


def test_empty_and_none():
    for text in ("", None, "   "):
        f = parse_search(text)
        assert all(v == [] for v in f.values()), text


def test_free_text_alongside_fields():
    f = parse_search("artist:Radiohead creep")
    assert f["artist"] == ["Radiohead"]
    assert f["free"] == ["creep"]


def test_iso_date_parsed():
    assert parse_search("date:2026-07-15")["dates"] == ["2026-07-15"]


def test_impossible_date_falls_through_to_free_text():
    # Feb 31 must not reach the SQL cast (it would 500); it becomes text.
    f = parse_search("date:2026-02-31")
    assert f["dates"] == []
    assert f["free"] == ["date:2026-02-31"]


def test_malformed_date_falls_through():
    f = parse_search("date:july")
    assert f["dates"] == []
    assert f["free"] == ["date:july"]


def test_date_and_part_combine():
    f = parse_search("date:2026-07-15 part:evening")
    assert f["dates"] == ["2026-07-15"]
    assert f["parts"] == [3]
