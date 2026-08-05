"""Date parsing, air-window logic, movie-window nuance."""
from __future__ import annotations

from datetime import date

from src.edge_cases import date_ranges_overlap, entry_window, parse_date


def test_parse_date_valid_and_none():
    assert parse_date("2020-01-15") == date(2020, 1, 15)
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("garbage") is None


def test_parse_date_iso_with_time():
    # Jikan dates may carry a time/zone suffix.
    assert parse_date("2020-01-15T00:00:00+00:00") == date(2020, 1, 15)


def test_entry_window_movie_closes_null_end():
    """A Movie with null aired_to gets a single-day window, not open-ended."""
    start, end = entry_window({"type": "Movie", "episodes": 1,
                               "aired_from": "2020-10-16", "aired_to": None})
    assert start == date(2020, 10, 16)
    assert end == date(2020, 10, 16)


def test_entry_window_tv_keeps_null_end_open():
    """A currently-airing TV series keeps an open end (None)."""
    start, end = entry_window({"type": "TV", "episodes": 12,
                               "aired_from": "2026-01-01", "aired_to": None})
    assert start == date(2026, 1, 1)
    assert end is None


def test_date_ranges_overlap():
    a = (date(2020, 1, 1), date(2020, 6, 1))
    assert date_ranges_overlap(*a, date(2020, 3, 1), date(2020, 9, 1)) is True
    assert date_ranges_overlap(*a, date(2021, 1, 1), date(2021, 6, 1)) is False


def test_date_ranges_overlap_missing_start():
    assert date_ranges_overlap(None, None, date(2020, 1, 1), date(2020, 6, 1)) is False
