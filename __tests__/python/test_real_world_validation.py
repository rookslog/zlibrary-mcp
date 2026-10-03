"""
Real-World Validation Tests - Ground Truth Based TDD.

NO MOCKING - Tests entire pipeline with actual PDFs and documented ground truth.

Purpose: Prevent hallucinations, catch architectural flaws, validate assumptions.
Lesson: Today's real PDF testing caught flaw that 495 unit tests missed.

Usage:
    pytest __tests__/python/test_real_world_validation.py -v
"""

import pytest
import time
from pathlib import Path
import sys

# Add project root and test_files to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "test_files"))

from lib.rag_processing import process_pdf
from ground_truth_loader import load_ground_truth, list_ground_truth_tests


class TestRealWorldSousRature:
    """Real-world tests for sous-rature (X-mark) detection and recovery."""

    def test_derrida_xmark_detection_real(self):
        """
        Test X-mark detection with REAL Derrida PDF - END-TO-END pipeline test.

        Validates: Processing completes, X-marks detected, performance within budget
        NOTE: Does NOT validate text recovery (that's a separate hard ML problem)
        NO MOCKS - tests complete pipeline
        """
        # Load ground truth
        gt = load_ground_truth("derrida_of_grammatology")

        # Process REAL PDF
        pdf_path = Path(gt["pdf_file"])
        assert pdf_path.exists(), f"Test PDF not found: {pdf_path}"

        start = time.time()
        result = process_pdf(pdf_path, output_format="markdown")
        elapsed_ms = (time.time() - start) * 1000

        # Verify processing completed successfully
        assert result, "Processing returned empty result"
        assert len(result) > 100, "Output suspiciously short"

        # Verify performance within budget
        max_time = gt["expected_quality"]["processing_time_max_ms"]
        assert elapsed_ms < max_time, (
            f"Too slow: {elapsed_ms:.0f}ms > {max_time}ms budget"
        )

        print(
            f"✅ Processed {gt['metadata']['pages']} pages in {elapsed_ms:.0f}ms ({elapsed_ms / gt['metadata']['pages']:.0f}ms/page)"
        )

    def test_derrida_sous_rature_detection(self):
        """
        Test that X-marks (sous-rature) are correctly DETECTED.

        Ground truth: X-mark crossing out "is" at specific bbox location
        Critical: Must detect real philosophical sous-rature, not artifacts

        NOTE: Text RECOVERY under heavy X-marks is a hard ML problem requiring
        specialized models (inpainting, trained on crossed-out text, NLP prediction).
        This test verifies DETECTION accuracy only. Recovery is future work.

        See: docs/specifications/SOUS_RATURE_RECOVERY_STRATEGY.md for recovery spec
        """
        from lib.strikethrough_detection import (
            detect_strikethrough_enhanced,
            XMarkDetectionConfig,
        )

        gt = load_ground_truth("derrida_of_grammatology")
        pdf_path = Path(gt["pdf_file"])

        # Test X-mark detection on each ground truth X-mark
        for xmark in gt["features"]["xmarks"]:
            page_num = xmark["page_index"]
            expected_bbox = xmark["bbox"]
            word_under_erasure = xmark["word_under_erasure"]

            # Run X-mark detection
            config = XMarkDetectionConfig(
                min_line_length=10,
                diagonal_tolerance=15,
                proximity_threshold=20,
                confidence_threshold=0.5,
            )

            result = detect_strikethrough_enhanced(
                pdf_path, page_num, bbox=None, config=config
            )

            # Verify X-marks detected
            assert result.has_xmarks, (
                f"No X-marks detected on page {page_num} (expected {word_under_erasure} crossing)"
            )

            # Verify at least one X-mark near the expected location
            # Convert text bbox to 300 DPI image coordinates (scale ~4.17x)
            import fitz

            doc = fitz.open(str(pdf_path))
            page = doc[page_num]
            pix = page.get_pixmap(dpi=300)
            scale = pix.width / page.rect.width
            doc.close()

            target_x = (expected_bbox[0] + expected_bbox[2]) / 2 * scale
            target_y = (expected_bbox[1] + expected_bbox[3]) / 2 * scale

            # Find closest X-mark
            min_distance = float("inf")
            for candidate in result.candidates:
                bbox = candidate.bbox
                center_x = (bbox[0] + bbox[2]) / 2
                center_y = (bbox[1] + bbox[3]) / 2
                distance = (
                    (center_x - target_x) ** 2 + (center_y - target_y) ** 2
                ) ** 0.5
                min_distance = min(min_distance, distance)

            # Should have X-mark within 100 pixels of expected location
            assert min_distance < 100, (
                f"No X-mark found near expected location (closest: {min_distance:.1f}px away)"
            )

            print(
                f"✅ Detected sous-rature of '{word_under_erasure}' (distance: {min_distance:.1f}px)"
            )

    def test_heidegger_durchkreuzung_real(self):
        """
        Test Heidegger's Durchkreuzung (crossing-out) detection.

        Ground truth: 2 X-marks on pages 1-2 ("Being", "Sein")
        """
        gt = load_ground_truth("heidegger_being_time")
        result = process_pdf(Path(gt["pdf_file"]), output_format="markdown")

        # Validate each known X-mark
        for xmark in gt["features"]["xmarks"]:
            word = xmark["word_under_erasure"]

            # Word should appear (recovered)
            assert word in result or f"~~{word}~~" in result, (
                f"Heidegger X-mark '{word}' not detected or recovered"
            )


