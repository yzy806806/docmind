"""Minimal test to isolate OCR pipeline issues."""
import tempfile
from pathlib import Path
import io
from PIL import Image, ImageDraw, ImageFont
import pytest

@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        index_db = Path(tmpdir) / "test.db"
        search_db = Path(tmpdir) / "test_fts.db"
        yield index_db, search_db

@pytest.fixture
def service(tmp_db):
    from src.cli.services import DocMindService
    index_db, search_db = tmp_db
    svc = DocMindService(
        index_db_path=str(index_db),
        search_db_path=str(search_db),
    )
    yield svc
    svc.close()

def _make_scanned_pdf(text="Test"):
    img = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 60), text, fill="black")
    pdf_buf = io.BytesIO()
    img.save(pdf_buf, format="PDF", save_all=True)
    return pdf_buf.getvalue()

def test_minimal_ocr_pipeline(service):
    """Minimal: create scanned PDF, ingest, verify body non-empty."""
    import tempfile as tmpmod
    with tmpmod.TemporaryDirectory() as td:
        p = Path(td) / "scan.pdf"
        p.write_bytes(_make_scanned_pdf("Minimal OCR Test"))
        result = service.ingest_path(str(p.parent), source_name="minimal-test")
        assert result["count"] >= 1
        docs = service.list_documents(source="minimal-test", limit=10)
        assert len(docs) >= 1
        doc = service.get_document(docs[0]["id"])
        assert doc["body"] and len(doc["body"]) > 0
