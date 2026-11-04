"""
Integration tests for Z-Library MCP server.

These tests make REAL API calls to Z-Library and require valid credentials.
They validate that our assumptions about HTML structure, authentication,
and API behavior are correct.

IMPORTANT: These tests use real credentials and make real API calls.
- Be respectful of Z-Library's resources
- Use small result sets (limit=5-10)
- Don't run these in tight loops
- Clean up downloads after tests

Run with: pytest -m integration
Skip with: pytest -m "not integration"
"""

import pytest
import os
import sys
import asyncio
import time
import shutil
from pathlib import Path

# Add lib to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))

from term_tools import search_by_term
from author_tools import search_by_author
import python_bridge


# Keys get_book_metadata_complete may return: the detail fields get_book reports
# (lib/enhanced_metadata.py) plus the echoed id and hash. No terms, booklists or
# IPFS ids: EAPI does not expose them (#256).
DETAIL_FIELDS = {
    "id",
    "book_hash",
    "description",
    "rating",
    "series",
    "categories",
    "quality_score",
    "edition",
    "translator",
    "isbn_10",
    "isbn_13",
}

# Skip all integration tests if credentials not available
pytestmark = [
    pytest.mark.integration,
    # The zlib_client fixture is module-scoped; its httpx client binds to the
    # event loop it was created on, so all tests in this module must share a
    # module-scoped loop or later tests hit "Event loop is closed".
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not os.getenv("ZLIBRARY_EMAIL") or not os.getenv("ZLIBRARY_PASSWORD"),
        reason="Integration tests require ZLIBRARY_EMAIL and ZLIBRARY_PASSWORD environment variables",
    ),
]


def _assert_catalog_rows(result):
    """Positive canaries need usable rows, not a vacuously valid empty page."""
    assert isinstance(result, dict), "Search must return an object"
    books = result.get("books")
    assert isinstance(books, list), "Search must return a books list"
    assert books, "Expected results for this common catalog query"
    for book in books:
        assert isinstance(book, dict), "Each book must be an object"
        book_id = book.get("id")
        assert isinstance(book_id, str) and book_id.strip() not in {
            "",
            "0",
            "None",
            "N/A",
        }, "Each book must have a usable id"
        title = book.get("title")
        assert isinstance(title, str) and title.strip() not in {
            "",
            "N/A",
        }, "Each book must have a nonempty title"
    return books


def _assert_hegel_author(books):
    assert any(
        isinstance(book, dict)
        and isinstance(book.get("author"), str)
        and "hegel" in book["author"].casefold()
        for book in books
    ), "Expected at least one returned book with Hegel in its author field"


@pytest.fixture(scope="module")
def credentials():
    """Provide Z-Library credentials from environment."""
    return {
        "email": os.getenv("ZLIBRARY_EMAIL"),
        "password": os.getenv("ZLIBRARY_PASSWORD"),
        "mirror": os.getenv("ZLIBRARY_MIRROR", ""),
    }


@pytest.fixture(scope="module")
def test_download_dir():
    """Provide temporary download directory for integration tests."""
    test_dir = Path("./downloads_integration_test")
    test_dir.mkdir(exist_ok=True)
    yield str(test_dir)
    # Cleanup after all tests
    if test_dir.exists():
        shutil.rmtree(test_dir)


@pytest.mark.integration
class TestRealAuthentication:
    """Test real Z-Library authentication."""

    async def test_authentication_succeeds(self, credentials, eapi_domain):
        """Should successfully authenticate with real credentials."""
        from zlibrary.eapi import EAPIClient

        client = EAPIClient(eapi_domain)
        login_result = await client.login(credentials["email"], credentials["password"])

        # Verify login succeeded
        assert login_result.get("success") == 1

        # Verify we can search after authentication
        search_result = await client.search("test", limit=1)
        assert search_result is not None

        await client.close()

    async def test_authentication_fails_with_invalid_credentials(self, eapi_domain):
        """Should raise error or return failure with invalid credentials."""
        from zlibrary.eapi import EAPIClient

        client = EAPIClient(eapi_domain)
        try:
            login_result = await client.login("invalid@example.com", "wrongpassword")
        except Exception:
            pass  # A login exception is an expected authentication failure.
        else:
            # Keep this assertion outside the login exception handler.
            assert login_result.get("success") != 1
        finally:
            await client.close()

    async def test_session_persistence(self, credentials, eapi_domain):
        """Should maintain session across multiple operations."""
        from zlibrary.eapi import EAPIClient

        client = EAPIClient(eapi_domain)
        await client.login(credentials["email"], credentials["password"])

        # Perform multiple searches with same session
        result1 = await client.search("test", limit=1)
        await asyncio.sleep(0.5)
        result2 = await client.search("philosophy", limit=1)
        await asyncio.sleep(0.5)
        result3 = await client.search("science", limit=1)

        assert result1 is not None
        assert result2 is not None
        assert result3 is not None

        await client.close()


