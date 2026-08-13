"""
Unit tests for filename_utils.py

Tests unified filename generation, slugification, and author extraction.
"""

import pytest
import sys
from pathlib import Path

# Add lib to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "lib"))

from filename_utils import (
    to_camel_case,
    slugify,
    format_author_camelcase,
    create_unified_filename,
    create_metadata_filename,
    parse_filename,
)

pytestmark = pytest.mark.unit


class TestSlugify:
    """Test slugification function."""

    def test_basic_slugify(self):
        """Test basic string slugification."""
        assert slugify("The Burnout Society") == "the-burnout-society"
        assert slugify("L'Être et le Néant") == "l-etre-et-le-neant"

    def test_slugify_with_special_chars(self):
        """Test slugification with special characters."""
        assert slugify("Hello, World!") == "hello-world"
        assert slugify("Test: A Study") == "test-a-study"
        assert slugify("Book #1") == "book-1"

    def test_slugify_multiple_spaces(self):
        """Test handling of multiple spaces."""
        assert slugify("Multiple   Spaces") == "multiple-spaces"
        assert slugify("  Leading and trailing  ") == "leading-and-trailing"

    def test_slugify_unicode(self):
        """Test Unicode handling."""
        assert slugify("Byung-Chul Han") == "byung-chul-han"
        assert slugify("José Saramago") == "jose-saramago"

    def test_slugify_empty_string(self):
        """Test empty string handling."""
        assert slugify("") == "unknown"
        assert slugify("   ") == "unknown"

    def test_slugify_max_length(self):
        """Test length limiting."""
        long_string = "a" * 100
        result = slugify(long_string, max_length=50)
        assert len(result) == 50
        assert result == "a" * 50


class TestToCamelCase:
    """Test CamelCase conversion."""

    def test_basic_camelcase(self):
        """Test basic CamelCase conversion."""
        assert to_camel_case("Byung-Chul Han") == "ByungChulHan"
        assert to_camel_case("The Burnout Society") == "TheBurnoutSociety"

    def test_camelcase_with_hyphens(self):
        """Test that hyphens are removed."""
        assert to_camel_case("Jean-Luc Nancy") == "JeanLucNancy"
        assert to_camel_case("Being-in-the-World") == "BeingInTheWorld"

    def test_camelcase_with_apostrophes(self):
        """Test apostrophes are removed."""
        assert to_camel_case("L'Être et le Néant") == "LEtreEtLeNeant"

    def test_camelcase_empty(self):
        """Test empty string."""
        assert to_camel_case("") == "Unknown"


class TestFormatAuthorCamelcase:
    """Test author CamelCase formatting (LastNameFirstName)."""

    def test_format_simple_name(self):
        """Test simple names (no reordering needed)."""
        assert format_author_camelcase("Plato") == "Plato"
        assert format_author_camelcase("Aristotle") == "Aristotle"

    def test_format_two_word_name(self):
        """Test two-word names (LastName first)."""
        assert format_author_camelcase("Byung-Chul Han") == "HanByungChul"
        assert format_author_camelcase("Giorgio Agamben") == "AgambenGiorgio"

    def test_format_comma_separated(self):
        """Test comma-separated format (already LastName, FirstName)."""
        assert format_author_camelcase("Han, Byung-Chul") == "HanByungChul"
        assert format_author_camelcase("Agamben, Giorgio") == "AgambenGiorgio"

    def test_format_hyphenated_name(self):
        """Test hyphenated names (hyphens removed, LastName first)."""
        assert format_author_camelcase("Jean-Luc Nancy") == "NancyJeanLuc"

    def test_format_empty(self):
        """Test empty author."""
        assert format_author_camelcase("") == "UnknownAuthor"
        assert format_author_camelcase("   ") == "UnknownAuthor"


