"""Pure tests for the user-label → PA-sink-name sanitizer."""
from __future__ import annotations

from audio_spider.app import sanitize_pa_name


def test_passes_clean_name_through():
    assert sanitize_pa_name("vmic1") == "vmic1"


def test_replaces_spaces_with_underscores():
    assert sanitize_pa_name("My Virtual Mic") == "My_Virtual_Mic"


def test_collapses_runs_of_separators():
    assert sanitize_pa_name("a -- b") == "a_b"


def test_strips_leading_and_trailing_separators():
    assert sanitize_pa_name("  vmic  ") == "vmic"


def test_empty_falls_back():
    assert sanitize_pa_name("", fallback="vdev") == "vdev"


def test_only_punctuation_falls_back():
    assert sanitize_pa_name("???", fallback="x") == "x"


def test_non_ascii_collapses_to_fallback():
    # PA sink names are ASCII; cyrillic etc. → underscores → fallback
    assert sanitize_pa_name("мікрофон") == "vdev"
