"""
Unit tests for cross-page footnote continuation state machine.

Tests cover:
- Single-page footnotes (baseline)
- Multi-page continuations (2-3 pages)
- State machine transitions
- Edge cases (false positives, orphaned content, multiple incomplete)
- Confidence scoring
- Font matching
- Spatial analysis

NO MOCKING for core logic - uses synthetic data structures.
Integration tests with real PDFs in test_real_footnotes.py
"""

import pytest
from pathlib import Path
import sys

# Add lib to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "lib"))

from footnote_continuation import (
    FootnoteWithContinuation,
    CrossPageFootnoteParser,
    is_footnote_incomplete,
    analyze_footnote_batch,
    get_incomplete_confidence_threshold,
)
import footnote_continuation as _footnote_continuation

# Some assertions below are about the NLTK sentence-boundary path specifically —
# its reason codes (`nltk_*`) and its higher confidence — not about the contract
# every environment must satisfy. NLTK's punkt data is not a package dependency
# and the module deliberately never downloads it (a RAG operation has to be
# deterministic and usable offline, #103), so a checkout without it takes the
# deterministic fallback and those assertions are false there for a reason that
# is not a defect. CI provisions the data explicitly, so these still run there;
# the fallback path has its own dedicated coverage in
# `test_optional_dependencies.py`, which pins both branches exactly.
requires_nltk_tokenizer = pytest.mark.skipif(
    not _footnote_continuation._nltk_ready,
    reason="NLTK punkt data absent; this asserts the NLTK path, not the contract",
)
from rag_data_models import NoteSource

pytestmark = pytest.mark.unit


class TestFootnoteWithContinuation:
    """Test FootnoteWithContinuation data model."""

    def test_single_page_footnote(self):
        """Test basic single-page footnote."""
        footnote = FootnoteWithContinuation(
            marker="1",
            content="Brief citation.",
            pages=[5],
            bboxes=[{"x0": 50, "y0": 700, "x1": 550, "y1": 720}],
            is_complete=True,
            note_source=NoteSource.AUTHOR,
        )

        assert footnote.marker == "1"
        assert footnote.content == "Brief citation."
        assert footnote.pages == [5]
        assert len(footnote.bboxes) == 1
        assert footnote.is_complete is True
        assert footnote.continuation_confidence == 1.0
        assert footnote.note_source == NoteSource.AUTHOR

    def test_append_continuation_with_space(self):
        """Test appending continuation with automatic spacing."""
        footnote = FootnoteWithContinuation(
            marker="a",
            content="This is the first part",
            pages=[64],
            bboxes=[{"x0": 50, "y0": 700, "x1": 550, "y1": 780}],
            is_complete=False,
            note_source=NoteSource.TRANSLATOR,
        )

        footnote.append_continuation(
            additional_content="and this continues.",
            page_num=65,
            bbox={"x0": 50, "y0": 50, "x1": 550, "y1": 70},
            confidence=0.92,
        )

        assert footnote.content == "This is the first part and this continues."
        assert footnote.pages == [64, 65]
        assert len(footnote.bboxes) == 2
        assert footnote.continuation_confidence == 0.92

    def test_append_continuation_with_hyphen(self):
        """Test appending continuation with hyphenated word."""
        footnote = FootnoteWithContinuation(
            marker="*", content="This word is hyphen-", pages=[1], is_complete=False
        )

        footnote.append_continuation(
            additional_content="ated across pages.", page_num=2, confidence=0.90
        )

        assert footnote.content == "This word is hyphenated across pages."
        assert footnote.pages == [1, 2]

    def test_append_multiple_continuations(self):
        """Test appending multiple continuations (3+ pages)."""
        footnote = FootnoteWithContinuation(
            marker="†",
            content="Very long footnote starts here",
            pages=[10],
            is_complete=False,
        )

        footnote.append_continuation("and continues on page 2", 11, confidence=0.90)
        footnote.append_continuation("and finally ends on page 3.", 12, confidence=0.85)

        assert footnote.pages == [10, 11, 12]
        assert len(footnote.content) > 50
        # Confidence should be minimum (weakest link)
        assert footnote.continuation_confidence == 0.85

    def test_get_summary_single_page(self):
        """Test summary for single-page footnote."""
        footnote = FootnoteWithContinuation(
            marker="1",
            content="Brief note.",
            pages=[5],
            is_complete=True,
            note_source=NoteSource.AUTHOR,
        )

        summary = footnote.get_summary()
        assert "Footnote '1'" in summary
        assert "1 page(s) (5)" in summary
        assert "AUTHOR" in summary
        assert "complete" in summary
        assert "1.00" in summary

    def test_get_summary_multi_page(self):
        """Test summary for multi-page footnote."""
        footnote = FootnoteWithContinuation(
            marker="a",
            content="Long translator gloss.",
            pages=[64, 65],
            is_complete=True,
            continuation_confidence=0.92,
            note_source=NoteSource.TRANSLATOR,
        )

        summary = footnote.get_summary()
        assert "Footnote 'a'" in summary
        assert "2 page(s) (64-65)" in summary
        assert "TRANSLATOR" in summary
        assert "0.92" in summary


