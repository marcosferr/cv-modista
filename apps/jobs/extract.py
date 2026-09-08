"""Extracción de texto del CV subido.

Escalera pensada para ser liviana: casi todos los CVs son PDFs digitales, y para eso
alcanza pypdf, que es Python puro y pesa ~1 MB. El OCR es el último recurso y solo se
carga si de verdad hace falta (PDF escaneado o foto), porque importar el runtime ONNX
tarda unos segundos.

Se descartó tesseract (binario de sistema de ~120 MB) y easyocr/docling (arrastran
torch, varios GB). rapidocr-onnxruntime son ~40 MB por pip y sin binarios externos.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

log = logging.getLogger(__name__)

# Por debajo de esto asumimos que el PDF no tiene capa de texto.
MIN_TEXT_CHARS = 500
OCR_MAX_PAGES = 4
OCR_SCALE = 2.0


class ExtractionError(ValueError):
    pass


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def cv_hash(text: str) -> str:
    """Hash sobre el texto normalizado: el mismo CV con otro espaciado es el mismo CV."""
    normalized = " ".join((text or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return clean_text("\n".join(page.extract_text() or "" for page in reader.pages))


def _from_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    blocks = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            blocks.append(" | ".join(cell.text for cell in row.cells))
    return clean_text("\n".join(blocks))


def _ocr_pdf(data: bytes) -> str:
    """OCR de respaldo para PDFs escaneados. Import perezoso: cuesta segundos cargarlo."""
    try:
        import numpy as np
        import pypdfium2 as pdfium
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as exc:
        raise ExtractionError(
            "El PDF no tiene capa de texto y el OCR no está instalado. "
            'Instalalo con `uv sync --extra ocr` o pegá el CV como texto.'
        ) from exc

    engine = RapidOCR()
    document = pdfium.PdfDocument(io.BytesIO(data))
    lines: list[str] = []
    for index in range(min(len(document), OCR_MAX_PAGES)):
        image = document[index].render(scale=OCR_SCALE).to_pil().convert("RGB")
        result, _ = engine(np.array(image))
        if result:
            lines.extend(item[1] for item in result)
    return clean_text("\n".join(lines))


def extract_from_upload(filename: str, data: bytes) -> tuple[str, str]:
    """Devuelve (texto, método). Levanta ExtractionError si no hay nada legible."""
    name = (filename or "").lower()

    if name.endswith(".docx"):
        text = _from_docx(data)
        if len(text) < 50:
            raise ExtractionError("El .docx no tiene texto legible.")
        return text, "docx"

    if name.endswith(".doc"):
        raise ExtractionError(
            "El formato .doc antiguo no está soportado. Guardalo como .docx o como PDF."
        )

    if name.endswith(".txt") or name.endswith(".md"):
        return clean_text(data.decode("utf-8", errors="replace")), "texto"

    if name.endswith(".pdf"):
        text = _from_pdf(data)
        if len(text) >= MIN_TEXT_CHARS:
            return text, "pdf"
        log.info("El PDF trajo solo %d chars; probando OCR", len(text))
        ocr_text = _ocr_pdf(data)
        if len(ocr_text) < 100:
            raise ExtractionError(
                "No se pudo leer el PDF ni siquiera con OCR. Pegá el CV como texto."
            )
        return ocr_text, "pdf+ocr"

    raise ExtractionError(f"Formato no soportado: {filename}. Usá PDF, DOCX o TXT.")


def resolve_cv_text(pasted: str, filename: str | None, data: bytes | None) -> tuple[str, str]:
    """El texto pegado gana: si el usuario se tomó el trabajo, es el más confiable."""
    pasted = clean_text(pasted or "")
    if len(pasted) >= 200:
        return pasted, "pegado"
    if data:
        return extract_from_upload(filename or "", data)
    if pasted:
        raise ExtractionError(
            f"El CV pegado tiene solo {len(pasted)} caracteres. Pegá el CV completo o subí un archivo."
        )
    raise ExtractionError("Hace falta el CV: pegalo como texto o subí un PDF o DOCX.")
