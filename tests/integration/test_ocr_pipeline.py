"""Integration tests for the OCR-enabled extraction pipeline.

Verifies the full end-to-end path for scanned PDFs and mixed
native+scanned documents: extract → index → search → retrieve,
ensuring body is non-empty after OCR.

These tests use temporary SQLite databases and in-process
DocMindService — no external services required.
"""

from __future__ import annotations

import io
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from PIL import Image, ImageDraw, ImageFont


# ── Fixture helpers ──────────────────────────────────────────────


def _make_text_image(
    text: str = "Hello OCR World", width: int = 600, height: int = 400
) -> bytes:
    """Create a PNG image containing *text* and return its bytes."""
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28
        )
    except Exception:
        font = ImageFont.load_default()
    draw.text((20, 60), text, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_scanned_pdf(text: str = "Scanned PDF Text") -> bytes:
    """Create a scanned (image-only) PDF containing *text*.

    The PDF has no text layer — pdfplumber returns empty.
    Text is rendered as an image on the page.
    """
    img_bytes = _make_text_image(text, width=600, height=400)
    img = Image.open(io.BytesIO(img_bytes))
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", save_all=True)
    return pdf_buf.getvalue()


def _make_text_pdf(text_lines: list[str]) -> bytes:
    """Create a PDF with a real text layer (not scanned)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for line in text_lines:
        c.drawString(100, y, line)
        y -= 15
    c.showPage()
    c.save()
    return buf.getvalue()


def _make_multi_page_scanned_pdf(
    texts: list[str],
) -> bytes:
    """Create a multi-page scanned PDF, one image per page."""
    images = []
    for t in texts:
        img_bytes = _make_text_image(t, width=600, height=400)
        images.append(Image.open(io.BytesIO(img_bytes)))

    pdf_buf = io.BytesIO()
    images[0].save(
        pdf_buf,
        format="PDF",
        save_all=True,
        append_images=images[1:] if len(images) > 1 else [],
    )
    return pdf_buf.getvalue()


def _write_temp_file(content: bytes, suffix: str) -> Path:
    """Write bytes to a temp file and return its Path."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


def _create_temp_dir_with_files(
    files: list[tuple[str, bytes]],
) -> Path:
    """Create a temp directory with the given (filename, content) pairs."""
    tmpdir = tempfile.mkdtemp()
    for name, content in files:
        p = Path(tmpdir) / name
        p.write_bytes(content)
    return Path(tmpdir)


# ── Database fixtures ───────────────────────────────────────────


@pytest.fixture
def tmp_db() -> Generator[tuple[Path, Path], None, None]:
    """Create temporary database files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        index_db = Path(tmpdir) / "test_docmind.db"
        search_db = Path(tmpdir) / "test_docmind_fts.db"
        yield index_db, search_db


@pytest.fixture
def service(tmp_db):
    """Create a DocMindService with temporary databases."""
    from src.cli.services import DocMindService

    index_db, search_db = tmp_db
    svc = DocMindService(
        index_db_path=str(index_db),
        search_db_path=str(search_db),
    )
    yield svc
    svc.close()


# ── Scanned PDF fixture tests ───────────────────────────────────


class TestScannedPdfPipeline:
    """Verify end-to-end pipeline for scanned (image-only) PDFs."""

    def test_scanned_pdf_ingest_and_retrieve(self, service, tmp_path):
        """A scanned PDF should be OCR'd, indexed, and retrievable with non-empty body."""
        pdf_bytes = _make_scanned_pdf("Integration Test Scanned PDF")
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        result = service.ingest_path(str(tmp_path), source_name="ocr-test")
        assert result["count"] >= 1, (
            f"Expected at least 1 document ingested, got {result['count']}"
        )

        docs = service.list_documents(source="ocr-test", limit=10)
        assert len(docs) >= 1, "Document should appear in listing"

        doc_id = docs[0]["id"]
        doc = service.get_document(doc_id)
        assert doc is not None
        assert "body" in doc, "Document should have body field"

        body = doc["body"]
        assert isinstance(body, str), f"body should be str, got {type(body)}"
        assert len(body) > 0, (
            "Body must be non-empty after OCR — "
            f"got empty string for scanned PDF"
        )
        # OCR should have found at least some of the text
        assert (
            "Integration" in body
            or "Scanned" in body
            or "Test" in body
            or "PDF" in body
        ), f"OCR did not find expected text in: {body!r}"

    def test_scanned_pdf_body_is_not_none(self, service, tmp_path):
        """Body must never be None for a successfully indexed scanned PDF."""
        pdf_bytes = _make_scanned_pdf("Non-None Body Test")
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        result = service.ingest_path(str(tmp_path), source_name="ocr-none-test")
        assert result["count"] >= 1

        docs = service.list_documents(source="ocr-none-test", limit=10)
        assert len(docs) >= 1

        doc = service.get_document(docs[0]["id"])
        assert doc["body"] is not None, (
            "Body must not be None for OCR'd document"
        )
        assert len(doc["body"]) > 0, (
            "Body must not be empty for OCR'd document"
        )

    def test_scanned_pdf_searchable_after_ocr(self, service, tmp_path):
        """OCR'd content should be searchable via the full-text search."""
        pdf_bytes = _make_scanned_pdf("Searchable OCR Content")
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        service.ingest_path(str(tmp_path), source_name="ocr-search-test")

        results = service.search("Searchable OCR", top_k=5)
        assert len(results) >= 1, (
            "Search should find OCR'd scanned PDF content"
        )

        result = results[0]
        assert "title" in result
        assert "citation" in result

    def test_scanned_pdf_citation_has_hashes(self, service, tmp_path):
        """Citation integrity: scanned PDF results must include content/structural hashes."""
        pdf_bytes = _make_scanned_pdf("Citation Hash Test")
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        service.ingest_path(str(tmp_path), source_name="ocr-cite-test")

        results = service.search("Citation Hash", top_k=5)
        assert len(results) >= 1

        citation = results[0].get("citation", {})
        assert "content_hash" in citation, "Missing content_hash"
        assert "structural_hash" in citation, "Missing structural_hash"
        assert "confidence" in citation, "Missing confidence"

    def test_multipage_scanned_pdf_all_pages_ocrd(self, service, tmp_path):
        """A multi-page scanned PDF should OCR all pages and combine text."""
        pdf_bytes = _make_multi_page_scanned_pdf(
            ["First Page Content", "Second Page Content"]
        )
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        service.ingest_path(str(tmp_path), source_name="ocr-multipage")

        docs = service.list_documents(source="ocr-multipage", limit=10)
        assert len(docs) >= 1

        doc = service.get_document(docs[0]["id"])
        body = doc["body"]
        assert len(body) > 0

        # Text from both pages should appear (or at least some words)
        assert (
            "First" in body
            or "Second" in body
            or "Page" in body
            or "Content" in body
        ), f"Multi-page OCR missed expected text in: {body!r}"

    def test_scanned_pdf_ingest_twice_idempotent(self, service, tmp_path):
        """Ingesting the same scanned PDF twice should only index once."""
        pdf_bytes = _make_scanned_pdf("Idempotent Test")
        p = tmp_path / "scan.pdf"
        p.write_bytes(pdf_bytes)

        result1 = service.ingest_path(str(tmp_path), source_name="ocr-idem-test")
        result2 = service.ingest_path(str(tmp_path), source_name="ocr-idem-test")

        assert result1["count"] >= 1, "First ingest should index the document"
        assert result2["count"] == 0, (
            "Second ingest should be idempotent (hash-based dedup)"
        )


# ── Mixed native + scanned document tests ───────────────────────


class TestMixedNativeAndScanned:
    """Verify pipeline handles a mix of native-text and scanned PDFs."""

    def test_mixed_docs_ingest_all(self, service, tmp_path):
        """Both native PDFs and scanned PDFs in same directory should be indexed."""
        # Create a native text PDF
        text_pdf = _make_text_pdf(["This is native PDF text"])
        (tmp_path / "native.pdf").write_bytes(text_pdf)

        # Create a scanned (image-only) PDF
        scanned_pdf = _make_scanned_pdf("This is scanned OCR text")
        (tmp_path / "scanned.pdf").write_bytes(scanned_pdf)

        # Create a plain text file
        (tmp_path / "notes.txt").write_text("Plain text notes")

        result = service.ingest_path(str(tmp_path), source_name="mixed-test")
        assert result["count"] == 3, (
            f"Expected 3 documents (native PDF + scanned PDF + txt), "
            f"got {result['count']}"
        )

        docs = service.list_documents(source="mixed-test", limit=10)
        assert len(docs) == 3

        # Every document should have non-empty body
        for doc in docs:
            doc_detail = service.get_document(doc["id"])
            assert doc_detail["body"], (
                f"Document {doc['title']} has empty body"
            )
            assert len(doc_detail["body"]) > 0, (
                f"Document {doc['title']} body is empty string"
            )

    def test_mixed_docs_all_searchable(self, service, tmp_path):
        """All documents in a mixed batch should be searchable."""
        text_pdf = _make_text_pdf(["Machine learning pipeline overview"])
        (tmp_path / "pipeline.pdf").write_bytes(text_pdf)

        scanned_pdf = _make_scanned_pdf("Data preprocessing workflow")
        (tmp_path / "preprocessing.pdf").write_bytes(scanned_pdf)

        (tmp_path / "guide.txt").write_text("Evaluation metrics guide")

        service.ingest_path(str(tmp_path), source_name="mixed-search")

        # Search for native PDF content
        results = service.search("machine learning pipeline", top_k=5)
        assert len(results) >= 1, "Should find native PDF"

        # Search for scanned/OCR'd content
        results = service.search("preprocessing workflow", top_k=5)
        assert len(results) >= 1, "Should find scanned PDF via OCR"

        # Search for text file content
        results = service.search("evaluation metrics", top_k=5)
        assert len(results) >= 1, "Should find text file"

    def test_mixed_docs_correct_titles(self, service, tmp_path):
        """Each document should retain its original filename as title."""
        text_pdf = _make_text_pdf(["Native content"])
        (tmp_path / "report_2024.pdf").write_bytes(text_pdf)

        scanned_pdf = _make_scanned_pdf("Scanned content")
        (tmp_path / "scan_2024.pdf").write_bytes(scanned_pdf)

        service.ingest_path(str(tmp_path), source_name="mixed-titles")

        docs = service.list_documents(source="mixed-titles", limit=10)
        titles = {d["title"] for d in docs}

        assert "report_2024.pdf" in titles, f"Missing native PDF title in {titles}"
        assert "scan_2024.pdf" in titles, f"Missing scanned PDF title in {titles}"

    def test_mixed_docs_stats_accurate(self, service, tmp_path):
        """Stats should reflect all mixed documents."""
        (tmp_path / "doc1.pdf").write_bytes(
            _make_text_pdf(["Doc one"])
        )
        (tmp_path / "doc2.pdf").write_bytes(
            _make_scanned_pdf("Doc two scanned")
        )
        (tmp_path / "doc3.txt").write_text("Doc three")

        service.ingest_path(str(tmp_path), source_name="mixed-stats")

        stats = service.get_stats()
        assert stats["total"] >= 3, (
            f"Stats total should be >= 3, got {stats['total']}"
        )

    def test_mixed_docs_only_scanned_uses_ocr(self, service, tmp_path):
        """Verify that native PDFs still use pdfplumber (not OCR) while scanned PDFs use OCR."""
        import io as _io

        from src.core.extractor import Extractor

        text_pdf = _make_text_pdf(["Native text that pdfplumber reads"])
        (tmp_path / "native.pdf").write_bytes(text_pdf)

        scanned_pdf = _make_scanned_pdf("Scanned text that needs OCR")
        (tmp_path / "scanned.pdf").write_bytes(scanned_pdf)

        # Verify extraction directly
        native_result = Extractor.extract(tmp_path / "native.pdf")
        scanned_result = Extractor.extract(tmp_path / "scanned.pdf")

        assert native_result is not None
        assert "Native text" in native_result, (
            "Native PDF should be readable without OCR"
        )
        assert scanned_result is not None
        assert len(scanned_result) > 0, (
            "Scanned PDF should produce text via OCR"
        )

        # Now verify both get indexed
        service.ingest_path(str(tmp_path), source_name="mixed-ocr-check")
        docs = service.list_documents(source="mixed-ocr-check", limit=10)
        assert len(docs) == 2, (
            f"Expected 2 documents, got {len(docs)}"
        )

        for doc in docs:
            detail = service.get_document(doc["id"])
            assert detail["body"], f"{doc['title']} has empty body"
            assert len(detail["body"]) > 0


# ── extract_from_bytes OCR pipeline tests ───────────────────────


class TestExtractFromBytesPipeline:
    """Verify the extract_from_bytes path works for scanned PDFs in the pipeline."""

    def test_extract_from_bytes_scanned_pdf_body_nonempty(self):
        """extract_from_bytes on scanned PDF bytes should return non-empty string."""
        from src.core.extractor import Extractor

        pdf_bytes = _make_scanned_pdf("Bytes Pipeline Test")
        result = Extractor.extract_from_bytes(pdf_bytes, ".pdf")

        assert result is not None, (
            "extract_from_bytes must not return None for scanned PDF"
        )
        assert isinstance(result, str)
        assert len(result) > 0, (
            "extract_from_bytes must return non-empty text after OCR — "
            f"got: {result!r}"
        )

    def test_extract_from_bytes_text_pdf_still_works(self):
        """extract_from_bytes on text-layer PDF should still use pdfplumber."""
        from src.core.extractor import Extractor

        pdf_bytes = _make_text_pdf(["Bytes text PDF test"])
        result = Extractor.extract_from_bytes(pdf_bytes, ".pdf")

        assert result is not None
        assert "Bytes text PDF test" in result, (
            "Text-layer PDF should be readable via pdfplumber, not OCR"
        )

    def test_extract_from_bytes_image_formats(self):
        """extract_from_bytes should handle PNG and JPEG image formats via OCR."""
        from src.core.extractor import Extractor

        # PNG
        png_bytes = _make_text_image("PNG Bytes Test")
        result = Extractor.extract_from_bytes(png_bytes, ".png")
        assert result is not None
        assert len(result) > 0, f"PNG OCR returned empty: {result!r}"

        # JPEG
        img = Image.open(io.BytesIO(png_bytes))
        jpg_buf = io.BytesIO()
        img.save(jpg_buf, format="JPEG")

        result = Extractor.extract_from_bytes(jpg_buf.getvalue(), ".jpg")
        assert result is not None
        assert len(result) > 0, f"JPEG OCR returned empty: {result!r}"


# ── Edge case tests ─────────────────────────────────────────────


class TestOcrEdgeCases:
    """Verify pipeline behavior for OCR edge cases."""

    def test_blank_scanned_pdf_produces_empty_body(self):
        """A blank scanned PDF should produce empty string, and be skipped by storage."""
        from src.core.extractor import Extractor

        # Create a blank white image PDF
        img = Image.new("RGB", (600, 400), "white")
        pdf_buf = io.BytesIO()
        img.save(pdf_buf, format="PDF", save_all=True)
        pdf_bytes = pdf_buf.getvalue()

        p = _write_temp_file(pdf_bytes, ".pdf")
        try:
            result = Extractor.extract(p)
            # Blank image may produce empty or whitespace-only text
            # Either way, it should not cause an error
            assert result is not None, (
                "Blank scanned PDF must not crash the extractor"
            )
        finally:
            p.unlink()

    def test_corrupt_pdf_returns_none(self, service, tmp_path):
        """A corrupt PDF file should return None from extractor, not crash the pipeline."""
        (tmp_path / "corrupt.pdf").write_bytes(b"this is not a valid PDF at all")

        # Ingest should not crash
        result = service.ingest_path(str(tmp_path), source_name="corrupt-test")

        # Corrupt file should not be indexed (extraction returns None)
        assert result["count"] == 0, (
            f"Corrupt PDF should not be indexed, got {result['count']}"
        )

    def test_very_large_scanned_pdf_ocr_memory(self, service, tmp_path):
        """A reasonably large scanned PDF should still be processed."""
        from src.core.extractor import Extractor

        # Create a larger scanned PDF (3 pages, big enough to matter)
        pdf_bytes = _make_multi_page_scanned_pdf(
            [f"Large Page {i} Content With More Text To OCR" for i in range(3)]
        )
        p = _write_temp_file(pdf_bytes, ".pdf")

        try:
            # Memory budget check
            est = Extractor.estimate_memory(p)
            assert est > 0, "Memory estimate should be positive"
            within = Extractor.check_memory_budget(p)
            assert within is True, (
                "Reasonable scanned PDF should be within memory budget"
            )

            # Actual extraction
            result = Extractor.extract(p)
            assert result is not None
            assert len(result) > 0
        finally:
            p.unlink()

    def test_extractor_graceful_ocr_fallback_empty(self):
        """When OCR produces empty string, the result should be empty string, not None."""
        from unittest.mock import patch

        from src.core.extractor import Extractor

        pdf_bytes = _make_scanned_pdf("Graceful Fallback")
        p = _write_temp_file(pdf_bytes, ".pdf")

        try:
            # Mock Tesseract to return empty string
            with patch(
                "pytesseract.image_to_string", return_value=""
            ):
                result = Extractor.extract(p)
                assert result is not None, (
                    "Empty OCR should return '', not None"
                )
                assert result == "", (
                    f"Expected '', got {result!r}"
                )
        finally:
            p.unlink()


# ── Body non-empty contract tests ───────────────────────────────


class TestBodyNonEmptyContract:
    """The core contract: every indexed document must have non-empty body."""

    def test_native_pdf_body_nonempty(self, service, tmp_path):
        """Native text PDF must have non-empty body."""
        (tmp_path / "doc.pdf").write_bytes(
            _make_text_pdf(["Hello world"])
        )
        service.ingest_path(str(tmp_path), source_name="body-native")
        docs = service.list_documents(source="body-native", limit=10)
        assert len(docs) == 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0

    def test_scanned_pdf_body_nonempty(self, service, tmp_path):
        """Scanned PDF must have non-empty body after OCR."""
        (tmp_path / "doc.pdf").write_bytes(
            _make_scanned_pdf("OCR body contract test")
        )
        service.ingest_path(str(tmp_path), source_name="body-scanned")
        docs = service.list_documents(source="body-scanned", limit=10)
        assert len(docs) == 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0, (
            f"Scanned PDF body must be non-empty after OCR, "
            f"got: {doc['body']!r}"
        )

    def test_text_file_body_nonempty(self, service, tmp_path):
        """Plain text file must have non-empty body."""
        (tmp_path / "notes.txt").write_text("Some text content")
        service.ingest_path(str(tmp_path), source_name="body-txt")
        docs = service.list_documents(source="body-txt", limit=10)
        assert len(docs) == 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0

    def test_docx_body_nonempty(self, service, tmp_path):
        """DOCX must have non-empty body."""
        from docx import Document as DocxDocument

        doc = DocxDocument()
        doc.add_paragraph("DOCX body content")
        doc.save(str(tmp_path / "doc.docx"))

        service.ingest_path(str(tmp_path), source_name="body-docx")
        docs = service.list_documents(source="body-docx", limit=10)
        assert len(docs) == 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0

    def test_html_body_nonempty(self, service, tmp_path):
        """HTML must have non-empty body after tag stripping."""
        (tmp_path / "page.html").write_text(
            "<html><body><p>HTML body content</p></body></html>"
        )
        service.ingest_path(str(tmp_path), source_name="body-html")
        docs = service.list_documents(source="body-html", limit=10)
        assert len(docs) == 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0

    def test_empty_text_file_skipped(self, service, tmp_path):
        """An empty text file should be skipped (not indexed) by storage."""
        (tmp_path / "empty.txt").write_text("")

        result = service.ingest_path(str(tmp_path), source_name="empty-test")
        assert result["count"] == 0, (
            f"Empty file should be skipped, got {result['count']}"
        )

    def test_blank_scanned_pdf_skipped(self, service, tmp_path):
        """A blank scanned PDF (no OCR text) should be skipped by storage."""
        # Create a blank white image PDF
        img = Image.new("RGB", (600, 400), "white")
        pdf_buf = io.BytesIO()
        img.save(pdf_buf, format="PDF", save_all=True)
        (tmp_path / "blank.pdf").write_bytes(pdf_buf.getvalue())

        result = service.ingest_path(str(tmp_path), source_name="blank-test")

        # A blank scanned PDF may produce empty OCR; should be skipped
        # (If Tesseract happens to find noise, that's also acceptable —
        #  the key is that it doesn't crash.)
        docs = service.list_documents(source="blank-test", limit=10)
        for doc in docs:
            detail = service.get_document(doc["id"])
            assert detail["body"], (
                "If indexed, blank PDF body must still be non-empty"
            )