@pytest.mark.integration
class TestRealBasicSearch:
    """Test basic search operations with real Z-Library."""

    async def test_basic_search_returns_results(self, zlib_client):
        """Should return results for a common search query."""
        # Use injected client (no global state)
        result = await python_bridge.search(
            query="philosophy",
            count=5,
            client=zlib_client,  # Dependency injection
        )

        # Verify structure
        assert "books" in result
        assert "retrieved_from_url" in result

        # Should have found results
        assert len(result["books"]) > 0

        # Validate book structure
        first_book = result["books"][0]
        assert "title" in first_book or "name" in first_book
        assert "id" in first_book or "href" in first_book

    async def test_search_with_year_filter(self, zlib_client):
        """Should respect year filters in search."""
        result = await python_bridge.search(
            query="artificial intelligence",
            from_year=2020,
            to_year=2023,
            count=5,
            client=zlib_client,
        )

        for book in _assert_catalog_rows(result):
            # EAPI returns year as int; older HTML scraping returned str.
            year = str(book.get("year") or "")
            assert year.isdecimal(), "Year-filtered results must have a year"
            assert 2020 <= int(year) <= 2023

    async def test_search_with_language_filter(self, zlib_client):
        """Should respect language filters."""
        result = await python_bridge.search(
            query="philosophy", languages=["english"], count=5, client=zlib_client
        )

        for book in _assert_catalog_rows(result):
            language = book.get("language")
            assert isinstance(language, str) and language.casefold() == "english"

    async def test_recent_books_contract(self, zlib_client):
        """Check the distinct recent endpoint using the already-authenticated fixture.

        Inspect upstream shape before bridge normalization can turn a missing
        books field into an empty list. No new client or login is created.
        """
        result = await zlib_client.get_recently()
        assert isinstance(result, dict), (
            "Recent-books provider contract error: expected an object"
        )
        assert result.get("success", 1) == 1, (
            "Recent-books provider contract error: provider did not report success"
        )
        assert isinstance(result.get("books"), list), (
            "Recent-books provider contract error: expected a books list"
        )
        assert all(isinstance(book, dict) for book in result["books"]), (
            "Recent-books provider contract error: expected book objects"
        )


@pytest.mark.integration
class TestRealTermSearch:
    """Test term search with real Z-Library API."""

    async def test_search_common_term(self, credentials, zlib_client):
        """Should find books for a common philosophical term."""
        # Test now uses isolated client from fixture
        await asyncio.sleep(1)

        # Reuse the module's authenticated client: the live login endpoint
        # rate-limits repeated logins from one IP, so per-call logins flake.
        result = await search_by_term(
            term="dialectic",
            email=credentials["email"],
            password=credentials["password"],
            limit=5,
            eapi_client=zlib_client,
        )

        # Verify structure
        assert "term" in result
        assert result["term"] == "dialectic"
        assert "books" in result
        assert "total_results" in result

        # Verify we got real results
        assert result["total_results"] > 0
        assert len(result["books"]) > 0

        # Validate real book structure
        first_book = result["books"][0]
        assert "title" in first_book
        assert first_book["title"] != ""
        assert first_book["title"] != "N/A"

        # Should have some metadata
        assert "id" in first_book or "href" in first_book

    async def test_term_search_with_filters(self, credentials, zlib_client):
        """Should support year and language filters in term search."""
        await asyncio.sleep(1)

        result = await search_by_term(
            term="philosophy",
            email=credentials["email"],
            password=credentials["password"],
            year_from=2000,
            languages="english",
            limit=5,
            eapi_client=zlib_client,
        )

        books = _assert_catalog_rows(result)
        assert result["term"] == "philosophy"
        # These tools report the returned page length, not the catalog total.
        assert type(result["total_results"]) is int
        assert result["total_results"] == len(books)
        for book in books:
            year = str(book.get("year") or "")
            assert year.isdecimal() and int(year) >= 2000
            language = book.get("language")
            assert isinstance(language, str) and language.casefold() == "english"


