"""Compilación de LaTeX a PDF y verificaciones post-compilación.

Dos cosas que salen gratis y valen mucho:

- **Ajuste a una página** determinista. Si el PDF sale de más de una página se recortan
  bullets del rol más viejo y se recompila. Cero llamadas al LLM.
- **Aserción ATS**. Se extrae el texto del PDF y se verifica que el nombre, cada empresa
  y cada rango de fechas estén ahí. Eso es literalmente lo que hace un ATS, y detecta
  el fallo clásico de las plantillas bonitas: texto que se ve pero no se puede leer.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from django.conf import settings
from pypdf import PdfReader

log = logging.getLogger(__name__)

MIN_BULLETS_PER_ENTRY = 2
MAX_FIT_ITERATIONS = 4


class LatexError(RuntimeError):
    """La compilación falló. El mensaje trae la cola del log de LaTeX."""


class LatexCompiler(Protocol):
    def compile(self, tex_source: str, workdir: Path, stem: str) -> Path: ...


class TectonicCompiler:
    """Tectonic: binario único, motor XeTeX, baja los paquetes que falten y los cachea.

    Se corre nativo, no en contenedor: su caché vive en ~/Library/Caches/Tectonic y
    dentro de un contenedor efímero se re-descargaría todo en cada job.
    """

    def __init__(self, binary: str | None = None, timeout: int | None = None):
        self.binary = binary or settings.TECTONIC_BIN
        self.timeout = timeout or settings.LATEX_TIMEOUT

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def compile(self, tex_source: str, workdir: Path, stem: str = "cv") -> Path:
        workdir.mkdir(parents=True, exist_ok=True)
        tex_path = workdir / f"{stem}.tex"
        tex_path.write_text(tex_source, encoding="utf-8")

        if not self.available():
            raise LatexError(
                f"No se encontró el binario '{self.binary}'. Instalalo con: brew install tectonic"
            )

        try:
            result = subprocess.run(
                [
                    self.binary,
                    "--untrusted",  # sin shell-escape ni features inseguras
                    "--chatter", "minimal",
                    "--keep-logs",
                    "--outdir", str(workdir),
                    str(tex_path),
                ],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired as exc:
            raise LatexError(f"La compilación superó los {self.timeout}s") from exc

        pdf_path = workdir / f"{stem}.pdf"
        if result.returncode != 0 or not pdf_path.exists():
            tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-25:])
            raise LatexError(f"LaTeX falló:\n{tail}")
        return pdf_path


PREVIEW_SCALE = 2.0


def render_preview(pdf_path: Path, scale: float = PREVIEW_SCALE) -> bytes:
    """Primera página como PNG.

    Se genera en el servidor en vez de incrustar el PDF en un iframe: el visor nativo
    del navegador dibuja su propia barra de herramientas y su panel de miniaturas, y
    los parámetros `#toolbar=0` ya no se respetan. Una imagen se ve igual en todos lados.
    """
    import io

    import pypdfium2 as pdfium

    documento = pdfium.PdfDocument(str(pdf_path))
    try:
        imagen = documento[0].render(scale=scale).to_pil()
        buffer = io.BytesIO()
        imagen.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    finally:
        documento.close()


def page_count(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def pdf_text(pdf_path: Path) -> str:
    """Texto tal como lo leería un ATS."""
    reader = PdfReader(str(pdf_path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def ats_check(pdf_path: Path, final_cv: dict) -> list[str]:
    """Devuelve la lista de datos que NO se pudieron leer del PDF. Vacía es lo correcto."""
    extracted = _normalize(pdf_text(pdf_path))
    missing: list[str] = []

    def require(value: str, label: str) -> None:
        if not value:
            return
        needle = _normalize(value)
        # Se acepta si están todas las palabras: el extractor a veces mete saltos raros.
        if needle and not all(word in extracted for word in needle.split()):
            missing.append(f"{label}: {value}")

    require((final_cv.get("contact") or {}).get("full_name", ""), "Nombre")
    for entry in final_cv.get("experience") or []:
        require(entry.get("organization", ""), "Empresa")
        require(entry.get("dates", ""), "Fechas")
    for entry in final_cv.get("education") or []:
        require(entry.get("institution", ""), "Institución")
    return missing


def _trim_one_bullet(final_cv: dict) -> bool:
    """Saca un bullet del rol más antiguo que todavía tenga de sobra. False si no se puede."""
    for entry in reversed(final_cv.get("experience") or []):
        if len(entry.get("bullets") or []) > MIN_BULLETS_PER_ENTRY:
            entry["bullets"].pop()
            return True
    for entry in reversed(final_cv.get("extras") or []):
        if len(entry.get("details") or []) > 1:
            entry["details"].pop()
            return True
    return False


def compile_fitted(
    final_cv: dict,
    render_fn,
    workdir: Path,
    *,
    compiler: LatexCompiler | None = None,
    stem: str = "cv",
) -> dict:
    """Compila y, si hace falta, recorta hasta que entre en una página.

    Devuelve {tex, pdf_path, pages, trimmed, ats_missing}. `final_cv` se muta con los
    recortes aplicados, así que lo que se guarda y lo que se ve en el PDF coinciden.
    """
    compiler = compiler or TectonicCompiler()
    tex_source = render_fn(final_cv)
    pdf_path = compiler.compile(tex_source, workdir, stem)
    pages = page_count(pdf_path)
    trimmed = 0

    while pages > 1 and trimmed < MAX_FIT_ITERATIONS:
        if not _trim_one_bullet(final_cv):
            log.info("No queda nada recortable; el CV se queda en %d páginas", pages)
            break
        trimmed += 1
        tex_source = render_fn(final_cv)
        pdf_path = compiler.compile(tex_source, workdir, stem)
        pages = page_count(pdf_path)

    if trimmed:
        log.info("Ajuste a una página: %d bullets recortados, quedó en %d página(s)",
                 trimmed, pages)

    return {
        "tex": tex_source,
        "pdf_path": pdf_path,
        "preview": render_preview(pdf_path),
        "pages": pages,
        "trimmed": trimmed,
        "ats_missing": ats_check(pdf_path, final_cv),
    }
