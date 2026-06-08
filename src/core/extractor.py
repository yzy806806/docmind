"""Text extraction from various file formats."""
import json
import csv
from pathlib import Path
from typing import Optional


class Extractor:
    """Extract plain text from supported file types."""

    SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".html", ".htm", ".csv", ".json", ".xml"}

    @staticmethod
    def extract(file_path: Path) -> Optional[str]:
        """Extract text from a file based on its extension."""
        ext = file_path.suffix.lower()

        if ext not in Extractor.SUPPORTED:
            return None

        try:
            if ext == ".pdf":
                return Extractor._extract_pdf(file_path)
            elif ext == ".docx":
                return Extractor._extract_docx(file_path)
            elif ext in (".html", ".htm"):
                return Extractor._extract_html(file_path)
            elif ext == ".csv":
                return Extractor._extract_csv(file_path)
            elif ext == ".json":
                return Extractor._extract_json(file_path)
            elif ext == ".xml":
                return Extractor._extract_xml(file_path)
            else:
                return file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            # Log the error but don't crash
            print(f"[Extractor] Failed to extract {file_path}: {e}")
            return None

    @staticmethod
    def _extract_pdf(file_path: Path) -> str:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts)

    @staticmethod
    def _extract_docx(file_path: Path) -> str:
        from docx import Document
        doc = Document(str(file_path))
        return "\n".join(p.text for p in doc.paragraphs)

    @staticmethod
    def _extract_html(file_path: Path) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(file_path.read_text(encoding="utf-8", errors="replace"), "html.parser")
        # Remove script and style tags
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)

    @staticmethod
    def _extract_csv(file_path: Path) -> str:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            rows = [" | ".join(row) for row in reader]
            return "\n".join(rows)

    @staticmethod
    def _extract_json(file_path: Path) -> str:
        # Pretty-print JSON for readability
        data = json.loads(file_path.read_text(encoding="utf-8", errors="replace"))
        return json.dumps(data, ensure_ascii=False, indent=2)

    @staticmethod
    def _extract_xml(file_path: Path) -> str:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(file_path.read_text(encoding="utf-8", errors="replace"), "xml")
        return soup.get_text(separator="\n", strip=True)