@pytest.mark.integration
class TestRealAuthorSearch:
    """Test author search with real Z-Library API."""

    async def test_search_famous_author(self, credentials, zlib_client):
        """Should find books by a famous author."""
        await asyncio.sleep(1)

        result = await search_by_author(
            author="Hegel",
            email=credentials["email"],
            password=credentials["password"],
            limit=5,
            eapi_client=zlib_client,
        )

        # Verify structure
        assert "author" in result
        assert "books" in result
        assert "total_results" in result

        # Should find many works
        assert result["total_results"] > 0
        assert len(result["books"]) > 0

        # Validate real book data
        first_book = result["books"][0]
        assert "title" in first_book
        assert first_book["title"] != ""
        _assert_hegel_author(result["books"])

    async def test_search_author_with_comma_format(self, credentials, zlib_client):
        """Should handle 'Lastname, Firstname' format."""
        await asyncio.sleep(1)

        result = await search_by_author(
            author="Hegel, Georg",
            email=credentials["email"],
            password=credentials["password"],
            limit=5,
            eapi_client=zlib_client,
        )

        books = _assert_catalog_rows(result)
        assert result["author"] == "Hegel, Georg"
        assert type(result["total_results"]) is int
        assert result["total_results"] == len(books)
        _assert_hegel_author(books)

    async def test_author_search_with_year_filter(self, credentials, zlib_client):
        """Should support year filtering in author search."""
        await asyncio.sleep(1)

        result = await search_by_author(
            author="Hegel",
            email=credentials["email"],
            password=credentials["password"],
            # EAPI years describe editions, not the author's lifetime.
            year_from=1950,
            year_to=2025,
            limit=5,
            eapi_client=zlib_client,
        )

        books = _assert_catalog_rows(result)
        assert result["author"] == "Hegel"
        assert type(result["total_results"]) is int
        assert result["total_results"] == len(books)
        _assert_hegel_author(books)
        for book in books:
            year = str(book.get("year") or "")
            assert year.isdecimal() and 1950 <= int(year) <= 2025


@pytest.mark.integration
class TestRealMetadataExtraction:
    """Test metadata extraction with real book pages."""

    async def test_detail_matches_get_book_shape(
        self, credentials, zlib_client, known_book
    ):
        """The live detail reply carries only fields get_book can report (#256).

        EAPI exposes no terms, booklists or IPFS ids. Absent upstream values
        are omitted rather than returned empty, so every key present must hold
        a value of the documented type.
        """
        await asyncio.sleep(1)

        metadata = await python_bridge.get_book_metadata_complete(
            book_id=known_book["id"], book_hash=known_book["hash"]
        )

        assert metadata["id"] == known_book["id"]
        assert metadata["book_hash"] == known_book["hash"]
        assert set(metadata) <= DETAIL_FIELDS, (
            f"Unexpected detail fields: {sorted(set(metadata) - DETAIL_FIELDS)}"
        )
        # The bridge always echoes id and book_hash; a reply with nothing else
        # means EAPI dropped or renamed every detail field.
        assert set(metadata) - {"id", "book_hash"}, (
            "Live detail reply carried no get_book field besides the identifiers"
        )
        for key in ("description", "series", "edition", "translator"):
            if key in metadata:
                assert isinstance(metadata[key], str) and metadata[key]
        for key in ("isbn_10", "isbn_13"):
            if key in metadata:
                assert isinstance(metadata[key], str) and metadata[key]
        if "categories" in metadata:
            assert metadata["categories"]
            assert all(set(c) == {"name"} and c["name"] for c in metadata["categories"])
        if "rating" in metadata:
            assert isinstance(metadata["rating"]["value"], float)
            assert set(metadata["rating"]) <= {"value", "count"}
        if "quality_score" in metadata:
            assert isinstance(metadata["quality_score"], float)

        print(f"\nDetail fields from live EAPI: {sorted(metadata)}")

    async def test_extract_rating_data(self, credentials, zlib_client, known_book):
        """Test that rating and quality metrics are extracted."""
        await asyncio.sleep(1)

        metadata = await python_bridge.get_book_metadata_complete(
            book_id=known_book["id"], book_hash=known_book["hash"]
        )

        # Rating is omitted when the book has no rating; when present it is a
        # dict with value and, if upstream sends it, count
        # (see enhanced_metadata._extract_rating_from_eapi).
        if "rating" in metadata:
            assert "value" in metadata["rating"]

    @pytest.mark.skip(
        reason="download link not part of EAPI metadata — downloads flow "
        "through EAPIClient.download_book (covered by TestDownloadOperations)"
    )
    async def test_extract_download_link(self, credentials, zlib_client, known_book):
        """Test that download link is properly extracted."""
        await asyncio.sleep(1)

        metadata = await python_bridge.get_book_metadata_complete(
            book_id=known_book["id"], book_hash=known_book["hash"]
        )

        # Should have download info
        assert "download" in metadata
        assert isinstance(metadata["download"], dict)

        # Should have link
        assert "link" in metadata["download"]
        assert "/dl/" in metadata["download"]["link"]

    async def test_metadata_extraction_performance(
        self, credentials, zlib_client, known_book
    ):
        """Should extract metadata within performance target (<5s with network)."""
        await asyncio.sleep(1)

        start = time.time()
        metadata = await python_bridge.get_book_metadata_complete(
            book_id=known_book["id"], book_hash=known_book["hash"]
        )
        duration = time.time() - start

        # Should complete reasonably fast (allowing for network latency)
        assert duration < 10.0, f"Metadata extraction took {duration}s, expected <10s"

        # Verify we got data despite speed
        assert metadata is not None
        assert isinstance(metadata, dict)