class TestCreateUnifiedFilename:
    """Test unified filename generation."""

    def test_basic_filename_generation(self):
        """Test basic filename generation with CamelCase_Underscores (LastNameFirstName)."""
        book_details = {
            "author": "Byung-Chul Han",
            "title": "The Burnout Society",
            "id": "3505318",
            "extension": "pdf",
        }
        result = create_unified_filename(book_details)
        assert result == "HanByungChul_TheBurnoutSociety_3505318.pdf"

    def test_filename_with_comma_author(self):
        """Test with comma-separated author (already LastName, FirstName format)."""
        book_details = {
            "author": "Agamben, Giorgio",
            "title": "The Coming Community",
            "id": "6035827",
            "extension": "epub",
        }
        result = create_unified_filename(book_details)
        assert result == "AgambenGiorgio_TheComingCommunity_6035827.epub"

    def test_filename_missing_author(self):
        """Test with missing author."""
        book_details = {"title": "Unknown Work", "id": "12345", "extension": "pdf"}
        result = create_unified_filename(book_details)
        assert result == "UnknownAuthor_UnknownWork_12345.pdf"

    def test_filename_missing_title(self):
        """Test with missing title (LastNameFirstName: Author is lastname, Test is firstname)."""
        book_details = {"author": "Test Author", "id": "12345", "extension": "pdf"}
        result = create_unified_filename(book_details)
        assert result == "AuthorTest_UntitledBook_12345.pdf"

    def test_filename_with_special_chars(self):
        """Test with special characters in title (removed in CamelCase)."""
        book_details = {
            "author": "Test Author",
            "title": "Book: A Study (Part 1)",
            "id": "99999",
            "extension": "pdf",
        }
        result = create_unified_filename(book_details)
        assert result == "AuthorTest_BookAStudyPart1_99999.pdf"

    def test_filename_with_suffix(self):
        """Test with suffix parameter."""
        book_details = {
            "author": "Test Author",
            "title": "Test Book",
            "id": "123",
            "extension": "pdf",
        }
        result = create_unified_filename(book_details, suffix=".processed.markdown")
        assert result.endswith(".processed.markdown")

    def test_filename_extension_override(self):
        """Test extension override."""
        book_details = {
            "author": "Test Author",
            "title": "Test Book",
            "id": "123",
            "extension": "pdf",
        }
        result = create_unified_filename(book_details, extension="epub")
        assert result.endswith(".epub")

    def test_filename_length_truncation(self):
        """Test automatic length truncation."""
        book_details = {
            "author": "A" * 100,
            "title": "B" * 100,
            "id": "12345",
            "extension": "pdf",
        }
        result = create_unified_filename(book_details, max_total_length=100)
        # Should truncate to fit within max_total_length + extension
        assert len(result) <= 110  # 100 + ".pdf" + some buffer

    @pytest.mark.parametrize(
        ("book_details", "identity"),
        [
            ({"id": "42", "md5": "a" * 32}, "42"),
            ({"id": "", "md5": "b" * 32}, "b" * 32),
            ({"id": "   ", "md5": "c" * 32}, "c" * 32),
            ({"id": "", "md5": ""}, "NoID"),
        ],
    )
    def test_filename_identity_prefers_id_then_md5_then_noid(
        self, book_details, identity
    ):
        """Removing identity precedence would collide source search results."""
        details = {"author": "Same Author", "title": "Same Title", **book_details}

        result = create_unified_filename(details)

        assert result == f"AuthorSame_SameTitle_{identity}"

    def test_distinct_md5_values_disambiguate_identical_metadata(self):
        """Two source records with equal metadata but different content stay distinct."""
        common = {"author": "Same Author", "title": "Same Title", "id": ""}

        first = create_unified_filename({**common, "md5": "a" * 32})
        second = create_unified_filename({**common, "md5": "b" * 32})

        assert first != second

    @pytest.mark.parametrize("extension", ["", "   ", ".  "])
    def test_empty_extension_does_not_add_trailing_dot(self, extension):
        """Whitespace-only metadata is missing extension evidence, not a suffix."""
        result = create_unified_filename(
            {"author": "Test Author", "title": "Book", "id": "7"},
            extension=extension,
        )

        assert result == "AuthorTest_Book_7"

    @pytest.mark.parametrize(
        "extension",
        [
            "../pdf",
            "pdf/../../escape",
            "pdf\\escape",
            "pdf\x00.exe",
            "pdf\nexe",
            "exe",
            "sh",
            "unknown",
            "a" * 64,
        ],
    )
    def test_unsafe_or_unknown_extension_cannot_alter_the_output_path(self, extension):
        """Untrusted metadata must not become a path or executable suffix."""
        result = create_unified_filename(
            {"author": "Test Author", "title": "Book", "id": "7"},
            extension=extension,
        )

        assert result == "AuthorTest_Book_7"
        assert Path(result).name == result

    @pytest.mark.parametrize("extension", ["pdf", ".EPUB", " txt ", "mobi", "azw3"])
    def test_safe_document_extensions_are_normalized(self, extension):
        """Non-RAG acquisition retains a conservative document vocabulary."""
        result = create_unified_filename(
            {"author": "Test Author", "title": "Book", "id": "7"},
            extension=extension,
        )

        assert result.rsplit(".", 1)[1] == extension.strip().lstrip(".").lower()


