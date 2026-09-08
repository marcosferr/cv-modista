"""Prompts de las dos llamadas del pipeline.

PROMPT_VERSION forma parte de la clave de caché: subirla invalida todo lo cacheado.
Súbila cada vez que edites un prompt de acá.
"""

from __future__ import annotations

import json
import re

PROMPT_VERSION = "v1"

# Bloques que no aportan señal y sí muchos tokens en un aviso de LinkedIn.
_BOILERPLATE_PATTERNS = [
    r"(?is)\b(about us|about the company|sobre nosotros|quiénes somos|quienes somos)\b.{0,1200}",
    r"(?is)\b(equal opportunity|EEO statement|igualdad de oportunidades)\b.{0,900}",
    r"(?is)\b(benefits|beneficios|what we offer|qué ofrecemos|que ofrecemos|perks)\b.{0,900}",
    r"(?is)\b(our mission|nuestra misión|nuestra mision|our values|nuestros valores)\b.{0,900}",
    r"(?is)\b(how to apply|cómo aplicar|como aplicar|application process)\b.{0,600}",
]

LANGUAGE_NAMES = {"es": "español", "en": "inglés"}


def strip_boilerplate(text: str, max_chars: int = 9000) -> str:
    """Recorta relleno corporativo del aviso. Menos tokens y mejor señal."""
    cleaned = text or ""
    for pattern in _BOILERPLATE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rsplit("\n", 1)[0] + "\n[...recortado...]"
    return cleaned


def language_clause(language: str, job_description: str) -> str:
    if language in LANGUAGE_NAMES:
        return f"Escribí TODO el contenido en {LANGUAGE_NAMES[language]}."
    return (
        "Escribí TODO el contenido en el mismo idioma en el que está redactada la "
        "descripción del puesto."
    )


# --- llamada 1: parse -------------------------------------------------------

PARSE_SYSTEM = """Extraés datos estructurados de currículums. Devolvés únicamente un objeto JSON.

Reglas:
- Transcribí lo que dice el CV. No inventes, no completes huecos, no embellezcas.
- Si un dato no está, usá string vacío o lista vacía. Nunca "N/A", nunca null.
- Conservá el idioma original del CV.
- Cada logro o responsabilidad va como un elemento separado de "bullets".
- Ordená experiencia y educación de lo más reciente a lo más antiguo.
- No agregues texto fuera del JSON."""

PARSE_EXAMPLE = json.dumps(
    {
        "contact": {
            "full_name": "Ana Gómez",
            "email": "ana@example.com",
            "phone": "+595 981 000000",
            "location": "Asunción, Paraguay",
            "linkedin": "linkedin.com/in/anagomez",
        },
        "education": [
            {
                "institution": "Universidad Nacional de Asunción",
                "degree": "Ingeniería Informática",
                "location": "Asunción, Paraguay",
                "dates": "2014 - 2019",
                "details": ["Promedio 4.2/5"],
            }
        ],
        "experience": [
            {
                "organization": "Acme S.A.",
                "title": "Desarrolladora Backend Senior",
                "location": "Asunción, Paraguay",
                "dates": "Ene 2021 - Presente",
                "bullets": [
                    "Diseñé la API de pagos que procesa 12.000 transacciones diarias",
                    "Reduje el tiempo de respuesta del checkout de 800 ms a 210 ms",
                ],
            }
        ],
        "extras": [
            {
                "title": "Mentora",
                "organization": "PyLadies Paraguay",
                "dates": "2022 - 2023",
                "details": ["Mentoreé a 15 desarrolladoras junior"],
            }
        ],
        "skills": ["Python", "Django", "PostgreSQL", "Docker", "Inglés C1"],
    },
    ensure_ascii=False,
    indent=2,
)


def parse_user_prompt(cv_text: str) -> str:
    return f"""Extraé el contenido de este CV al JSON con esta forma exacta:

{PARSE_EXAMPLE}

Respondé solo con el objeto JSON.

--- CV ---
{cv_text}
--- FIN DEL CV ---"""


# --- llamada 2: tailor ------------------------------------------------------

TAILOR_SYSTEM = """Adaptás currículums a una oferta concreta. Devolvés únicamente un objeto JSON.

Nunca emitís nombres de empresa, cargos, fechas ni instituciones: esos datos se copian
tal cual del CV original. Vos solo reescribís bullets y textos.

Reglas innegociables:
- NO inventes cifras. Si el bullet original no trae un número, el reescrito tampoco.
  Podés reformular un número que ya está, jamás agregar uno nuevo.
- NO inventes tecnologías, herramientas, clientes ni responsabilidades que no estén en
  el CV original.
- Reescribí en voz activa, empezando por un verbo de acción, sin pronombres
  ("Lideré la migración...", no "Yo fui responsable de...").
- Máximo 5 bullets por entrada, de una a dos líneas cada uno.
- Priorizá lo que la oferta pide de verdad. Lo irrelevante se acorta o se marca keep=false.
- "matched_keywords" son requisitos de la oferta que el CV respalda con evidencia real.
  "missing_requirements" son los que no. Sé honesto en ambos: sirven para que el
  candidato sepa dónde está parado.
- No agregues texto fuera del JSON."""

