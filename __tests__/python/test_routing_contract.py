"""The source/route reporting contract decided on #96, built by #101 and #107.

Three surfaces, one contract:

- `search_multi_source` carries a `routing` block of purely local facts;
- acquisition returns nested `provenance` naming what actually served the file;
- `get_download_limits` reports every source, and costs a round-trip only for
  the source that has one.

The test that matters most is
`test_a_fallback_to_another_source_is_visible_in_the_response`. #74 fixed the
routing half of that bug and left the reporting half open: a caller could ask
for Anna's, receive LibGen, and have nothing in the response say so.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
)

import python_bridge  # noqa: E402
from lib.sources.capabilities import (  # noqa: E402
    LIMIT_KNOWN,
    LIMIT_NONE,
    LIMIT_NOT_APPLICABLE,
    LIMIT_UNKNOWN,
)
from lib.sources.config import SourceConfig  # noqa: E402
from lib.sources.models import (  # noqa: E402
    DownloadResult,
    SourceType,
    UnifiedBookResult,
)
from lib.sources.router import SourceRouter  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """No credentials and no cached router leak between tests."""
    for name in ("ZLIBRARY_EMAIL", "ZLIBRARY_PASSWORD", "ANNAS_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(python_bridge, "_source_router", None)
    monkeypatch.setattr(python_bridge, "_eapi_client", None)


@pytest.fixture
def zlibrary_credentials(monkeypatch):
    monkeypatch.setenv("ZLIBRARY_EMAIL", "reader@example.com")
    monkeypatch.setenv("ZLIBRARY_PASSWORD", "hunter2")


def book(md5: str, source: SourceType) -> UnifiedBookResult:
    return UnifiedBookResult(md5=md5, title="A Book", source=source)


def install_router(monkeypatch, results, config=None):
    """Install a real SourceRouter whose adapters are replaced by a stub search.

    Real, not a SimpleNamespace: `routing.fell_back` is computed from the
    router's own idea of which provider it meant to reach, so a fake that
    invents that answer would test nothing.
    """
    router = SourceRouter(config or SourceConfig())
    monkeypatch.setattr(router, "search", AsyncMock(return_value=results))
    monkeypatch.setattr(python_bridge, "_source_router", router)
    return router


class TestRoutingBlock:
    @pytest.mark.asyncio
    async def test_a_fallback_to_another_source_is_visible_in_the_response(
        self, monkeypatch
    ):
        """Asking for Anna's and receiving LibGen must show up in `routing`.

        This is the bug the whole contract exists to prevent. `sources_used`
        was derived from results, so a substitution was indistinguishable from
        a direct hit — the reporting half of #74.
        """
        install_router(monkeypatch, [book("a" * 32, SourceType.LIBGEN)])

        result = await python_bridge.search_multi_source("hegel", source="annas")

        routing = result["routing"]
        assert routing["requested"] == "annas"
        assert routing["served_by"] == ["libgen"]
        assert routing["fell_back"] is True

    @pytest.mark.asyncio
    async def test_auto_served_by_its_first_choice_is_not_a_fallback(self, monkeypatch):
        install_router(
            monkeypatch,
            [book("b" * 32, SourceType.ANNAS_ARCHIVE)],
            SourceConfig(annas_secret_key="k"),
        )

        result = await python_bridge.search_multi_source("hegel", source="auto")

        assert result["routing"]["served_by"] == ["annas_archive"]
        assert result["routing"]["fell_back"] is False

    @pytest.mark.asyncio
    async def test_auto_falling_past_its_first_choice_is_a_fallback(self, monkeypatch):
        """With a key, `auto` means Anna's first. LibGen answering is a
        substitution, and the caller who asked for `auto` still gets told."""
        install_router(
            monkeypatch,
            [book("c" * 32, SourceType.LIBGEN)],
            SourceConfig(annas_secret_key="k"),
        )

        result = await python_bridge.search_multi_source("hegel", source="auto")

        assert result["routing"]["fell_back"] is True

    @pytest.mark.asyncio
    async def test_auto_without_a_key_prefers_libgen_and_reports_no_fallback(
        self, monkeypatch
    ):
        install_router(monkeypatch, [book("d" * 32, SourceType.LIBGEN)])

        result = await python_bridge.search_multi_source("hegel", source="auto")

        assert result["routing"]["fell_back"] is False

    @pytest.mark.asyncio
    async def test_no_results_is_not_reported_as_a_fallback(self, monkeypatch):
        """Nothing served means nothing was substituted; calling that a
        fallback would invent an event that did not happen."""
        install_router(monkeypatch, [])

        result = await python_bridge.search_multi_source("hegel", source="annas")

        assert result["books"] == []
        assert result["routing"]["served_by"] == []
        assert result["routing"]["fell_back"] is False

    @pytest.mark.asyncio
    async def test_routing_is_nested_and_cannot_collide_with_book_fields(
        self, monkeypatch
    ):
        """`**r.extra` is spread into each book dict, so a source-supplied
        `source` or `size` would collide with a flat contract field (#96)."""
        result_with_extra = UnifiedBookResult(
            md5="e" * 32,
            title="A Book",
            source=SourceType.LIBGEN,
            size="4 MB",
            extra={"also_available_on": ["lgli"]},
        )
        install_router(monkeypatch, [result_with_extra])

        result = await python_bridge.search_multi_source("hegel", source="libgen")

        assert set(result) == {"books", "routing"}
        assert result["books"][0]["source"] == "libgen"
        assert result["books"][0]["size"] == "4 MB"
        assert result["books"][0]["also_available_on"] == ["lgli"]

    @pytest.mark.asyncio
    async def test_sources_used_is_superseded_by_served_by(self, monkeypatch):
        """#96 supersedes it: derived from results, it could not show a
        fallback, which was the one thing callers needed from it."""
        install_router(monkeypatch, [book("f" * 32, SourceType.LIBGEN)])

        result = await python_bridge.search_multi_source("hegel", source="libgen")

        assert "sources_used" not in result

    @pytest.mark.asyncio
    async def test_every_source_is_described_symmetrically(self, monkeypatch):
        install_router(monkeypatch, [])

        sources = (await python_bridge.search_multi_source("hegel"))["routing"][
            "sources"
        ]

        assert set(sources) == {"annas_archive", "libgen", "zlibrary"}
        shapes = {tuple(sorted(entry)) for entry in sources.values()}
        assert len(shapes) == 1, sources

    @pytest.mark.asyncio
    async def test_routing_costs_no_network_call(self, monkeypatch):
        """The inline half of the contract is local by definition (#96)."""
        install_router(monkeypatch, [])
        monkeypatch.setattr(
            python_bridge,
            "initialize_eapi_client",
            AsyncMock(side_effect=AssertionError("routing must not log in")),
        )

        result = await python_bridge.search_multi_source("hegel")

        assert result["routing"]["sources"]["zlibrary"]["daily_limit"]["state"] == (
            LIMIT_NOT_APPLICABLE
        )


class TestPerSourceDownloadLimits:
    @pytest.mark.asyncio
    async def test_a_libgen_only_question_makes_no_eapi_call(self, monkeypatch):
        """The whole point of the `sources` parameter (#107). A LibGen answer
        is knowable from configuration, so paying for a Z-Library profile
        fetch to produce it buys a symmetry nobody benefits from."""
        monkeypatch.setattr(
            python_bridge,
            "initialize_eapi_client",
            AsyncMock(side_effect=AssertionError("EAPI login attempted")),
        )
        monkeypatch.setattr(
            python_bridge,
            "get_eapi_client",
            AsyncMock(side_effect=AssertionError("EAPI client requested")),
        )

        result = await python_bridge.get_download_limits(sources=["libgen"])

        assert result["requested"] == ["libgen"]
        assert set(result["sources"]) == {"libgen"}
        assert result["sources"]["libgen"]["daily_limit"]["state"] == LIMIT_NONE

    @pytest.mark.asyncio
    async def test_the_dispatcher_does_not_force_a_login_for_this_tool(self):
        """Z-Library's entry logs in lazily and degrades to its own `unknown`,
        so an auth outage costs one entry rather than the whole tool."""
        assert python_bridge._requires_eapi_client("get_download_limits", {}) is False

    @pytest.mark.asyncio
    async def test_every_source_is_reported_by_default(self, monkeypatch):
        result = await python_bridge.get_download_limits()

        assert result["requested"] == ["annas_archive", "libgen", "zlibrary"]
        assert set(result["sources"]) == {"annas_archive", "libgen", "zlibrary"}

    @pytest.mark.asyncio
    async def test_the_three_limit_states_are_never_collapsed(self, monkeypatch):
        """No limit exists / not knowable here / a concrete number."""
        result = await python_bridge.get_download_limits()
        states = {
            name: entry["daily_limit"]["state"]
            for name, entry in result["sources"].items()
        }

        assert states["libgen"] == LIMIT_NONE
        assert states["annas_archive"] == LIMIT_NOT_APPLICABLE
        assert states["zlibrary"] == LIMIT_NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_zlibrary_reports_concrete_numbers_from_the_profile(
        self, monkeypatch, zlibrary_credentials
    ):
        eapi = SimpleNamespace(
            get_profile=AsyncMock(
                return_value={
                    "user": {
                        "downloads_today": 3,
                        "downloads_limit": 10,
                        "isPremium": 0,
                    }
                }
            )
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        entry = (await python_bridge.get_download_limits(sources=["zlibrary"]))[
            "sources"
        ]["zlibrary"]

        assert entry["daily_limit"]["state"] == LIMIT_KNOWN
        assert entry["daily_limit"]["total"] == 10
        assert entry["daily_limit"]["used"] == 3
        assert entry["daily_limit"]["remaining"] == 7
        assert entry["details"]["is_premium"] is False

    @pytest.mark.asyncio
    async def test_zlibrary_remaining_is_clamped_at_zero(
        self, monkeypatch, zlibrary_credentials
    ):
        """The server counts a download when issued and can exceed the cap."""
        eapi = SimpleNamespace(
            get_profile=AsyncMock(
                return_value={"user": {"downloads_today": 12, "downloads_limit": 10}}
            )
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        entry = (await python_bridge.get_download_limits(sources=["zlibrary"]))[
            "sources"
        ]["zlibrary"]

        assert entry["daily_limit"]["remaining"] == 0

    @pytest.mark.asyncio
    async def test_a_known_cap_with_an_unknown_spend_reports_no_remainder(
        self, monkeypatch, zlibrary_credentials
    ):
        """The cap is known and the spend is not, so the remainder is not
        derivable. Reporting the full cap would fabricate a number."""
        eapi = SimpleNamespace(
            get_profile=AsyncMock(return_value={"user": {"downloads_limit": 10}})
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        limit = (await python_bridge.get_download_limits(sources=["zlibrary"]))[
            "sources"
        ]["zlibrary"]["daily_limit"]

        assert limit["state"] == LIMIT_KNOWN
        assert limit["total"] == 10
        assert limit["used"] is None
        assert limit["remaining"] is None
        assert "downloads_today" in limit["note"]

    @pytest.mark.asyncio
    async def test_a_renamed_profile_field_degrades_to_unknown_not_a_number(
        self, monkeypatch, zlibrary_credentials
    ):
        eapi = SimpleNamespace(
            get_profile=AsyncMock(return_value={"user": {"something_else": 5}})
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        entry = (await python_bridge.get_download_limits(sources=["zlibrary"]))[
            "sources"
        ]["zlibrary"]

        assert entry["daily_limit"]["state"] == LIMIT_UNKNOWN
        assert entry["daily_limit"]["total"] is None
        assert "downloads_limit" in entry["daily_limit"]["note"]

    @pytest.mark.asyncio
    async def test_a_zlibrary_outage_costs_one_entry_not_the_whole_call(
        self, monkeypatch, zlibrary_credentials
    ):
        """A credential-free-by-outage Z-Library must not take LibGen's
        answer down with it — the failure #129 fixed for search."""
        eapi = SimpleNamespace(
            get_profile=AsyncMock(side_effect=RuntimeError("EAPI login failed"))
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        result = await python_bridge.get_download_limits()

        assert result["sources"]["libgen"]["daily_limit"]["state"] == LIMIT_NONE
        zlibrary = result["sources"]["zlibrary"]["daily_limit"]
        assert zlibrary["state"] == LIMIT_UNKNOWN
        assert "EAPI login failed" in zlibrary["note"]

    @pytest.mark.asyncio
    async def test_unkeyed_annas_reports_search_only_without_erroring(self):
        entry = (await python_bridge.get_download_limits(sources=["annas"]))["sources"][
            "annas_archive"
        ]

        assert entry["routes"] == ["search"]
        assert entry["daily_limit"]["state"] == LIMIT_NOT_APPLICABLE

    @pytest.mark.asyncio
    async def test_an_unknown_source_is_named_rather_than_ignored(self):
        with pytest.raises(ValueError, match="Unknown source 'gutenberg'"):
            await python_bridge.get_download_limits(sources=["gutenberg"])

    @pytest.mark.asyncio
    async def test_entries_stay_symmetric_across_sources(
        self, monkeypatch, zlibrary_credentials
    ):
        eapi = SimpleNamespace(
            get_profile=AsyncMock(
                return_value={"user": {"downloads_today": 1, "downloads_limit": 10}}
            )
        )
        monkeypatch.setattr(python_bridge, "_eapi_client", eapi)
        monkeypatch.setattr(
            python_bridge, "get_eapi_client", AsyncMock(return_value=eapi)
        )

        entries = (await python_bridge.get_download_limits())["sources"]

        shapes = {tuple(sorted(entry)) for entry in entries.values()}
        assert len(shapes) == 1, entries


class TestProvenance:
    @pytest.mark.asyncio
    async def test_source_acquisition_returns_the_mirror_and_cdn_host(
        self, tmp_path, monkeypatch
    ):
        """Before #101 these went to `logger.warning` on stderr and the caller
        never saw which of three mirrors had answered (found on #98)."""

        async def candidates(_md5, source):
            yield DownloadResult(
                url="https://libgen.vg/get.php?md5=f&key=k",
                source=SourceType.LIBGEN,
                route="get.php",
                mirror="vg",
                host="cdn3.booksdl.test",
            )

        raw = tmp_path / ".source-owned.pdf"
        raw.write_bytes(b"%PDF-1.7 bytes")
        monkeypatch.setattr(
            python_bridge,
            "get_source_router",
            AsyncMock(
                return_value=SimpleNamespace(iter_download_candidates=candidates)
            ),
        )
        monkeypatch.setattr(
            python_bridge,
            "_download_url_to_file",
            AsyncMock(return_value=str(raw)),
        )

        _path, provenance = await python_bridge._fetch_from_source(
            {"md5": "f" * 32, "source": "libgen"}, str(tmp_path)
        )

        assert provenance == {
            "source": "libgen",
            "route": "get.php",
            "mirror": "vg",
            "host": "libgen.vg",
        }

    @pytest.mark.asyncio
    async def test_provenance_is_nested_on_the_download_result(
        self, tmp_path, monkeypatch
    ):
        raw = tmp_path / ".source-owned.pdf"
        raw.write_bytes(b"%PDF-1.7 bytes")
        monkeypatch.setattr(
            python_bridge,
            "_fetch_from_source",
            AsyncMock(
                return_value=(
                    str(raw),
                    {
                        "source": "libgen",
                        "route": "get.php",
                        "mirror": "li",
                        "host": "cdn1.booksdl.test",
                    },
                )
            ),
        )

        result = await python_bridge.download_book(
            book_details={
                "md5": "f" * 32,
                "source": "libgen",
                "title": "A Book",
                "extension": "pdf",
            },
            output_dir=str(tmp_path),
        )

        assert result["provenance"]["mirror"] == "li"
        assert result["provenance"]["host"] == "cdn1.booksdl.test"
        # Flat keys were the smaller diff and the wrong choice (#96).
        assert "mirror" not in result
        assert "host" not in result

    @pytest.mark.asyncio
    async def test_provenance_describes_the_transfer_not_the_content(
        self, tmp_path, monkeypatch
    ):
        """Invariant 1: no document text may ride back inside provenance."""
        raw = tmp_path / ".source-owned.pdf"
        raw.write_bytes(b"%PDF-1.7 bytes")
        monkeypatch.setattr(
            python_bridge,
            "_fetch_from_source",
            AsyncMock(return_value=(str(raw), python_bridge._provenance("libgen"))),
        )

        result = await python_bridge.download_book(
            book_details={"md5": "f" * 32, "source": "libgen", "title": "A Book"},
            output_dir=str(tmp_path),
        )

        assert sorted(result["provenance"]) == ["host", "mirror", "route", "source"]
        assert result["provenance"]["mirror"] is None
