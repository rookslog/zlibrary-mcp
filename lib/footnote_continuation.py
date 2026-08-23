"""
Cross-page footnote continuation tracking for scholarly document processing.

This module implements a state machine for parsing footnotes that span multiple pages,
a common occurrence in scholarly texts with extensive commentary (e.g., translator glosses,
editorial apparatus in critical editions).

Design Philosophy:
    "Preserve complete scholarly notes" - Multi-page footnotes represent unitary
    scholarly arguments that must not be fragmented by page boundaries.

Architecture:
    - State Machine: Tracks incomplete footnotes across page boundaries
    - Continuation Detection: Identifies orphaned content belonging to previous notes
    - Confidence Scoring: Quantifies linking quality between parts
    - Font Matching: Uses typographic consistency as continuation signal

Key Use Cases:
    1. Long translator glosses spanning 2-3 pages (Kant translations)
    2. Extensive editorial commentary in critical editions
    3. Multi-paragraph author footnotes with complex arguments
    4. Notes interrupted by page breaks mid-sentence

Limitations (v1.0):
    - Handles single incomplete footnote at a time
    - v1.1 will support multiple simultaneous incomplete notes
    - Requires spatial metadata (bbox) for robust detection

Created: 2025-10-28 (Phase 3 of RAG architecture enhancement)

Example Usage:
    from lib.footnote_continuation import (
        CrossPageFootnoteParser,
        FootnoteWithContinuation
    )
    from lib.rag_data_models import NoteSource

    parser = CrossPageFootnoteParser()

    # Process each page sequentially
    for page_num, page_footnotes in enumerate(document_pages, start=1):
        completed = parser.process_page(page_footnotes, page_num)
        for footnote in completed:
            print(f"Footnote {footnote.marker}: {footnote.content}")
            print(f"  Pages: {footnote.pages}")
            print(f"  Complete: {footnote.is_complete}")
            print(f"  Confidence: {footnote.continuation_confidence}")

    # Handle any remaining incomplete at document end
    final_notes = parser.finalize()
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set, Tuple
from enum import Enum
from functools import lru_cache
import re
import logging

from lib.rag_data_models import NoteSource


# =============================================================================
# Logging
# =============================================================================

logger = logging.getLogger(__name__)


# =============================================================================
# NLTK Initialization (Eager Loading for Performance)
# =============================================================================
# Performance Note: Eager import moves 658ms cost from footnote detection
# to module load time, reducing per-page overhead by ~44ms.

try:
    from nltk.tokenize import sent_tokenize as _sent_tokenize

    # Calling the tokenizer is the readiness check: current NLTK releases need
    # both punkt and punkt_tab. Never download data at import/runtime; a RAG
    # operation must remain deterministic and usable offline.
    try:
        _sent_tokenize('Tokenizer readiness probe.')
    except LookupError:
        logger.warning(
            "NLTK tokenizer data is unavailable. Using deterministic sentence "
            "boundary fallback for incomplete footnote detection."
        )
        _nltk_ready = False
    else:
        _nltk_ready = True
except ImportError:
    logger.warning(
        "NLTK is unavailable. Using deterministic sentence boundary fallback "
        "for incomplete footnote detection."
    )
    _nltk_ready = False
    _sent_tokenize = None


def _ensure_nltk_data() -> None:
    """
    DEPRECATED: NLTK now imported eagerly at module load.

    This function is kept for compatibility but is now a no-op.
    The global _nltk_ready flag indicates if NLTK is available.
    """
    pass  # NLTK initialization happens at module load


def _fallback_sent_tokenize(text: str) -> List[str]:
    """Split on explicit sentence endings without external tokenizer data."""
    return [
        sentence.strip()
        for sentence in re.split(r'(?<=[.!?])\s+', text)
        if sentence.strip()
    ]


def _tokenize_sentences(text: str) -> Tuple[List[str], bool]:
    """Return sentences plus whether the NLTK tokenizer produced them."""
    global _nltk_ready

    if _nltk_ready and _sent_tokenize is not None:
        try:
            return (_sent_tokenize(text), True)
        except LookupError:
            # Tokenizer data can disappear after import (for example, a
            # short-lived mounted data directory). Only that known condition
            # falls back; unrelated NLTK errors remain visible.
            logger.warning(
                "NLTK tokenizer data became unavailable. Using deterministic "
                "sentence boundary fallback."
            )
            _nltk_ready = False

    return (_fallback_sent_tokenize(text), False)


# =============================================================================
# Incomplete Footnote Detection (NLTK-based)
# =============================================================================

# Regex patterns for incomplete detection
HYPHENATION_PATTERN = re.compile(r'-\s*$')  # Word ends with hyphen
CONTINUATION_WORDS = re.compile(
    r'\b(to|and|or|but|yet|for|nor|so|of|in|on|at|with|from|by|about|as|into|through|during|before|after|above|below|between|among|under|over)\s*$',
    re.IGNORECASE
)
INCOMPLETE_PHRASES = re.compile(
    r'\b(refers to|according to|in order to|such as|as well as|in addition to|due to|because of|in terms of|with respect to|by means of)\s*$',
    re.IGNORECASE
)


@lru_cache(maxsize=1024)
def is_footnote_incomplete(text: str) -> Tuple[bool, float, str]:
    """
    Detect incomplete footnotes using NLTK + regex patterns.

    Strategy:
    1. NLTK sentence boundary detection (primary)
    2. Regex patterns for obvious cases (supplement)
    3. Confidence scoring based on signal strength

    Args:
        text: Footnote content to analyze (whitespace will be normalized)

    Returns:
        Tuple of (is_incomplete, confidence, reason):
        - is_incomplete: True if footnote appears to continue on next page
        - confidence: Float 0.0-1.0 indicating detection confidence
        - reason: String describing detection rationale

    Confidence Levels:
        - 0.95-1.0: Very strong signal (hyphenation)
        - 0.85-0.95: Strong signal (NLTK + pattern)
        - 0.75-0.85: Good signal (NLTK incomplete)
        - 0.60-0.75: Moderate signal (continuation word only)
        - <0.60: Weak/complete

    Examples:
        >>> is_footnote_incomplete("The concept refers to")
        (True, 0.88, 'nltk_incomplete+continuation_phrase')

        >>> is_footnote_incomplete("See Dr. Smith's work.")
        (False, 0.92, 'nltk_complete')

        >>> is_footnote_incomplete("concept-")
        (True, 0.95, 'hyphenation')

        >>> is_footnote_incomplete("")
        (False, 0.0, 'empty_text')
    """
    # Ensure NLTK is ready
    _ensure_nltk_data()

    # Normalize whitespace
    text = text.strip()

    # Edge cases
    if not text:
        return (False, 0.0, 'empty_text')

    if len(text) < 5:  # Too short to analyze meaningfully
        return (False, 0.5, 'text_too_short')

    # Pattern-based checks (high confidence signals)

    # 1. Hyphenation at end (very strong signal) - CHECK FIRST before short gloss
    # This ensures "concept-" returns (True, 0.95, 'hyphenation') not short_gloss_complete
    if HYPHENATION_PATTERN.search(text):
        return (True, 0.95, 'hyphenation')

    # ISSUE-FN-004 FIX: Short glosses/single-word footnotes are complete
    # German word glosses like "Rechtmässigkeit", "Kenntnisse", "Principien"
    # should NOT be marked as incomplete just because they lack punctuation.
    # Heuristic: Single word or very short text (< 30 chars) without continuation
    # words at the end is likely a complete gloss, not an incomplete sentence.
    # NOTE: This check must come AFTER hyphenation check to avoid false negatives.
    word_count = len(text.split())
    if word_count <= 2 and len(text) < 30:
        # Check if it ends with a continuation word (would indicate incomplete)
        if not CONTINUATION_WORDS.search(text):
            return (False, 0.85, 'short_gloss_complete')

    # 2. Incomplete phrases (strong signal independent of tokenizer data)
    if INCOMPLETE_PHRASES.search(text):
        return (True, 0.90, 'incomplete_phrase')

    # 3. Sentence boundary analysis (NLTK when ready; deterministic fallback)
    sentences, used_nltk = _tokenize_sentences(text)

    if not sentences:
        return (False, 0.5, 'no_sentences_detected')

    last_sentence = sentences[-1].strip()

    # Check if last sentence is complete
    has_terminal_punctuation = last_sentence and last_sentence[-1] in '.!?'

    # If no terminal punctuation, likely incomplete
    if not has_terminal_punctuation:
        # Check for continuation words to strengthen signal
        if CONTINUATION_WORDS.search(text):
            if used_nltk:
                return (True, 0.88, 'nltk_incomplete+continuation_word')
            return (True, 0.82, 'fallback_incomplete+continuation_word')
        if used_nltk:
            return (True, 0.80, 'nltk_incomplete')
        return (True, 0.75, 'fallback_incomplete')

    # Has terminal punctuation - check for continuation words anyway
    # (could be mid-sentence punctuation like "Dr." or "e.g.")
    if CONTINUATION_WORDS.search(text):
        # Ambiguous case - has punctuation but ends with continuation word
        # This could be "See Dr. Smith" (incomplete) or "According to Dr. Smith." (complete)

        # Heuristic: If the last "sentence" is very short and ends with continuation word,
        # likely incomplete despite punctuation
        if len(last_sentence.split()) <= 4:
            reason = (
                'continuation_word_with_punctuation'
                if used_nltk
                else 'fallback_continuation_word_with_punctuation'
            )
            return (True, 0.70, reason)
        else:
            # Longer sentence with terminal punctuation - likely complete
            reason = (
                'nltk_complete_despite_continuation_word'
                if used_nltk
                else 'fallback_complete_despite_continuation_word'
            )
            return (False, 0.85, reason)

    # Clean sentence boundary with terminal punctuation
    if used_nltk:
        return (False, 0.92, 'nltk_complete')
    return (False, 0.85, 'fallback_complete')


def analyze_footnote_batch(footnotes: List[str]) -> List[Tuple[bool, float, str]]:
    """
    Analyze multiple footnotes for incomplete detection.

    More efficient than individual calls for large batches due to LRU caching.

    Args:
        footnotes: List of footnote text strings

    Returns:
        List of (is_incomplete, confidence, reason) tuples

    Example:
        >>> results = analyze_footnote_batch([
        ...     "Complete sentence.",
        ...     "Incomplete refers to",
        ...     "word-"
        ... ])
        >>> len(results)
        3
    """
    return [is_footnote_incomplete(fn) for fn in footnotes]


def get_incomplete_confidence_threshold() -> float:
    """
    Get the recommended confidence threshold for production use.

    Returns:
        Float threshold (0.75) - footnotes with confidence >= this are considered incomplete

    Example:
        >>> threshold = get_incomplete_confidence_threshold()
        >>> threshold
        0.75
    """
    return 0.75


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class FootnoteWithContinuation:
    """
    Extended footnote data structure supporting multi-page notes.

    Attributes:
        marker: Note identifier ("1", "a", "*", etc.)
        content: Accumulated text content across all pages
        pages: List of page numbers where this footnote appears
        bboxes: List of bounding boxes (one per page where note appears)
        is_complete: True if footnote is finished (marker found on next page or doc end)
        continuation_confidence: Quality score for multi-page linking (0.0-1.0)
        note_source: Classification (AUTHOR, TRANSLATOR, EDITOR, UNKNOWN)
        classification_confidence: Confidence in source classification (0.0-1.0)
        classification_method: Strategy used for classification
        font_name: Font family for typographic matching
        font_size: Font size for typographic matching

    Design Notes:
        - Spatial metadata (bboxes) enables robust continuation detection
        - Font metadata enables typographic consistency validation
        - Confidence scoring enables downstream filtering/validation
        - Single marker with multiple pages represents ONE logical note

    Example:
        # Single-page footnote
        FootnoteWithContinuation(
            marker="1",
            content="Brief citation to Heidegger p. 42.",
            pages=[5],
            bboxes=[{'x0': 50, 'y0': 700, 'x1': 550, 'y1': 720}],
            is_complete=True,
            continuation_confidence=1.0,
            note_source=NoteSource.AUTHOR
        )

        # Multi-page footnote (long translator gloss)
        FootnoteWithContinuation(
            marker="a",
            content="German: 'Aufhebung' (sublation). This concept... [continues]",
            pages=[64, 65],
            bboxes=[
                {'x0': 50, 'y0': 700, 'x1': 550, 'y1': 780},  # Page 64 (incomplete)
                {'x0': 50, 'y0': 50, 'x1': 550, 'y1': 120}     # Page 65 (completion)
            ],
            is_complete=True,
            continuation_confidence=0.92,  # High confidence linking
            note_source=NoteSource.TRANSLATOR,
            font_name="TimesNewRoman",
            font_size=9.0
        )
    """
    marker: str
    content: str
    pages: List[int] = field(default_factory=list)
    bboxes: List[Dict] = field(default_factory=list)
    is_complete: bool = True
    continuation_confidence: float = 1.0
    note_source: NoteSource = NoteSource.UNKNOWN
    classification_confidence: float = 1.0
    classification_method: str = "schema_based"
    font_name: str = ""
    font_size: float = 0.0

    def append_continuation(
        self,
        additional_content: str,
        page_num: int,
        bbox: Optional[Dict] = None,
        confidence: float = 1.0
    ) -> None:
        """
        Append continuation content from next page.

        Args:
            additional_content: Text to append
            page_num: Page where continuation appears
            bbox: Bounding box of continuation content
            confidence: Confidence in the continuation link (0.0-1.0)

        Effects:
            - Appends content with space separator
            - Adds page to pages list
            - Adds bbox to bboxes list (if provided)
            - Updates continuation_confidence (minimum of all links)
        """
        # Smart spacing: add space only if content doesn't end with hyphen
        if self.content and not self.content.rstrip().endswith('-'):
            self.content += ' '
        elif self.content.rstrip().endswith('-'):
            # Remove hyphen for word continuation
            self.content = self.content.rstrip()[:-1]

        self.content += additional_content
        self.pages.append(page_num)

        if bbox is not None:
            self.bboxes.append(bbox)

        # Update confidence: take minimum (weakest link determines quality)
        self.continuation_confidence = min(self.continuation_confidence, confidence)

    def get_summary(self) -> str:
        """
        Get human-readable summary of footnote.

        Returns:
            String describing footnote status and metadata

        Example:
            "Footnote 'a': 2 pages (64-65), TRANSLATOR, complete (conf: 0.92)"
            "Footnote '1': 1 page (5), AUTHOR, complete (conf: 1.00)"
            "Footnote '*': 1 page (120), EDITOR, incomplete (conf: 1.00)"
        """
        page_range = f"{min(self.pages)}-{max(self.pages)}" if len(self.pages) > 1 else str(self.pages[0])
        status = "complete" if self.is_complete else "incomplete"
        return (
            f"Footnote '{self.marker}': {len(self.pages)} page(s) ({page_range}), "
            f"{self.note_source.name}, {status} "
            f"(conf: {self.continuation_confidence:.2f})"
        )


# =============================================================================
# Continuation Detection Signals
# =============================================================================

class ContinuationSignal(Enum):
    """Signals indicating content is a continuation of previous footnote."""
    NO_MARKER_AT_START = "no_marker"          # No footnote marker at beginning
    STARTS_LOWERCASE = "starts_lowercase"     # Starts with lowercase letter
    STARTS_CONJUNCTION = "starts_conjunction" # Starts with and, but, or, etc.
    IN_FOOTNOTE_AREA = "in_footnote_area"    # Spatial: in footnote region
    FONT_MATCHES = "font_matches"             # Font matches incomplete footnote
    SEQUENCE_FOLLOWS = "sequence_follows"     # No intervening marker detected


# Conjunction patterns indicating continuation
CONTINUATION_CONJUNCTIONS = {
    'and', 'but', 'or', 'however', 'moreover', 'furthermore', 'nevertheless',
    'thus', 'therefore', 'hence', 'consequently', 'accordingly', 'besides'
}

# Lowercase start pattern (excluding punctuation)
LOWERCASE_START_PATTERN = re.compile(r'^\s*[a-z]')


# =============================================================================
# Cross-Page Footnote Parser (State Machine)
# =============================================================================

class CrossPageFootnoteParser:
    """
    State machine for parsing footnotes across multiple pages.

    Maintains state for incomplete footnotes and merges continuations as they appear.
    Handles edge cases including false positives, very long notes, and orphaned content.

    State Transitions:
        1. NEW → INCOMPLETE: Footnote starts but doesn't complete on page
        2. INCOMPLETE → COMPLETE: Continuation found and merged
        3. INCOMPLETE → ORPHANED: Next page has new marker, no continuation
        4. COMPLETE → (output): Footnote finished and returned

    Design Decisions:
        - Single incomplete footnote tracking (v1.0 limitation)
        - Confidence scoring for continuation quality assessment
        - Font similarity as primary continuation signal
        - Spatial analysis (footnote area detection) as secondary signal
        - Sequence order validation as fallback

    Limitations (v1.0):
        - Tracks only one incomplete footnote at a time
        - Multiple simultaneous incomplete notes will be deferred to v1.1
        - Requires spatial metadata (bbox) for robust detection
        - Font metadata optional but recommended for confidence

    Example Usage:
        parser = CrossPageFootnoteParser()

        # Page 1: Footnote starts but incomplete
        page1_notes = [
            {
                'marker': '*',
                'content': 'This is a long translator note that continues',
                'is_complete': False,
                'bbox': {'x0': 50, 'y0': 700, 'x1': 550, 'y1': 780},
                'font_name': 'TimesNewRoman',
                'font_size': 9.0
            }
        ]
        completed = parser.process_page(page1_notes, page_num=1)
        # completed = [] (none finished yet)

        # Page 2: Continuation found
        page2_notes = [
            {
                'marker': None,  # No marker = continuation
                'content': 'onto the next page with more context.',
                'is_continuation': True,
                'bbox': {'x0': 50, 'y0': 50, 'x1': 550, 'y1': 70},
                'font_name': 'TimesNewRoman',
                'font_size': 9.0
            }
        ]
        completed = parser.process_page(page2_notes, page_num=2)
        # completed = [FootnoteWithContinuation(marker='*', pages=[1, 2], ...)]
    """

    def __init__(self):
        """Initialize parser with empty state."""
        self.incomplete_footnotes: List[FootnoteWithContinuation] = []
        self.completed_footnotes: List[FootnoteWithContinuation] = []
        self.last_seen_markers: Set[str] = set()  # Track markers seen on this page

    def process_page(
        self,
        page_footnotes: List[Dict],
        page_num: int
    ) -> List[FootnoteWithContinuation]:
        """
        Process single page of footnotes, handling continuations from previous pages.

        Algorithm:
            1. Check for continuations of incomplete footnotes
            2. Detect new footnotes on this page
            3. Mark each as complete or incomplete
            4. Return completed footnotes

        Args:
            page_footnotes: List of footnote dicts from current page
                Expected keys:
                    - 'marker': str or None (None = continuation candidate)
                    - 'content': str (footnote text)
                    - 'is_complete': bool (optional, inferred if missing)
                    - 'bbox': Dict (optional but recommended)
                    - 'font_name': str (optional but recommended)
                    - 'font_size': float (optional but recommended)
                    - 'note_source': NoteSource (optional)
                    - 'classification_confidence': float (optional)

            page_num: Current page number (1-indexed)

        Returns:
            List of FootnoteWithContinuation objects that completed on this page

        Side Effects:
            - Updates self.incomplete_footnotes
            - Updates self.completed_footnotes
            - Updates self.last_seen_markers

        Edge Cases:
            1. False incomplete: Previous page footnote marked incomplete, but
               current page has new marker with no continuation → mark previous complete
            2. Orphaned content: Continuation candidate but no incomplete footnote
               → log warning, skip content
            3. Multiple candidates: >1 continuation candidate → use first, log warning
        """
        completed_this_page: List[FootnoteWithContinuation] = []
        current_page_markers: Set[str] = set()

        logger.debug(f"Processing page {page_num}: {len(page_footnotes)} footnotes")

        # Step 1: Check for continuations of incomplete footnotes
        continuation_found = False
        if self.incomplete_footnotes:
            continuation_content = self._detect_continuation_content(page_footnotes)

            if continuation_content is not None:
                # Merge continuation with incomplete footnote
                incomplete = self.incomplete_footnotes[0]  # v1.0: single incomplete only
                confidence = continuation_content.get('continuation_confidence', 0.85)

                incomplete.append_continuation(
                    additional_content=continuation_content['content'],
                    page_num=page_num,
                    bbox=continuation_content.get('bbox'),
                    confidence=confidence
                )

                logger.info(
                    f"Continuation merged for footnote '{incomplete.marker}' "
                    f"(pages: {incomplete.pages})"
                )
                continuation_found = True

                # Check if continuation itself is complete
                if continuation_content.get('is_complete', True):
                    # Footnote finished on this page
                    incomplete.is_complete = True
                    completed_this_page.append(incomplete)
                    self.completed_footnotes.append(incomplete)
                    self.incomplete_footnotes.remove(incomplete)
                    logger.info(f"Footnote '{incomplete.marker}' completed on page {page_num}")

        # Step 2: Process new footnotes on this page
        for footnote_dict in page_footnotes:
            # CRITICAL FIX: Use actual_marker from corruption recovery if available
            # Corruption recovery corrects corrupted symbols (e.g., 'a' → '*', 't' → '†')
            # If corruption recovery has run, use actual_marker; otherwise use raw marker
            marker = footnote_dict.get('actual_marker', footnote_dict.get('marker'))

            # Skip continuation content (already processed)
            if marker is None and continuation_found:
                continue

            # Skip if this was detected as continuation
            if footnote_dict.get('is_continuation', False):
                continue

            if marker is not None:
                current_page_markers.add(marker)

                # Create FootnoteWithContinuation object
                footnote = FootnoteWithContinuation(
                    marker=marker,  # Now uses corrected marker from corruption recovery
                    content=footnote_dict.get('content', ''),
                    pages=[page_num],
                    bboxes=[footnote_dict.get('bbox', {})] if 'bbox' in footnote_dict else [],
                    is_complete=footnote_dict.get('is_complete', True),
                    continuation_confidence=1.0,  # New note, full confidence
                    note_source=footnote_dict.get('note_source', NoteSource.UNKNOWN),
                    classification_confidence=footnote_dict.get('classification_confidence', 1.0),
                    classification_method=footnote_dict.get('classification_method', 'schema_based'),
                    font_name=footnote_dict.get('font_name', ''),
                    font_size=footnote_dict.get('font_size', 0.0)
                )

                # Step 3: Determine if complete or incomplete
                if footnote.is_complete:
                    completed_this_page.append(footnote)
                    self.completed_footnotes.append(footnote)
                else:
                    self.incomplete_footnotes.append(footnote)
                    logger.info(
                        f"Footnote '{marker}' started on page {page_num} but incomplete"
                    )

        # Step 4: Handle false incomplete (previous incomplete but new marker on this page)
        # If there are incomplete footnotes from previous pages AND new markers appear
        # without continuation, mark old incomplete as complete (false positive)
        if self.incomplete_footnotes and current_page_markers and not continuation_found:
            # Check if there are old incomplete footnotes (from previous pages)
            # If new markers appeared without continuation, previous incomplete was false positive
            false_incomplete = []
            for incomplete in list(self.incomplete_footnotes):
                # Only process footnotes not from current page
                if page_num not in incomplete.pages:
                    logger.warning(
                        f"Footnote '{incomplete.marker}' marked incomplete but no continuation found. "
                        f"Marking as complete (false incomplete detection)."
                    )
                    incomplete.is_complete = True
                    false_incomplete.append(incomplete)
                    self.completed_footnotes.append(incomplete)
                    self.incomplete_footnotes.remove(incomplete)

            # Prepend false incomplete to maintain chronological order
            # (older footnotes appear first in returned list)
            completed_this_page = false_incomplete + completed_this_page

        # Update state
        self.last_seen_markers = current_page_markers

        logger.debug(
            f"Page {page_num} processed: {len(completed_this_page)} completed, "
            f"{len(self.incomplete_footnotes)} incomplete"
        )

        return completed_this_page

    def _detect_continuation_content(
        self,
        page_footnotes: List[Dict]
    ) -> Optional[Dict]:
        """
        Detect orphaned content that continues previous footnote.

        Continuation Signals (ranked by reliability):
            1. Font matches incomplete footnote (HIGH confidence: 0.92)
            2. In footnote area + no marker (MEDIUM confidence: 0.85)
            3. Starts lowercase/conjunction + no marker (LOW confidence: 0.70)
            4. Sequence order (no intervening marker) (BASELINE: 0.65)

        Args:
            page_footnotes: List of footnote dicts from current page

        Returns:
            Continuation dict with keys:
                - 'content': str (continuation text)
                - 'bbox': Dict (optional)
                - 'font_name': str (optional)
                - 'font_size': float (optional)
                - 'continuation_confidence': float (0.0-1.0)
                - 'signals': List[ContinuationSignal] (detected signals)
            Returns None if no continuation detected

        Edge Cases:
            1. Multiple candidates: Return first with highest confidence, log warning
            2. Orphaned content without incomplete: Return None, log warning
            3. Ambiguous signals: Use confidence scoring to decide
        """
        if not self.incomplete_footnotes:
            return None

        incomplete = self.incomplete_footnotes[0]  # v1.0: single incomplete
        candidates = []

        for footnote_dict in page_footnotes:
            marker = footnote_dict.get('marker')
            content = footnote_dict.get('content', '').strip()

            # Skip if has marker (not a continuation)
            if marker is not None:
                continue

            # Skip empty content
            if not content:
                continue

            # Detect continuation signals
            signals: List[ContinuationSignal] = []
            confidence = 0.65  # Baseline confidence

            # Signal 1: No marker at start (required)
            signals.append(ContinuationSignal.NO_MARKER_AT_START)

            # Signal 2: Starts lowercase
            if LOWERCASE_START_PATTERN.match(content):
                signals.append(ContinuationSignal.STARTS_LOWERCASE)
                confidence += 0.05

            # Signal 3: Starts with conjunction
            first_word = content.split()[0].lower() if content else ''
            if first_word in CONTINUATION_CONJUNCTIONS:
                signals.append(ContinuationSignal.STARTS_CONJUNCTION)
                confidence += 0.10

            # Signal 4: Font matches (HIGH confidence signal)
            font_name = footnote_dict.get('font_name', '')
            font_size = footnote_dict.get('font_size', 0.0)
            if font_name and incomplete.font_name:
                if font_name == incomplete.font_name:
                    signals.append(ContinuationSignal.FONT_MATCHES)
                    # Font match is strong signal
                    if abs(font_size - incomplete.font_size) < 0.5:
                        confidence = 0.92  # High confidence
                    else:
                        confidence = 0.80  # Font name matches but size differs

            # Signal 5: In footnote area (spatial analysis)
            bbox = footnote_dict.get('bbox')
            if bbox and self._is_in_footnote_area(bbox):
                signals.append(ContinuationSignal.IN_FOOTNOTE_AREA)
                confidence += 0.10

            # Signal 6: Sequence follows (no intervening marker)
            if not self.last_seen_markers or len(self.last_seen_markers) == 0:
                signals.append(ContinuationSignal.SEQUENCE_FOLLOWS)
                confidence += 0.05

            # Cap confidence at 1.0
            confidence = min(confidence, 1.0)

            # Require minimum confidence threshold
            if confidence >= 0.65:
                candidates.append({
                    'content': content,
                    'bbox': bbox,
                    'font_name': font_name,
                    'font_size': font_size,
                    'continuation_confidence': confidence,
                    'signals': signals,
                    'is_complete': footnote_dict.get('is_complete', True)
                })

        if not candidates:
            return None

        # Multiple candidates: take highest confidence
        if len(candidates) > 1:
            logger.warning(
                f"Multiple continuation candidates detected ({len(candidates)}). "
                f"Using highest confidence candidate."
            )
            candidates.sort(key=lambda c: c['continuation_confidence'], reverse=True)

        best_candidate = candidates[0]
        logger.debug(
            f"Continuation detected (confidence: {best_candidate['continuation_confidence']:.2f}, "
            f"signals: {[s.value for s in best_candidate['signals']]})"
        )

        return best_candidate

    def _is_in_footnote_area(self, bbox: Dict) -> bool:
        """
        Check if bounding box is in typical footnote area (bottom of page).

        Heuristic: Footnotes typically appear in bottom 20% of page.
        Assumes standard page height ~792 points (US Letter).

        Args:
            bbox: Bounding box dict with keys 'x0', 'y0', 'x1', 'y1'

        Returns:
            True if in footnote area, False otherwise

        Note:
            This is a heuristic and may need adjustment for different page layouts.
            Consider making threshold configurable in future versions.
        """
        if not bbox or 'y1' not in bbox:
            return False

        # Footnote area threshold (bottom 20% of standard page)
        FOOTNOTE_THRESHOLD_Y = 632  # 792 * 0.8 (top 80% is body, bottom 20% is footnote area)

        # Check if bbox is in bottom region
        return bbox['y1'] > FOOTNOTE_THRESHOLD_Y

    def finalize(self) -> List[FootnoteWithContinuation]:
        """
        Call at end of document to handle remaining incomplete footnotes.

        At document end, any incomplete footnotes are marked as complete
        (no more pages to continue).

        Returns:
            List of any remaining footnotes marked as complete

        Side Effects:
            - Marks all incomplete footnotes as complete
            - Moves them to completed_footnotes list
            - Clears incomplete_footnotes list
        """
        remaining = []

        for incomplete in self.incomplete_footnotes:
            logger.info(
                f"Document end: marking footnote '{incomplete.marker}' as complete "
                f"(was incomplete on page(s) {incomplete.pages})"
            )
            incomplete.is_complete = True
            remaining.append(incomplete)
            self.completed_footnotes.append(incomplete)

        self.incomplete_footnotes.clear()

        return remaining

    def get_all_completed(self) -> List[FootnoteWithContinuation]:
        """
        Get all completed footnotes processed so far.

        Returns:
            List of all completed FootnoteWithContinuation objects
        """
        return self.completed_footnotes.copy()

    def get_summary(self) -> Dict:
        """
        Get processing summary statistics.

        Returns:
            Dictionary with summary statistics:
                - 'total_completed': int
                - 'total_incomplete': int
                - 'multi_page_count': int (footnotes spanning multiple pages)
                - 'single_page_count': int
                - 'average_confidence': float
        """
        multi_page_count = sum(1 for fn in self.completed_footnotes if len(fn.pages) > 1)
        single_page_count = len(self.completed_footnotes) - multi_page_count

        avg_confidence = (
            sum(fn.continuation_confidence for fn in self.completed_footnotes) / len(self.completed_footnotes)
            if self.completed_footnotes else 0.0
        )

        return {
            'total_completed': len(self.completed_footnotes),
            'total_incomplete': len(self.incomplete_footnotes),
            'multi_page_count': multi_page_count,
            'single_page_count': single_page_count,
            'average_confidence': avg_confidence
        }


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # NLTK-based incomplete detection
    'is_footnote_incomplete',
    'analyze_footnote_batch',
    'get_incomplete_confidence_threshold',
    # Cross-page tracking
    'FootnoteWithContinuation',
    'CrossPageFootnoteParser',
    'ContinuationSignal',
]