TAILOR_EXAMPLE = json.dumps(
    {
        "entries": [
            {
                "id": "exp0",
                "keep": True,
                "bullets": [
                    "Diseñé la API de pagos que procesa 12.000 transacciones diarias",
                    "Reduje la latencia del checkout de 800 ms a 210 ms",
                ],
            },
            {"id": "exp1", "keep": False, "bullets": []},
            {"id": "edu0", "keep": True, "bullets": []},
        ],
        "skills": ["Python", "Django", "PostgreSQL"],
        "matched_keywords": ["Python", "APIs REST", "PostgreSQL"],
        "missing_requirements": ["Kubernetes", "5 años en fintech"],
        "summary": "Desarrolladora backend con 6 años construyendo sistemas de pagos.",
        "headline": "Backend Engineer | Python & Django | Sistemas de pagos a escala",
        "about": "Construyo backends de pagos...",
        "recruiter_message": "Hola Juan, vi la búsqueda de Backend Engineer...",
    },
    ensure_ascii=False,
    indent=2,
)


def render_cv_outline(parsed: dict) -> str:
    """Representación compacta del CV con los ids que el patch debe referenciar."""
    lines: list[str] = []

    for index, entry in enumerate(parsed.get("experience", [])):
        header = " — ".join(p for p in (entry.get("organization"), entry.get("title")) if p)
        lines.append(f"exp{index} | {header} | {entry.get('dates', '')}")
        for bullet in entry.get("bullets", []):
            lines.append(f"    - {bullet}")

    for index, entry in enumerate(parsed.get("education", [])):
        header = " — ".join(p for p in (entry.get("institution"), entry.get("degree")) if p)
        lines.append(f"edu{index} | {header} | {entry.get('dates', '')}")
        for detail in entry.get("details", []):
            lines.append(f"    - {detail}")

    for index, entry in enumerate(parsed.get("extras", [])):
        header = " — ".join(p for p in (entry.get("title"), entry.get("organization")) if p)
        lines.append(f"xtr{index} | {header} | {entry.get('dates', '')}")
        for detail in entry.get("details", []):
            lines.append(f"    - {detail}")

    skills = ", ".join(parsed.get("skills", []))
    if skills:
        lines.append(f"skills | {skills}")

    return "\n".join(lines)


def tailor_user_prompt(
    *,
    parsed: dict,
    target_role: str,
    job_description: str,
    language: str,
    include_summary: bool,
) -> str:
    summary_clause = (
        'Escribí "summary" con un resumen profesional de 2 frases.'
        if include_summary
        else 'Dejá "summary" como string vacío.'
    )
    return f"""Puesto al que se aplica: {target_role}

--- DESCRIPCIÓN DEL PUESTO ---
{strip_boilerplate(job_description)}
--- FIN DE LA DESCRIPCIÓN ---

--- CV DEL CANDIDATO (cada entrada con su id) ---
{render_cv_outline(parsed)}
--- FIN DEL CV ---

Devolvé un objeto JSON con esta forma exacta:

{TAILOR_EXAMPLE}

Instrucciones:
- Incluí en "entries" una entrada por cada id de arriba (exp*, edu*, xtr*), en el orden
  en que deben aparecer en el CV, lo más relevante primero.
- "bullets" vacío conserva los bullets originales sin tocar. Usalo cuando ya están bien.
- keep=false omite esa entrada del CV final. Usalo solo para lo irrelevante, nunca para
  tapar un hueco en el historial laboral.
- Para las entradas edu* solo podés reescribir sus detalles, y casi siempre conviene
  dejarlas con "bullets" vacío.
- "skills" son las del CV original reordenadas por relevancia. No agregues ninguna nueva.
- {summary_clause}
- {language_clause(language, job_description)}

Respondé solo con el objeto JSON."""


# --- reparación (como máximo una por job) -----------------------------------

REPAIR_SYSTEM = """Arreglás JSON roto. Devolvés únicamente el objeto JSON corregido,
con exactamente el mismo contenido, sin agregar ni quitar datos. Nada de texto extra."""


def repair_user_prompt(broken: str, error: str) -> str:
    fragment = broken[:6000]
    return f"""Este texto tenía que ser un objeto JSON válido pero falló al parsearse.

Error: {error}

--- TEXTO ---
{fragment}
--- FIN ---

Devolvé solo el JSON corregido."""