@pytest.mark.integration
class TestRealWorldEdgeCases:
    """Test with real-world edge cases from Z-Library."""

    async def test_search_returns_both_books_and_articles(
        self, credentials, zlib_client
    ):
        """Should handle mixed search results (books + articles)."""
        await asyncio.sleep(1)

        # Broad search likely to return both
        result = await python_bridge.search(
            query="computer science", count=20, client=zlib_client
        )

        # Extract types
        types = {b.get("type", "book") for b in result["books"]}

        # All should have a type
        assert "book" in types or "article" in types

        # All should have titles
        for book in result["books"]:
            title = book.get("title", book.get("name", ""))
            assert title != "", "All results should have titles"
            assert title != "N/A", "Titles should be extracted, not 'N/A'"

    async def test_handle_special_characters_in_titles(self, credentials, zlib_client):
        """Should handle books with special characters in titles."""
        await asyncio.sleep(1)

        # Search for something likely to have special chars
        result = await python_bridge.search(
            query="L'être",  # French with apostrophe
            count=5,
            client=zlib_client,
        )

        # Should not crash on special characters
        assert "books" in result
        assert isinstance(result["books"], list)

    async def test_unicode_handling(self, credentials, zlib_client):
        """Should handle Unicode characters in various scripts."""
        await asyncio.sleep(1)

        # Search for something with Unicode
        result = await python_bridge.search(
            query="哲学",  # Chinese for "philosophy"
            count=5,
            client=zlib_client,
        )

        # Should not crash
        assert "books" in result

    async def test_empty_search_results(self, credentials, zlib_client):
        """Should return a consistent page even when a sparse query gains matches."""
        await asyncio.sleep(1)

        # Very specific query unlikely to have results
        # The term helper exposes total_results as the returned page length.
        result = await search_by_term(
            term="xyzabc123nonexistent987",
            email=credentials["email"],
            password=credentials["password"],
            limit=5,
            eapi_client=zlib_client,
        )

        # Catalog contents can change; deterministic emptiness is checked offline.
        assert isinstance(result, dict)
        assert isinstance(result.get("books"), list)
        assert type(result.get("total_results")) is int
        assert result["total_results"] == len(result["books"])


@pytest.mark.integration
class TestDownloadOperations:
    """Test download functionality with real Z-Library."""

    async def test_download_small_book(
        self, credentials, zlib_client, test_download_dir
    ):
        """Should download a small book successfully."""
        await asyncio.sleep(2)

        # Find a small book
        search_result = await python_bridge.search(
            query="Python", count=1, client=zlib_client
        )

        if len(search_result["books"]) == 0:
            pytest.skip("No books found for download test")

        book = search_result["books"][0]

        # Download the book
        download_result = await python_bridge.download_book(
            book_details=book,
            output_dir=test_download_dir,
            process_for_rag=False,  # Don't process, just download
        )

        # Verify download succeeded
        assert "file_path" in download_result
        assert download_result["file_path"] is not None

        # Verify file exists
        assert os.path.exists(download_result["file_path"]), (
            f"Downloaded file should exist at {download_result['file_path']}"
        )

        # Verify file has content
        file_size = os.path.getsize(download_result["file_path"])
        assert file_size > 1000, (
            f"Downloaded file should have content, got {file_size} bytes"
        )


@pytest.mark.integration
class TestPerformanceMetrics:
    """Measure real-world performance with live API."""

    async def test_search_response_time(self, credentials, zlib_client):
        """Measure search response time."""
        await asyncio.sleep(1)

        start = time.time()
        result = await python_bridge.search(query="test", count=5, client=zlib_client)  # noqa: F841
        duration = time.time() - start

        # Should complete reasonably fast (network latency included)
        assert duration < 10.0, f"Search took {duration}s, expected <10s"

        # Log performance for monitoring
        print(f"\nSearch performance: {duration:.2f}s")

    async def test_metadata_extraction_response_time(
        self, credentials, zlib_client, known_book
    ):
        """Measure metadata extraction response time."""
        await asyncio.sleep(1)

        start = time.time()
        metadata = await python_bridge.get_book_metadata_complete(
            book_id=known_book["id"], book_hash=known_book["hash"]
        )
        duration = time.time() - start

        # Should complete reasonably fast
        assert duration < 10.0, f"Metadata extraction took {duration}s, expected <10s"

        # Log performance
        print(f"\nMetadata extraction performance: {duration:.2f}s")
        print(f"Keys in metadata: {list(metadata.keys())}")
