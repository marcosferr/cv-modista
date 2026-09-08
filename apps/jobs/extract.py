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

# Ligaduras tipográficas. Un CV de Canva trae "planiﬁcación" con U+FB01, que no es
# "fi": rompe la búsqueda de keywords de un ATS y no compila igual en LaTeX.
LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st"}

# Un hueco horizontal mayor a esto separa columnas.
COLUMN_GAP = 60
# Tolerancia vertical para considerar que dos fragmentos están en el mismo renglón.
LINE_TOLERANCE = 3.0
# Si el reordenamiento por columnas rescata menos de esta fracción del texto plano,
# algo salió mal con las heurísticas y se usa el texto plano.
COLUMN_MIN_RATIO = 0.6


class ExtractionError(ValueError):
    pass


def clean_text(text: str) -> str:
    text = (text or "").replace("\x00", "")
    for ligature, plain in LIGATURES.items():
        text = text.replace(ligature, plain)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def cv_hash(text: str) -> str:
    """Hash sobre el texto normalizado: el mismo CV con otro espaciado es el mismo CV."""
    normalized = " ".join((text or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _fragments(page) -> list[dict]:
    """Fragmentos de texto con su posición absoluta en la página.

    `tm` viene en el espacio del Form XObject que envuelve al texto, no de la página:
    los generadores de diseño como Canva anidan cada bloque en el suyo. Hay que
    componerlo con la CTM para obtener coordenadas comparables entre bloques.
    """
    found: list[dict] = []

    def visitor(text, cm, tm, font_dict, font_size):  # noqa: ANN001
        if not (text and text.strip()):
            return
        found.append({
            "x": tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
            "y": tm[4] * cm[1] + tm[5] * cm[3] + cm[5],
            "size": abs(font_size * (cm[0] or 1)) or 1.0,
            "text": text.strip(),
        })

    page.extract_text(visitor_text=visitor)

    # pypdf a veces emite además un fragmento con TODO el texto de la página. Si se
    # deja, duplica el contenido entero y arruina el orden.
    textos = [f["text"] for f in found]
    return [
        f for f in found
        if not (f["text"].count("\n") > 3
                and sum(1 for t in textos if t != f["text"] and t in f["text"]) > 3)
    ]


def _columns(fragments: list[dict]) -> list[list[dict]]:
    """Agrupa por borde izquierdo: los huecos grandes en X son bordes de columna."""
    lefts = sorted({round(f["x"]) for f in fragments})
    cortes = [b for a, b in zip(lefts, lefts[1:], strict=False) if b - a > COLUMN_GAP]
    grupos: dict[int, list[dict]] = {}
    for fragment in fragments:
        indice = sum(1 for corte in cortes if fragment["x"] >= corte)
        grupos.setdefault(indice, []).append(fragment)
    return [grupos[k] for k in sorted(grupos)]


def _join_line(fragments: list[dict]) -> str:
    """Une los fragmentos de un renglón, sin espacio si vienen pegados.

    Sin esto un email sale partido: Canva emite la primera letra como su propio
    fragmento y queda "h ola@ejemplo.com".
    """
    fragments = sorted(fragments, key=lambda f: f["x"])
    linea = fragments[0]["text"]
    fin = fragments[0]["x"] + len(fragments[0]["text"]) * fragments[0]["size"] * 0.5
    for fragment in fragments[1:]:
        separador = "" if fragment["x"] - fin < fragment["size"] * 0.35 else " "
        linea += separador + fragment["text"]
        fin = fragment["x"] + len(fragment["text"]) * fragment["size"] * 0.5
    return linea


def _read_in_columns(page) -> str:
    """Reconstruye el orden de lectura: cada columna de arriba abajo, de izquierda a derecha.

    Un CV de diseño en varias columnas se extrae en el orden del content stream, que no
    es el orden en que se lee. Visto con un CV real de Canva: los bullets de un empleo
    terminaban asignados al otro, porque en el texto plano los dos cargos salían juntos
    y las dos empresas juntas, lejos de sus bullets.
    """
    fragments = _fragments(page)
    if not fragments:
        return ""

    bloques: list[str] = []
    for columna in _columns(fragments):
        renglones: list[list[dict]] = []
        for fragment in sorted(columna, key=lambda f: (f["y"], f["x"])):
            if renglones and abs(fragment["y"] - renglones[-1][0]["y"]) <= LINE_TOLERANCE:
                renglones[-1].append(fragment)
            else:
                renglones.append([fragment])
        bloques.append("\n".join(_join_line(r) for r in renglones))
    return "\n\n".join(b for b in bloques if b.strip())


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    plano = clean_text("\n".join(page.extract_text() or "" for page in reader.pages))

    try:
        ordenado = clean_text("\n\n".join(_read_in_columns(p) for p in reader.pages))
    except Exception as exc:  # noqa: BLE001 - ante la duda, el texto plano
        log.warning("El reordenamiento por columnas falló (%s); se usa el texto plano", exc)
        return plano

    # Red de seguridad: si las heurísticas perdieron contenido, gana el texto plano.
    if len(ordenado) < len(plano) * COLUMN_MIN_RATIO:
        log.info("El reordenamiento rescató %d de %d chars; se usa el texto plano",
                 len(ordenado), len(plano))
        return plano
    return ordenado


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
