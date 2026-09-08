"""Render de JSON a LaTeX.

El LLM nunca escribe LaTeX. Emite JSON y esta capa lo pasa a .tex con escapado estricto.
Eso mata de una dos problemas a la vez: la inyección de LaTeX desde texto del usuario y
el clásico "el modelo generó LaTeX que no compila".
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import jinja2

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Se reemplaza en UNA sola pasada con una regex, no con replace() encadenados.
# Encadenados hay un bug clásico: "\\" -> "\textbackslash{}" mete llaves nuevas, y el
# reemplazo posterior de "{" y "}" las vuelve a escapar, dando "\textbackslash\{\}".
_TEX_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_TEX_RE = re.compile("|".join(re.escape(char) for char in _TEX_MAP))

# Tipografía que los procesadores de texto meten y pdflatex no digiere.
_UNICODE_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "---", "−": "-", "‐": "-", "‑": "-",
    "…": "...", "•": "-", "·": "-", "●": "-", "▪": "-",
    " ": " ", " ": " ", " ": " ", "​": "", "﻿": "",
    "→": "->", "←": "<-", "≤": "<=", "≥": ">=",
    # Ligaduras tipográficas: un ATS que busca "planificación" no encuentra "planiﬁcación".
    "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "ft", "ﬆ": "st",
}


def normalize_unicode(text: str) -> str:
    """Normaliza tipografía y saca lo que ningún motor de LaTeX puede componer."""
    text = unicodedata.normalize("NFC", text or "")
    for source, target in _UNICODE_MAP.items():
        text = text.replace(source, target)
    # Se descartan emoji, símbolos sueltos y no asignados. Se conservan los acentos
    # latinos, que con T1 + inputenc funcionan en los tres motores.
    cleaned = "".join(ch for ch in text if unicodedata.category(ch) not in {"So", "Cn", "Cs", "Co"})
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def escape_tex(value) -> str:
    if value is None:
        return ""
    text = normalize_unicode(str(value))
    return _TEX_RE.sub(lambda match: _TEX_MAP[match.group()], text)


def escape_url(value) -> str:
    """Para el primer argumento de \\href: ahí solo hay que proteger % y #."""
    if not value:
        return ""
    url = normalize_unicode(str(value)).strip()
    if url and not url.startswith(("http://", "https://", "mailto:")):
        url = "https://" + url.lstrip("/")
    return url.replace("%", r"\%").replace("#", r"\#")


@lru_cache(maxsize=1)
def get_env() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
        undefined=jinja2.ChainableUndefined,
    )
    env.filters["tex"] = escape_tex
    env.filters["url"] = escape_url
    return env


def contact_parts(contact: dict) -> list[dict]:
    """Línea de contacto: solo lo que existe, sin foto ni datos personales de más."""
    parts: list[dict] = []
    if contact.get("location"):
        parts.append({"text": contact["location"], "href": ""})
    if contact.get("phone"):
        parts.append({"text": contact["phone"], "href": ""})
    if contact.get("email"):
        parts.append({"text": contact["email"], "href": f"mailto:{contact['email']}"})
    if contact.get("linkedin"):
        handle = re.sub(r"^https?://(www\.)?", "", str(contact["linkedin"])).rstrip("/")
        parts.append({"text": handle, "href": contact["linkedin"]})
    return parts


def render_tex(final_cv: dict, *, language: str = "es") -> str:
    """JSON del CV -> fuente LaTeX lista para compilar."""
    titles = titles_for(language)
    template = get_env().get_template("cv.tex.j2")
    return template.render(
        cv=final_cv,
        contact=final_cv.get("contact") or {},
        contact_parts=contact_parts(final_cv.get("contact") or {}),
        titles=titles,
    )


SECTION_TITLES = {
    "es": {
        "summary": "Perfil Profesional",
        "education": "Formación Académica",
        "experience": "Experiencia Profesional",
        "extras": "Liderazgo y Actividades",
        "skills": "Habilidades e Idiomas",
    },
    "en": {
        "summary": "Summary",
        "education": "Education",
        "experience": "Experience",
        "extras": "Leadership & Activities",
        "skills": "Skills & Interests",
    },
}
DEFAULT_TITLES = SECTION_TITLES["es"]


def titles_for(language: str) -> dict:
    """Los títulos de sección siguen al idioma del CV, no al de la interfaz."""
    return SECTION_TITLES.get(language, DEFAULT_TITLES)
