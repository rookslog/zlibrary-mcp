"""Tests for the symmetric source-capability vocabulary (#96, #101, #107).

The two rules under test are the ones a plausible refactor breaks silently:
every source carries the same key set, and a daily limit is three-valued
rather than nullable.
"""

import pytest

from lib.sources.capabilities import (
    KNOWN_SOURCES,
    LIMIT_KNOWN,
    LIMIT_NONE,
    LIMIT_NOT_APPLICABLE,
    LIMIT_UNKNOWN,
    SOURCE_ANNAS,
    SOURCE_LIBGEN,
    SOURCE_ZLIBRARY,
    canonical_source,
    describe_sources,
    known_daily_limit,
    resolve_requested_sources,
)
from lib.sources.config import SourceConfig

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_zlibrary_credentials(monkeypatch):
    """Default to a credential-free environment; tests opt in explicitly."""
    monkeypatch.delenv("ZLIBRARY_EMAIL", raising=False)
    monkeypatch.delenv("ZLIBRARY_PASSWORD", raising=False)


@pytest.fixture
def zlibrary_credentials(monkeypatch):
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.com")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "hunter2")


class TestSourceNames:
    def test_selector_spellings_map_to_canonical_names(self):
        """`source="annas"` and the report's `annas_archive` are one source."""
        assert canonical_source("annas") == SOURCE_ANNAS
        assert canonical_source("ANNAS_ARCHIVE") == SOURCE_ANNAS
        assert canonical_source("libgen") == SOURCE_LIBGEN
        assert canonical_source("z-library") == SOURCE_ZLIBRARY

    def test_unknown_source_names_itself_and_the_alternatives(self):
        with pytest.raises(ValueError, match="Unknown source 'gutenberg'"):
            canonical_source("gutenberg")

    def test_omitted_selection_means_every_source(self):
        assert resolve_requested_sources(None) == list(KNOWN_SOURCES)

    def test_selection_is_deduplicated_and_ordered(self):
        assert resolve_requested_sources(["libgen", "annas", "libgen"]) == [
            SOURCE_ANNAS,
            SOURCE_LIBGEN,
        ]

    def test_explicitly_empty_selection_is_rejected_not_widened(self):
        """Widening 'nothing' to 'everything' would spend the round-trip the
        caller was trying to avoid."""
        with pytest.raises(ValueError, match="at least one source"):
            resolve_requested_sources([])


class TestSymmetry:
    """Invariant 4: report, never rank. No source gets a field the others lack."""

    def test_every_source_carries_the_same_key_set(self, zlibrary_credentials):
        entries = describe_sources(SourceConfig(annas_secret_key="k"))
        assert set(entries) == set(KNOWN_SOURCES)
        shapes = {name: sorted(entry) for name, entry in entries.items()}
        assert len(set(map(tuple, shapes.values()))) == 1, shapes
        assert shapes[SOURCE_LIBGEN] == [
            "available",
            "daily_limit",
            "note",
            "routes",
        ]

    def test_key_set_is_identical_with_and_without_credentials(self):
        """An unconfigured source reports the same fields, not fewer."""
        keyed = describe_sources(SourceConfig(annas_secret_key="k"))
        unkeyed = describe_sources(SourceConfig(annas_secret_key=""))
        assert sorted(keyed[SOURCE_ANNAS]) == sorted(unkeyed[SOURCE_ANNAS])
        for entry in list(keyed.values()) + list(unkeyed.values()):
            assert sorted(entry["daily_limit"]) == [
                "note",
                "remaining",
                "state",
                "total",
                "used",
            ]


class TestLimitStates:
    """Three facts, three states. Collapsing them onto null is the bug."""

    def test_libgen_reports_no_limit_rather_than_unknown(self):
        entry = describe_sources(SourceConfig())[SOURCE_LIBGEN]
        assert entry["daily_limit"]["state"] == LIMIT_NONE
        assert entry["daily_limit"]["total"] is None
        assert entry["routes"] == ["search", "download"]

    def test_zlibrary_limit_is_unknown_locally_and_says_what_would_answer(
        self, zlibrary_credentials
    ):
        entry = describe_sources(SourceConfig())[SOURCE_ZLIBRARY]
        assert entry["available"] is True
        assert entry["daily_limit"]["state"] == LIMIT_UNKNOWN
        assert "get_download_limits" in entry["daily_limit"]["note"]

    def test_zlibrary_without_credentials_is_unavailable_not_unknown(self):
        entry = describe_sources(SourceConfig())[SOURCE_ZLIBRARY]
        assert entry["available"] is False
        assert entry["routes"] == []
        assert entry["daily_limit"]["state"] == LIMIT_NOT_APPLICABLE

    def test_unkeyed_annas_is_search_only_and_still_available(self):
        """#107: search-only, reported without erroring."""
        entry = describe_sources(SourceConfig(annas_secret_key=""))[SOURCE_ANNAS]
        assert entry["available"] is True
        assert entry["routes"] == ["search"]
        assert entry["daily_limit"]["state"] == LIMIT_NOT_APPLICABLE
        assert "ANNAS_SECRET_KEY" in entry["note"]

    def test_keyed_annas_gains_the_download_route(self):
        entry = describe_sources(SourceConfig(annas_secret_key="k"))[SOURCE_ANNAS]
        assert entry["routes"] == ["search", "download"]
        assert entry["daily_limit"]["state"] == LIMIT_UNKNOWN

    def test_a_concrete_number_is_distinguishable_from_both(self):
        limit = known_daily_limit(10, 3, 7)
        assert limit["state"] == LIMIT_KNOWN
        assert (limit["total"], limit["used"], limit["remaining"]) == (10, 3, 7)

    def test_the_four_states_are_distinct(self):
        assert len({LIMIT_NONE, LIMIT_KNOWN, LIMIT_UNKNOWN, LIMIT_NOT_APPLICABLE}) == 4


class TestNoNetwork:
    def test_describing_sources_opens_no_connection(self, monkeypatch):
        """`routing` rides along with every search, so it must stay local."""
        import socket

        def refuse(*args, **kwargs):
            raise AssertionError("describe_sources must not touch the network")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)
        assert set(describe_sources(SourceConfig())) == set(KNOWN_SOURCES)