class TestCreateMetadataFilename:
    """Test metadata filename generation."""

    def test_metadata_from_original(self):
        """Test metadata filename from original (LastNameFirstName format)."""
        original = "HanByungChul_TheBurnoutSociety_3505318.pdf"
        result = create_metadata_filename(original)
        assert result == "HanByungChul_TheBurnoutSociety_3505318.pdf.metadata.json"

    def test_metadata_from_processed(self):
        """Test metadata filename from processed file."""
        processed = "HanByungChul_TheBurnoutSociety_3505318.pdf.processed.markdown"
        result = create_metadata_filename(processed)
        # Should remove .processed.markdown
        assert result == "HanByungChul_TheBurnoutSociety_3505318.pdf.metadata.json"

    def test_metadata_with_path(self):
        """Test with full path."""
        full_path = "/downloads/HanByungChul_TheBurnoutSociety_3505318.pdf"
        result = create_metadata_filename(full_path)
        assert result == "HanByungChul_TheBurnoutSociety_3505318.pdf.metadata.json"


class TestParseFilename:
    """Test filename parsing (CamelCase_Underscores format)."""

    def test_parse_basic_filename(self):
        """Test parsing basic CamelCase filename (LastNameFirstName)."""
        filename = "HanByungChul_TheBurnoutSociety_3505318.pdf"
        result = parse_filename(filename)

        assert result["author_camel"] == "HanByungChul"
        assert result["title_camel"] == "TheBurnoutSociety"
        assert result["book_id"] == "3505318"
        assert result["extension"] == "pdf"

    def test_parse_processed_filename(self):
        """Test parsing processed filename."""
        filename = "HanByungChul_TheBurnoutSociety_3505318.pdf.processed.markdown"
        result = parse_filename(filename)

        assert result["author_camel"] == "HanByungChul"
        assert result["title_camel"] == "TheBurnoutSociety"
        assert result["book_id"] == "3505318"
        assert result["extension"] == "processed.markdown"

    def test_parse_with_path(self):
        """Test parsing with full path."""
        filename = "/downloads/AgambenGiorgio_TheComingCommunity_6035827.epub"
        result = parse_filename(filename)

        assert result["author_camel"] == "AgambenGiorgio"
        assert result["title_camel"] == "TheComingCommunity"
        assert result["book_id"] == "6035827"
        assert result["extension"] == "epub"

    def test_parse_multi_author(self):
        """Test parsing multiple authors (parser treats first component as author)."""
        filename = "DerridaJacques_NancyJeanLuc_TheInoperativeCommunity_1234567.pdf"
        result = parse_filename(filename)

        # Parser can't distinguish multi-author from multi-word-title
        # It treats first component as author, rest as title
        assert result["author_camel"] == "DerridaJacques"
        assert result["title_camel"] == "NancyJeanLuc_TheInoperativeCommunity"
        assert result["book_id"] == "1234567"

        # Note: For accurate parsing of multi-author files, use the metadata JSON


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
