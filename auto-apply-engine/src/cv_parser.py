"""Extract plain text from a PDF, DOCX, or text résumé."""

from __future__ import annotations

from pathlib import Path

from src.errors import CVError


def extract_text(path: Path) -> str:
    """Read a résumé file and return its text. Scanned image PDFs are rejected."""
    if not path.exists():
        raise CVError(f"CV file not found: {path}")
    if not path.is_file():
        raise CVError(f"CV path is not a file: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        text = _extract_pdf(path)
    elif suffix == ".docx":
        text = _extract_docx(path)
    elif suffix == ".txt":
        text = path.read_text(encoding="utf-8")
    else:
        raise CVError(f"Unsupported CV type '{suffix}'. Use a .pdf, .docx, or .txt file.")

    cleaned = text.replace("\x00", "").strip()
    if len(cleaned) < 40:
        raise CVError(
            f"Almost no text was extracted from {path.name}. "
            "Export a text-based PDF or DOCX. Scanned image PDFs are not supported."
        )
    return cleaned


def _extract_pdf(path: Path) -> str:
    plumber_text = _pdfplumber(path)
    pypdf_text = _pypdf(path)
    if len(pypdf_text.strip()) > len(plumber_text.strip()):
        return pypdf_text
    return plumber_text or pypdf_text


def _pdfplumber(path: Path) -> str:
    try:
        import pdfplumber
    except ImportError as exc:
        raise CVError("pdfplumber is not installed. Run pip install -r requirements.txt.") from exc
    try:
        pages: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
        return "\n\n".join(pages)
    except Exception:
        return ""


def _pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise CVError("pypdf is not installed. Run pip install -r requirements.txt.") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:
                raise CVError(f"{path.name} is encrypted. Export an unlocked PDF.") from exc
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except CVError:
        raise
    except PdfReadError as exc:
        raise CVError(f"Could not read {path.name}: {exc}") from exc
    except Exception as exc:
        raise CVError(f"Could not read {path.name}: {exc}") from exc


def _extract_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError as exc:
        raise CVError("python-docx is not installed. Run pip install -r requirements.txt.") from exc
    try:
        document = Document(str(path))
    except Exception as exc:
        raise CVError(f"Could not read {path.name}: {exc}") from exc

    parts: list[str] = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)