class TestCrossPageFootnoteParser:
    """Test CrossPageFootnoteParser state machine."""

    def test_single_page_complete_footnote(self):
        """Test processing single complete footnote on one page."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {
                "marker": "1",
                "content": "Complete footnote on page 1.",
                "is_complete": True,
                "note_source": NoteSource.AUTHOR,
            }
        ]

        completed = parser.process_page(page1_notes, page_num=1)

        assert len(completed) == 1
        assert completed[0].marker == "1"
        assert completed[0].pages == [1]
        assert completed[0].is_complete is True
        assert len(parser.incomplete_footnotes) == 0

    def test_two_page_continuation_basic(self):
        """Test basic two-page continuation."""
        parser = CrossPageFootnoteParser()

        # Page 1: Incomplete footnote
        page1_notes = [
            {
                "marker": "*",
                "content": "This footnote continues",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]

        completed = parser.process_page(page1_notes, page_num=1)
        assert len(completed) == 0  # Not finished yet
        assert len(parser.incomplete_footnotes) == 1

        # Page 2: Continuation (no marker, same font)
        page2_notes = [
            {
                "marker": None,
                "content": "onto the next page.",
                "is_complete": True,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]

        completed = parser.process_page(page2_notes, page_num=2)
        assert len(completed) == 1
        assert completed[0].marker == "*"
        assert completed[0].pages == [1, 2]
        assert "This footnote continues onto the next page." in completed[0].content
        assert completed[0].is_complete is True
        assert completed[0].continuation_confidence >= 0.85

    def test_three_page_continuation(self):
        """Test continuation across three pages."""
        parser = CrossPageFootnoteParser()

        # Page 1: Start
        page1_notes = [
            {
                "marker": "a",
                "content": "Long translator gloss begins",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page1_notes, page_num=1)

        # Page 2: Middle continuation
        page2_notes = [
            {
                "marker": None,
                "content": "and continues with more detail",
                "is_complete": False,  # Still not done
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)
        assert len(completed) == 0  # Still incomplete

        # Page 3: Final continuation
        page3_notes = [
            {
                "marker": None,
                "content": "and finally concludes here.",
                "is_complete": True,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        completed = parser.process_page(page3_notes, page_num=3)

        assert len(completed) == 1
        assert completed[0].pages == [1, 2, 3]
        assert len(completed[0].content) > 50
        assert completed[0].is_complete is True

    def test_false_incomplete_detection(self):
        """Test handling false incomplete (no continuation on next page)."""
        parser = CrossPageFootnoteParser()

        # Page 1: Marked incomplete but actually complete
        page1_notes = [
            {
                "marker": "1",
                "content": "This looks incomplete but is actually complete.",
                "is_complete": False,
            }
        ]
        completed = parser.process_page(page1_notes, page_num=1)
        assert len(completed) == 0
        assert len(parser.incomplete_footnotes) == 1

        # Page 2: New footnote (no continuation)
        page2_notes = [
            {"marker": "2", "content": "A new footnote on page 2.", "is_complete": True}
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        # Should mark previous as complete (false incomplete)
        assert len(completed) == 2  # Both previous and current
        assert completed[0].marker == "1"
        assert completed[0].is_complete is True
        assert completed[1].marker == "2"

    def test_orphaned_content_no_incomplete(self):
        """Test handling orphaned continuation content without incomplete footnote."""
        parser = CrossPageFootnoteParser()

        # Page 1: Content without marker but no incomplete to attach to
        page1_notes = [
            {
                "marker": None,
                "content": "Orphaned content with no previous footnote.",
            }
        ]

        completed = parser.process_page(page1_notes, page_num=1)

        # Should skip orphaned content
        assert len(completed) == 0
        assert len(parser.incomplete_footnotes) == 0

    def test_continuation_confidence_font_match(self):
        """Test high confidence when fonts match."""
        parser = CrossPageFootnoteParser()

        # Page 1: Incomplete with font metadata
        page1_notes = [
            {
                "marker": "*",
                "content": "Start of footnote",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page1_notes, page_num=1)

        # Page 2: Continuation with matching font
        page2_notes = [
            {
                "marker": None,
                "content": "continuation text.",
                "font_name": "TimesNewRoman",
                "font_size": 9.0,  # Exact match
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1
        # Font match should give high confidence
        assert completed[0].continuation_confidence >= 0.90

    def test_continuation_confidence_font_mismatch(self):
        """Test lower confidence when fonts don't match."""
        parser = CrossPageFootnoteParser()

        # Page 1: Incomplete
        page1_notes = [
            {
                "marker": "a",
                "content": "Start of footnote",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page1_notes, page_num=1)

        # Page 2: Continuation with different font
        page2_notes = [
            {
                "marker": None,
                "content": "continuation with different font.",
                "font_name": "Arial",  # Different font
                "font_size": 10.0,
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1
        # Font mismatch should give lower confidence
        assert completed[0].continuation_confidence < 0.90

    def test_continuation_signal_lowercase_start(self):
        """Test continuation detection with lowercase start."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "1", "content": "Incomplete note", "is_complete": False}
        ]
        parser.process_page(page1_notes, page_num=1)

        # Continuation starts lowercase
        page2_notes = [
            {
                "marker": None,
                "content": "and continues here.",  # lowercase start
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1

    def test_continuation_signal_conjunction_start(self):
        """Test continuation detection with conjunction start."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "2", "content": "Note starts here", "is_complete": False}
        ]
        parser.process_page(page1_notes, page_num=1)

        # Continuation starts with conjunction
        page2_notes = [
            {
                "marker": None,
                "content": "However, the argument continues.",  # conjunction start
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1

    def test_finalize_with_incomplete_footnote(self):
        """Test finalizing document with remaining incomplete footnote."""
        parser = CrossPageFootnoteParser()

        # Add incomplete footnote
        page1_notes = [
            {
                "marker": "*",
                "content": "Incomplete note at document end.",
                "is_complete": False,
            }
        ]
        parser.process_page(page1_notes, page_num=1)

        assert len(parser.incomplete_footnotes) == 1

        # Finalize (document end)
        remaining = parser.finalize()

        assert len(remaining) == 1
        assert remaining[0].marker == "*"
        assert remaining[0].is_complete is True  # Marked complete at doc end
        assert len(parser.incomplete_footnotes) == 0

    def test_finalize_with_no_incomplete(self):
        """Test finalizing with no incomplete footnotes."""
        parser = CrossPageFootnoteParser()

        # Process complete footnote
        page1_notes = [
            {"marker": "1", "content": "Complete note.", "is_complete": True}
        ]
        parser.process_page(page1_notes, page_num=1)

        # Finalize
        remaining = parser.finalize()

        assert len(remaining) == 0
        assert len(parser.incomplete_footnotes) == 0

    def test_get_all_completed(self):
        """Test retrieving all completed footnotes."""
        parser = CrossPageFootnoteParser()

        # Process multiple pages
        page1_notes = [
            {"marker": "1", "content": "First footnote.", "is_complete": True}
        ]
        parser.process_page(page1_notes, page_num=1)

        page2_notes = [
            {"marker": "2", "content": "Second footnote.", "is_complete": True}
        ]
        parser.process_page(page2_notes, page_num=2)

        all_completed = parser.get_all_completed()

        assert len(all_completed) == 2
        assert all_completed[0].marker == "1"
        assert all_completed[1].marker == "2"

    def test_get_summary(self):
        """Test summary statistics."""
        parser = CrossPageFootnoteParser()

        # Single-page footnote
        page1_notes = [
            {"marker": "1", "content": "Single page note.", "is_complete": True}
        ]
        parser.process_page(page1_notes, page_num=1)

        # Multi-page footnote
        page2_notes = [
            {
                "marker": "a",
                "content": "Multi-page note starts",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page2_notes, page_num=2)

        page3_notes = [
            {
                "marker": None,
                "content": "and continues.",
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page3_notes, page_num=3)

        summary = parser.get_summary()

        assert summary["total_completed"] == 2
        assert summary["total_incomplete"] == 0
        assert summary["single_page_count"] == 1
        assert summary["multi_page_count"] == 1
        assert summary["average_confidence"] > 0.0

    def test_spatial_analysis_footnote_area(self):
        """Test spatial detection of footnote area."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "1", "content": "Incomplete note", "is_complete": False}
        ]
        parser.process_page(page1_notes, page_num=1)

        # Continuation in footnote area (bottom of page)
        page2_notes = [
            {
                "marker": None,
                "content": "continuation in footnote area.",
                "bbox": {"x0": 50, "y0": 700, "x1": 550, "y1": 750},  # Bottom area
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1
        # Spatial signal should increase confidence
        assert completed[0].continuation_confidence >= 0.70

    def test_multiple_footnotes_same_page(self):
        """Test processing multiple footnotes on same page."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "1", "content": "First footnote.", "is_complete": True},
            {"marker": "2", "content": "Second footnote.", "is_complete": True},
            {"marker": "3", "content": "Third footnote.", "is_complete": True},
        ]

        completed = parser.process_page(page1_notes, page_num=1)

        assert len(completed) == 3
        assert completed[0].marker == "1"
        assert completed[1].marker == "2"
        assert completed[2].marker == "3"

    def test_mixed_complete_and_incomplete(self):
        """Test page with both complete and incomplete footnotes."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "1", "content": "Complete footnote.", "is_complete": True},
            {
                "marker": "2",
                "content": "Incomplete footnote continues",
                "is_complete": False,
            },
        ]

        completed = parser.process_page(page1_notes, page_num=1)

        assert len(completed) == 1  # Only first completed
        assert completed[0].marker == "1"
        assert len(parser.incomplete_footnotes) == 1
        assert parser.incomplete_footnotes[0].marker == "2"


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_empty_page(self):
        """Test processing empty page (no footnotes)."""
        parser = CrossPageFootnoteParser()

        completed = parser.process_page([], page_num=1)

        assert len(completed) == 0
        assert len(parser.incomplete_footnotes) == 0

    def test_continuation_without_content(self):
        """Test continuation dict with empty content."""
        parser = CrossPageFootnoteParser()

        page1_notes = [{"marker": "1", "content": "Incomplete", "is_complete": False}]
        parser.process_page(page1_notes, page_num=1)

        # Empty continuation content
        page2_notes = [
            {
                "marker": None,
                "content": "",  # Empty
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)

        # Should not detect empty content as continuation
        assert len(completed) == 0

    def test_very_long_footnote_4_pages(self):
        """Test very long footnote spanning 4+ pages."""
        parser = CrossPageFootnoteParser()

        # Page 1
        page1_notes = [
            {
                "marker": "†",
                "content": "Very long editorial note starts",
                "is_complete": False,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        parser.process_page(page1_notes, page_num=1)

        # Pages 2-3: Continuations
        for page_num in [2, 3]:
            page_notes = [
                {
                    "marker": None,
                    "content": f"continuation on page {page_num}",
                    "is_complete": False,
                    "font_name": "TimesNewRoman",
                    "font_size": 9.0,
                }
            ]
            completed = parser.process_page(page_notes, page_num=page_num)
            assert len(completed) == 0  # Still incomplete

        # Page 4: Final continuation
        page4_notes = [
            {
                "marker": None,
                "content": "and finally ends on page 4.",
                "is_complete": True,
                "font_name": "TimesNewRoman",
                "font_size": 9.0,
            }
        ]
        completed = parser.process_page(page4_notes, page_num=4)

        assert len(completed) == 1
        assert completed[0].pages == [1, 2, 3, 4]
        assert "page 2" in completed[0].content
        assert "page 3" in completed[0].content
        assert "page 4" in completed[0].content

    def test_hyphen_at_page_break(self):
        """Test word hyphenation across page boundary."""
        parser = CrossPageFootnoteParser()

        page1_notes = [
            {"marker": "a", "content": "This is a hyphen-", "is_complete": False}
        ]
        parser.process_page(page1_notes, page_num=1)

        page2_notes = [{"marker": None, "content": "ated word across pages."}]
        completed = parser.process_page(page2_notes, page_num=2)

        assert len(completed) == 1
        # Hyphen should be removed
        assert "hyphenated word" in completed[0].content
        assert "hyphen- ated" not in completed[0].content


# =============================================================================
# NLTK-based Incomplete Detection Tests
# =============================================================================


class TestIsFootnoteIncomplete:
    """Test NLTK-based is_footnote_incomplete() function."""

    def test_empty_text(self):
        """Test handling of empty text."""
        incomplete, confidence, reason = is_footnote_incomplete("")
        assert incomplete is False
        assert confidence == 0.0
        assert reason == "empty_text"

    def test_very_short_text(self):
        """Test handling of very short text (< 5 chars)."""
        incomplete, confidence, reason = is_footnote_incomplete("to")
        assert incomplete is False
        assert confidence == 0.5
        assert reason == "text_too_short"

    def test_hyphenation_incomplete(self):
        """Test detection of hyphenated word at end (very strong signal)."""
        incomplete, confidence, reason = is_footnote_incomplete("concept-")
        assert incomplete is True
        assert confidence == 0.95
        assert reason == "hyphenation"

        # Test with context
        incomplete, confidence, reason = is_footnote_incomplete(
            "The notion of différance-"
        )
        assert incomplete is True
        assert confidence == 0.95
        assert reason == "hyphenation"

    @requires_nltk_tokenizer
    def test_incomplete_phrase_strong_signal(self):
        """Test incomplete phrases (refers to, according to, etc.)."""
        # These end with incomplete phrase patterns (high confidence)
        high_confidence_cases = [
            "The concept refers to",
            "The argument according to",
            "In order to",
            "Such as",
            "As well as",
            "Due to",
            "In terms of",
            "With respect to",
            "By means of",
        ]

        for text in high_confidence_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is True, f"Failed for: {text}"
            assert confidence >= 0.85, f"Low confidence for: {text} (got {confidence})"
            assert reason == "incomplete_phrase", f"Wrong reason for: {text}"

        # These contain incomplete phrases but not at the end (still incomplete via NLTK)
        nltk_cases = [
            "According to the principle",
            "In order to understand",
            "Due to the complexity",
        ]

        for text in nltk_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is True, f"Failed for: {text}"
            assert confidence >= 0.75, f"Low confidence for: {text} (got {confidence})"
            assert "nltk_incomplete" in reason, f"Wrong reason for: {text}"

    @requires_nltk_tokenizer
    def test_nltk_incomplete_no_punctuation(self):
        """Test NLTK detection of sentences without terminal punctuation."""
        test_cases = [
            "This is an incomplete thought",
            "The argument continues",
            "See the discussion below",
            "Compare with Heidegger's analysis",
        ]

        for text in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is True, f"Failed for: {text}"
            assert confidence >= 0.75, f"Low confidence for: {text}"
            assert "nltk_incomplete" in reason, f"Wrong reason for: {text}"

    @requires_nltk_tokenizer
    def test_nltk_incomplete_with_continuation_word(self):
        """Test NLTK + continuation word for stronger signal."""
        test_cases = [
            "The concept of",
            "Refers to",
            "According to",
            "In the context of",
            "With regard to",
        ]

        for text in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is True, f"Failed for: {text}"
            assert confidence >= 0.85, f"Low confidence for: {text}"
            # Should detect both NLTK and continuation word
            assert "continuation" in reason or "incomplete_phrase" in reason, (
                f"Wrong reason for: {text}"
            )

    def test_complete_with_period(self):
        """Test detection of complete sentences with terminal punctuation."""
        test_cases = [
            "See Martin Heidegger, Being and Time, p. 123.",
            "This is a complete sentence.",
            "Compare with Kant's First Critique!",
            "What is the meaning of Being?",
        ]

        for text in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is False, f"Failed for: {text}"
            assert confidence >= 0.85, f"Low confidence for: {text}"
            assert "complete" in reason, f"Wrong reason for: {text}"

    def test_complete_despite_continuation_word(self):
        """Test cases with continuation words but complete sentences."""
        # Longer sentences with continuation words should be marked complete
        test_cases = [
            "According to Martin Heidegger's fundamental ontology, Dasein is characterized.",
            "With respect to the Kantian critique, we must examine the categories.",
            "In terms of phenomenological reduction, the epoché is essential.",
        ]

        for text in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is False, f"Failed for: {text}"
            assert confidence >= 0.80, f"Low confidence for: {text}"

    def test_abbreviation_edge_case(self):
        """Test handling of abbreviations (Dr., e.g., i.e.)."""
        # Short text with abbreviation and continuation word
        test_cases = [
            ("See Dr. Smith", True),  # Likely incomplete
            ("See Dr. Smith's analysis.", False),  # Complete
            ("E.g. the concept of", True),  # Incomplete
            ("E.g., see the discussion in Chapter 3.", False),  # Complete
        ]

        for text, expected_incomplete in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete == expected_incomplete, (
                f"Wrong detection for: {text} (expected {expected_incomplete})"
            )

    @requires_nltk_tokenizer
    def test_multiple_sentences_complete(self):
        """Test footnotes with multiple complete sentences."""
        text = (
            "See Heidegger, Being and Time, p. 42. "
            "The concept of Dasein is central. "
            "Compare with Kant's notion of transcendental apperception."
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is False
        assert confidence >= 0.90
        assert "complete" in reason

    def test_multiple_sentences_incomplete_last(self):
        """Test footnotes with complete sentences but incomplete last sentence."""
        text = (
            "See Heidegger, Being and Time, p. 42. "
            "The concept of Dasein is central. "
            "This notion refers to"
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is True
        assert confidence >= 0.85

    def test_citation_patterns_complete(self):
        """Test common citation patterns (should be complete)."""
        test_cases = [
            "Kant, Critique of Pure Reason, B75.",
            "Heidegger (1927), pp. 123-145.",
            "See Derrida, Of Grammatology, trans. Spivak (1976).",
            "Cf. Husserl, Logical Investigations, §24.",
        ]

        for text in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete is False, f"Failed for: {text}"

    def test_greek_latin_terms(self):
        """Test handling of Greek/Latin terms and special characters."""
        test_cases = [
            ("The Greek term λόγος refers to", True),  # Incomplete
            (
                "The Latin phrase 'cogito ergo sum' means 'I think, therefore I am'.",
                False,
            ),
            ("Dasein (Da-sein) is", True),  # Incomplete
        ]

        for text, expected_incomplete in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete == expected_incomplete, f"Wrong detection for: {text}"

    def test_parenthetical_content(self):
        """Test handling of parenthetical content."""
        test_cases = [
            ("See the discussion in Chapter 3 (esp. pp. 45-67).", False),
            ("The concept (Ger. Begriff) refers to", True),
            ("Compare with Kant's view (B75).", False),
        ]

        for text, expected_incomplete in test_cases:
            incomplete, confidence, reason = is_footnote_incomplete(text)
            assert incomplete == expected_incomplete, f"Wrong detection for: {text}"

    def test_whitespace_normalization(self):
        """Test that whitespace is properly normalized."""
        test_cases = [
            "  The concept refers to  ",
            "The concept refers to",
            "\nThe concept refers to\n",
            "The\nconcept\nrefers\nto",
        ]

        # All should produce same result
        results = [is_footnote_incomplete(text) for text in test_cases]
        assert all(r[0] == results[0][0] for r in results), (
            "Inconsistent whitespace handling"
        )

    def test_performance_simple_text(self):
        """Test performance for simple cases (should be <1ms)."""
        import time

        text = "This is a test footnote that continues"
        iterations = 1000

        start = time.perf_counter()
        for _ in range(iterations):
            is_footnote_incomplete(text)
        end = time.perf_counter()

        avg_time_ms = ((end - start) / iterations) * 1000
        assert avg_time_ms < 1.0, f"Performance too slow: {avg_time_ms:.3f}ms per call"

    def test_caching_behavior(self):
        """Test LRU cache improves performance for repeated calls."""
        import time

        text = "This is a test footnote"

        # First call (cold)
        start = time.perf_counter()
        is_footnote_incomplete(text)
        cold_time = time.perf_counter() - start

        # Subsequent calls (cached)
        start = time.perf_counter()
        for _ in range(100):
            is_footnote_incomplete(text)
        cached_time = (time.perf_counter() - start) / 100

        # Cached calls should be much faster
        assert cached_time < cold_time / 10, "Caching not working effectively"


class TestAnalyzeFootnoteBatch:
    """Test batch analysis function."""

    def test_empty_batch(self):
        """Test batch analysis with empty list."""
        results = analyze_footnote_batch([])
        assert results == []

    def test_single_item_batch(self):
        """Test batch with single footnote."""
        results = analyze_footnote_batch(["Complete sentence."])
        assert len(results) == 1
        incomplete, confidence, reason = results[0]
        assert incomplete is False

    def test_mixed_batch(self):
        """Test batch with mix of complete and incomplete."""
        footnotes = [
            "Complete sentence.",
            "Incomplete refers to",
            "word-",
            "Another complete citation (p. 42).",
        ]

        results = analyze_footnote_batch(footnotes)
        assert len(results) == 4

        # Check expected results
        assert results[0][0] is False  # Complete
        assert results[1][0] is True  # Incomplete (refers to)
        assert results[2][0] is True  # Incomplete (hyphen)
        assert results[3][0] is False  # Complete

    def test_batch_consistency_with_individual(self):
        """Test batch results match individual calls."""
        footnotes = ["Test footnote one.", "Test footnote refers to", "hyphen-"]

        batch_results = analyze_footnote_batch(footnotes)
        individual_results = [is_footnote_incomplete(fn) for fn in footnotes]

        assert batch_results == individual_results

    def test_large_batch_performance(self):
        """Test performance with large batch."""
        import time

        # Create 100 diverse footnotes
        footnotes = [
            f"Footnote {i} with content." if i % 2 == 0 else f"Footnote {i} refers to"
            for i in range(100)
        ]

        start = time.perf_counter()
        results = analyze_footnote_batch(footnotes)
        elapsed = time.perf_counter() - start

        assert len(results) == 100
        assert elapsed < 0.1, f"Batch too slow: {elapsed:.3f}s for 100 items"


class TestGetIncompleteConfidenceThreshold:
    """Test confidence threshold utility."""

    def test_returns_expected_value(self):
        """Test returns 0.75 threshold."""
        threshold = get_incomplete_confidence_threshold()
        assert threshold == 0.75

    def test_threshold_application(self):
        """Test using threshold for filtering."""
        test_cases = [
            ("Complete.", False, 0.92),
            ("Incomplete refers to", True, 0.88),
            ("word-", True, 0.95),
            ("to", False, 0.5),  # Below threshold
        ]

        threshold = get_incomplete_confidence_threshold()

        for text, expected_incomplete, expected_conf in test_cases:
            incomplete, confidence, _ = is_footnote_incomplete(text)

            # Apply threshold filter
            is_high_confidence = confidence >= threshold

            if expected_incomplete and expected_conf >= threshold:
                assert incomplete and is_high_confidence, (
                    f"Should be high-confidence incomplete: {text}"
                )
            elif not expected_incomplete:
                # Complete cases should have high confidence
                assert not incomplete or confidence < threshold


class TestRealWorldScenarios:
    """Test with real-world scholarly footnote examples."""

    def test_derrida_footnote_incomplete(self):
        """Test Derrida-style translator footnote (incomplete)."""
        text = (
            "French: 'sous rature' (under erasure). Derrida's strategy of writing "
            "a word, crossing it out, and printing both word and deletion refers to"
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is True
        assert confidence >= 0.85

    def test_derrida_footnote_complete(self):
        """Test Derrida-style translator footnote (complete)."""
        text = (
            "French: 'sous rature' (under erasure). Derrida's strategy of writing "
            "a word, crossing it out, and printing both word and deletion. "
            "See Gayatri Spivak's introduction, p. xvii."
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is False
        assert confidence >= 0.85

    def test_kant_scholarly_apparatus(self):
        """Test Kant critical edition footnote."""
        text = (
            "a. This is the translator's gloss on Kant's term 'Aufhebung', "
            "which in German means both 'to cancel' and 'to preserve'. "
            "The dialectical nature of the concept"
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is True
        assert confidence >= 0.75

    def test_heidegger_multilingual_reference(self):
        """Test Heidegger footnote with German terms."""
        text = (
            "German: 'Dasein' (being-there). The term cannot be adequately "
            "translated into English. See Being and Time, §9."
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is False

    def test_cross_reference_incomplete(self):
        """Test cross-reference that continues."""
        text = "See Chapter 3, especially the discussion of"
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is True
        assert confidence >= 0.75

    def test_multi_paragraph_footnote_incomplete(self):
        """Test multi-paragraph footnote (incomplete at paragraph break)."""
        text = (
            "The concept of différance is central to Derrida's deconstruction. "
            "It combines 'difference' and 'deferral' to indicate the play of "
            "signification that exceeds binary oppositions.\n\n"
            "This neologism appears throughout"
        )
        incomplete, confidence, reason = is_footnote_incomplete(text)
        assert incomplete is True

    def test_accuracy_on_diverse_corpus(self):
        """Test overall accuracy on diverse footnote corpus."""
        # Ground truth: (text, is_incomplete)
        corpus = [
            ("See Kant, CPR, B75.", False),
            ("The German term refers to", True),
            ("Complete citation (Heidegger 1927).", False),
            ("According to the principle", True),
            ("hyphen-", True),
            ("See the discussion in §24.", False),
            ("In order to", True),
            ("Cf. Derrida, OG, p. 42.", False),
            ("The concept of", True),
            ("Translation by Spivak.", False),
        ]

        correct = 0
        total = len(corpus)

        for text, expected_incomplete in corpus:
            incomplete, confidence, _ = is_footnote_incomplete(text)
            if incomplete == expected_incomplete:
                correct += 1

        accuracy = correct / total
        assert accuracy >= 0.95, f"Accuracy too low: {accuracy * 100:.1f}%"
