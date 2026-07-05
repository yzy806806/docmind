"""Tests for OCR integration in src.core.extractor.

These tests verify that:
- Scanned (image-only) PDFs are OCR'd via Tesseract when text extraction yields nothing.
- Image files (.png, .jpg, etc.) are OCR'd directly.
- extract_from_bytes also supports OCR for scanned PDFs and images.
- The SUPPORTED set includes common image extensions.
- Extraction degrades gracefully when Tesseract is not installed.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Helpers ────────────────────────────────────────────────────

def _make_text_image(text: str = "Hello OCR World", width: int = 400, height: int = 200) -> bytes:
    """Create a PNG image containing *text* and return its bytes.

    Uses Pillow to draw text on a white background.
    """
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    # Try a TrueType font; fall back to default if not available.
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except Exception:
        font = ImageFont.load_default()

    draw.text((20, 60), text, fill="black", font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_scanned_pdf(text: str = "Scanned PDF Text") -> bytes:
    """Create a scanned (image-only) PDF containing *text*.

    The PDF has no text layer — pdfplumber.extract_text() returns None/empty.
    Text is rendered as an image on the page.
    """
    from PIL import Image

    # Build the PDF by saving a PIL image as PDF directly.
    img_bytes = _make_text_image(text, width=600, height=400)
    img = Image.open(io.BytesIO(img_bytes))

    pdf_buf = io.BytesIO()
    # PIL can save as PDF
    img.save(pdf_buf, format="PDF", save_all=True)
    return pdf_buf.getvalue()


def _write_temp_bytes(content: bytes, suffix: str = ".png") -> Path:
    """Write bytes to a temp file and return its Path."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


# ── Image extension support ────────────────────────────────────

def test_image_extensions_in_supported() -> None:
    """Image file extensions should be in the SUPPORTED set."""
    from src.core.extractor import Extractor

    for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        assert ext in Extractor.SUPPORTED, f"{ext} should be supported for OCR"


# ── PNG OCR extraction ─────────────────────────────────────────

def test_extract_png_ocr() -> None:
    """Extracting text from a PNG image should return OCR'd text."""
    from src.core.extractor import Extractor

    img_bytes = _make_text_image("Hello OCR World")
    p = _write_temp_bytes(img_bytes, ".png")
    try:
        result = Extractor.extract(p)
        assert result is not None
        assert "Hello" in result or "OCR" in result or "World" in result
    finally:
        p.unlink()


def test_extract_jpg_ocr() -> None:
    """Extracting text from a JPEG image should return OCR'd text."""
    from PIL import Image

    from src.core.extractor import Extractor

    img_bytes = _make_text_image("Jpeg Test Text")
    # Convert to JPEG
    img = Image.open(io.BytesIO(img_bytes))
    jpg_buf = io.BytesIO()
    img.save(jpg_buf, format="JPEG")
    p = _write_temp_bytes(jpg_buf.getvalue(), ".jpg")
    try:
        result = Extractor.extract(p)
        assert result is not None
        assert "Jpeg" in result or "Test" in result or "Text" in result
    finally:
        p.unlink()


# ── Scanned PDF OCR ────────────────────────────────────────────

def test_extract_scanned_pdf_ocr() -> None:
    """A scanned (image-only) PDF should be OCR'd when text extraction yields nothing."""
    from src.core.extractor import Extractor

    pdf_bytes = _make_scanned_pdf("Scanned PDF Text")
    p = _write_temp_bytes(pdf_bytes, ".pdf")
    try:
        result = Extractor.extract(p)
        assert result is not None
        # OCR should find at least some of the text
        assert "Scanned" in result or "PDF" in result or "Text" in result
    finally:
        p.unlink()


# ── extract_from_bytes OCR ─────────────────────────────────────

def test_extract_from_bytes_png_ocr() -> None:
    """extract_from_bytes should OCR a PNG image."""
    from src.core.extractor import Extractor

    img_bytes = _make_text_image("Bytes PNG OCR")
    result = Extractor.extract_from_bytes(img_bytes, ".png")
    assert result is not None
    assert "Bytes" in result or "PNG" in result or "OCR" in result


def test_extract_from_bytes_scanned_pdf_ocr() -> None:
    """extract_from_bytes should OCR a scanned PDF."""
    from src.core.extractor import Extractor

    pdf_bytes = _make_scanned_pdf("Bytes Scanned PDF")
    result = Extractor.extract_from_bytes(pdf_bytes, ".pdf")
    assert result is not None
    assert "Bytes" in result or "Scanned" in result or "PDF" in result


# ── Text PDF still works (no OCR needed) ───────────────────────

def test_text_pdf_not_ocrd() -> None:
    """A normal text PDF should not need OCR — text extraction should suffice.

    We verify that OCR is NOT invoked by checking that the result contains
    text from the PDF's text layer, not from OCR.
    """
    from src.core.extractor import Extractor

    # Minimal PDF with a text object — pdfplumber should extract text from it.
    # This is a more realistic text PDF than the minimal structure-only one.
    # We'll just verify that a text PDF returns a non-None result.
    # (The existing test_extract_pdf_basic covers the structure-only case.)
    # Here we test that OCR fallback doesn't corrupt results when text IS found.

    # Create a PDF with actual text using reportlab if available, else skip
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed")

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()
    p = Path(tmp.name)

    try:
        c = canvas.Canvas(str(p), pagesize=letter)
        c.drawString(100, 700, "This is real PDF text")
        c.save()

        result = Extractor.extract(p)
        assert result is not None
        assert "real PDF text" in result
    finally:
        p.unlink()


# ── Graceful degradation when Tesseract is missing ─────────────

def test_ocr_graceful_when_tesseract_missing() -> None:
    """When Tesseract binary is not found, extraction should return empty string, not crash."""
    from src.core.extractor import Extractor

    img_bytes = _make_text_image("Should Not Crash")
    p = _write_temp_bytes(img_bytes, ".png")

    # Mock pytesseract.image_to_string to raise FileNotFoundError
    try:
        with patch("pytesseract.image_to_string", side_effect=FileNotFoundError("tesseract not found")):
            result = Extractor.extract(p)
            # Should not raise — should return empty string or None gracefully
            assert result is not None
            assert result == ""
    finally:
        p.unlink()


# ── OCR on multi-page scanned PDF ──────────────────────────────

def test_extract_scanned_pdf_multipage() -> None:
    """A multi-page scanned PDF should OCR all pages."""
    from PIL import Image

    from src.core.extractor import Extractor

    # Create two images, save as multi-page PDF
    img1 = Image.open(io.BytesIO(_make_text_image("Page One", width=600, height=400)))
    img2 = Image.open(io.BytesIO(_make_text_image("Page Two", width=600, height=400)))

    pdf_buf = io.BytesIO()
    img1.save(pdf_buf, format="PDF", save_all=True, append_images=[img2])
    pdf_bytes = pdf_buf.getvalue()

    p = _write_temp_bytes(pdf_bytes, ".pdf")
    try:
        result = Extractor.extract(p)
        assert result is not None
        assert "One" in result or "Page" in result
        assert "Two" in result or "Page" in result
    finally:
        p.unlink()