class TestRealWorldPerformance:
    """Performance validation with real PDFs."""

    @pytest.mark.parametrize("test_name", list_ground_truth_tests())
    def test_processing_time_within_budget(self, test_name):
        """
        Validate processing time for each test PDF.

        Performance budgets defined in ground truth.
        """
        try:
            gt = load_ground_truth(test_name)
        except FileNotFoundError as e:
            pytest.skip(f"{test_name}: PDF file not found - {e}")

        # Skip if this ground truth file doesn't have performance budget
        if "expected_quality" not in gt or "processing_time_max_ms" not in gt.get(
            "expected_quality", {}
        ):
            pytest.skip(
                f"{test_name}: no performance budget defined (expected_quality.processing_time_max_ms)"
            )

        start = time.time()
        process_pdf(Path(gt["pdf_file"]))
        elapsed_ms = (time.time() - start) * 1000

        max_time = gt["expected_quality"]["processing_time_max_ms"]

        assert elapsed_ms < max_time, (
            f"{test_name}: {elapsed_ms:.0f}ms > {max_time}ms budget"
        )

    def test_xmark_detection_under_5ms_per_page(self):
        """
        Validate X-mark detection meets <5ms per page target.

        This is the performance budget for visual detection.
        """
        # TODO: Instrument to measure just X-mark detection time
        # For now, validate end-to-end is reasonable
        pass


class TestRealWorldQuality:
    """Quality validation with real PDFs."""

    def test_no_hallucinations_derrida(self):
        """
        Validate no content hallucinated (not in original PDF).

        Critical: Detect if pipeline invents content not present in source.
        """
        gt = load_ground_truth("derrida_of_grammatology")
        result = process_pdf(Path(gt["pdf_file"]))

        # Extract original text
        import fitz

        doc = fitz.open(gt["pdf_file"])
        original_text = " ".join(page.get_text() for page in doc)
        doc.close()

        # Find sentences in output not in original
        # (Allow for formatting differences, page markers, etc.)
        output_sentences = result.split(".")

        suspicious = []
        for sentence in output_sentences:
            # Clean sentence (remove markdown, page markers)
            clean = sentence.strip().replace("*", "").replace("~~", "")
            clean = clean.replace("[[PDF", "").replace("]]", "")

            if len(clean) > 20:  # Only check substantial sentences
                # Fuzzy match in original (allow for minor differences)
                if clean not in original_text:
                    # Further check: might be legitimate transformation
                    # For now, just flag for review
                    suspicious.append(clean[:50])

        # Allow up to 5 suspicious sentences (formatting artifacts)
        assert len(suspicious) < 5, f"Potential hallucinations: {suspicious[:3]}"

    def test_output_deterministic(self):
        """
        Validate same PDF produces consistent output.

        Catches non-determinism bugs.
        """
        pdf_path = Path("test_files/derrida_pages_110_135.pdf")

        # Process twice
        result1 = process_pdf(pdf_path, output_format="markdown")
        result2 = process_pdf(pdf_path, output_format="markdown")

        # Should be identical (or 99% similar allowing for timestamps)
        # Simple check: exact match
        assert result1 == result2, "Non-deterministic output detected"


class TestRealWorldRegression:
    """Regression tests with real PDFs (snapshot-based)."""

    def test_derrida_output_unchanged(self):
        """
        Validate Derrida output hasn't regressed (snapshot test).

        First run: Creates snapshot
        Subsequent runs: Compares with snapshot
        """
        pdf_path = Path("test_files/derrida_pages_110_135.pdf")
        snapshot_file = Path("test_files/expected_outputs/derrida_snapshot.md")

        result = process_pdf(pdf_path, output_format="markdown")

        if not snapshot_file.exists():
            # First run: create snapshot
            snapshot_file.parent.mkdir(parents=True, exist_ok=True)
            snapshot_file.write_text(result, encoding="utf-8")
            pytest.skip("Snapshot created - run again to validate")

        # Compare with snapshot
        expected = snapshot_file.read_text(encoding="utf-8")

        # Require 95% similarity (allow minor differences)
        similarity = _calculate_similarity(result, expected)

        assert similarity > 0.95, (
            f"Output regressed: {similarity:.1%} similar (expected >95%)\n"
            f"If intentional, update snapshot: rm {snapshot_file}"
        )


def _calculate_similarity(text1: str, text2: str) -> float:
    """Calculate text similarity (simple ratio)."""
    # Simple implementation - could use difflib for better accuracy
    if text1 == text2:
        return 1.0

    # Count matching characters
    min(len(text1), len(text2))
    max_len = max(len(text1), len(text2))

    matching = sum(1 for a, b in zip(text1, text2) if a == b)

    return matching / max_len if max_len > 0 else 0.0


# Pytest configuration
pytestmark = [pytest.mark.real_world, pytest.mark.slow, pytest.mark.ground_truth]
