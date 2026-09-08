"""Schemas de entrada/salida del LLM.

Dos reglas que vienen del hecho de trabajar con free models flojos:

1. **Todo campo tiene default.** Un campo que el modelo omite o emite mal se descarta,
   no tumba el job.
2. **El orden de los campos importa.** Si la respuesta se trunca, `json-repair` cierra
   las estructuras abiertas y los campos que faltan caen a su default. Por eso lo
   crítico (los bullets del CV) va primero y el pack de LinkedIn al final.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

# --- coerciones permisivas --------------------------------------------------

_TEXT_KEYS = ("text", "bullet", "value", "name", "description", "item", "skill")


def to_str(v: Any) -> str:
    """Cualquier cosa -> string. Los modelos flojos devuelven listas y dicts donde va texto."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        return ""
    if isinstance(v, int | float):
        return str(v)
    if isinstance(v, list):
        return " ".join(to_str(x) for x in v if x is not None).strip()
    if isinstance(v, dict):
        for key in _TEXT_KEYS:
            if key in v:
                return to_str(v[key])
        return ""
    return str(v).strip()


def to_str_list(v: Any) -> list[str]:
    """Cualquier cosa -> list[str], aplanando dicts envoltorio y strings multilínea."""
    if v is None:
        return []
    if isinstance(v, str):
        parts = [p.strip(" \t-•*") for p in v.splitlines()]
        return [p for p in parts if p]
    if isinstance(v, dict):
        for key in _TEXT_KEYS:
            if key in v:
                return to_str_list(v[key])
        return []
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            text = to_str(item)
            if text:
                out.append(text)
        return out
    return []


def to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in {"false", "no", "0", "drop", "remove", ""}
    if v is None:
        return True
    return bool(v)


class Permissive(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# --- llamada 1: parse del CV original ---------------------------------------


class Contact(Permissive):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""

    _s = field_validator("*", mode="before")(lambda v: to_str(v))


class Education(Permissive):
    institution: str = ""
    degree: str = ""
    location: str = ""
    dates: str = ""
    details: list[str] = []

    _s = field_validator("institution", "degree", "location", "dates", mode="before")(
        lambda v: to_str(v)
    )
    _l = field_validator("details", mode="before")(lambda v: to_str_list(v))


class Experience(Permissive):
    organization: str = ""
    title: str = ""
    location: str = ""
    dates: str = ""
    bullets: list[str] = []

    _s = field_validator("organization", "title", "location", "dates", mode="before")(
        lambda v: to_str(v)
    )
    _l = field_validator("bullets", mode="before")(lambda v: to_str_list(v))


class Extra(Permissive):
    """Leadership & Activities: voluntariado, proyectos, certificaciones, comunidad."""

    title: str = ""
    organization: str = ""
    dates: str = ""
    details: list[str] = []

    _s = field_validator("title", "organization", "dates", mode="before")(lambda v: to_str(v))
    _l = field_validator("details", mode="before")(lambda v: to_str_list(v))


class ParsedCv(Permissive):
    contact: Contact = Contact()
    education: list[Education] = []
    experience: list[Experience] = []
    extras: list[Extra] = []
    skills: list[str] = []

    _l = field_validator("skills", mode="before")(lambda v: to_str_list(v))

    @field_validator("contact", mode="before")
    @classmethod
    def _contact(cls, v: Any) -> Any:
        return v if isinstance(v, dict) else {}

    @field_validator("education", "experience", "extras", mode="before")
    @classmethod
    def _entries(cls, v: Any) -> list[Any]:
        if isinstance(v, dict):
            v = [v]
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]


# --- llamada 2: patch de adaptación -----------------------------------------


class EntryPatch(Permissive):
    """Reescritura de una entrada. `id` referencia un id asignado por Python en el parse.

    El modelo nunca emite empresa, cargo, fechas ni institución: solo bullets.
    Inventar un empleador es estructuralmente imposible.
    """

    id: str = ""
    keep: bool = True
    bullets: list[str] = []

    _s = field_validator("id", mode="before")(lambda v: to_str(v))
    _b = field_validator("keep", mode="before")(lambda v: to_bool(v))
    _l = field_validator("bullets", mode="before")(lambda v: to_str_list(v))


class TailorPatch(Permissive):
    entries: list[EntryPatch] = []
    skills: list[str] = []
    matched_keywords: list[str] = []
    missing_requirements: list[str] = []
    summary: str = ""
    headline: str = ""
    about: str = ""
    recruiter_message: str = ""

    _l = field_validator("skills", "matched_keywords", "missing_requirements", mode="before")(
        lambda v: to_str_list(v)
    )
    _s = field_validator("summary", "headline", "about", "recruiter_message", mode="before")(
        lambda v: to_str(v)
    )

    @field_validator("entries", mode="before")
    @classmethod
    def _entries(cls, v: Any) -> list[Any]:
        if isinstance(v, dict):
            v = [v]
        if not isinstance(v, list):
            return []
        return [item for item in v if isinstance(item, dict)]


# --- JSON Schemas para response_format --------------------------------------
# Escritos a mano en vez de con model_json_schema(): Pydantic genera $defs y anyOf,
# que los proveedores de structured output rechazan en modo strict.


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _str_array(desc: str) -> dict:
    return {"type": "array", "items": {"type": "string"}, "description": desc}


def _obj(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


PARSED_CV_SCHEMA = _obj(
    {
        "contact": _obj(
            {
                "full_name": _str("Nombre completo tal cual figura en el CV"),
                "email": _str("Email, o string vacío"),
                "phone": _str("Teléfono, o string vacío"),
                "location": _str("Ciudad y país, o string vacío"),
                "linkedin": _str("URL o handle de LinkedIn, o string vacío"),
            }
        ),
        "education": {
            "type": "array",
            "description": "Formación académica, de la más reciente a la más antigua",
            "items": _obj(
                {
                    "institution": _str("Nombre de la institución"),
                    "degree": _str("Título y campo de estudio"),
                    "location": _str("Ciudad y país"),
                    "dates": _str("Rango de fechas, ej '2018 - 2022'"),
                    "details": _str_array("Honores, promedio, tesis. Vacío si no hay"),
                }
            ),
        },
        "experience": {
            "type": "array",
            "description": "Experiencia laboral, de la más reciente a la más antigua",
            "items": _obj(
                {
                    "organization": _str("Nombre de la empresa u organización"),
                    "title": _str("Cargo"),
                    "location": _str("Ciudad y país"),
                    "dates": _str("Rango de fechas, ej 'Ene 2020 - Presente'"),
                    "bullets": _str_array("Cada logro o responsabilidad como una frase"),
                }
            ),
        },
        "extras": {
            "type": "array",
            "description": "Voluntariado, proyectos, certificaciones, comunidad",
            "items": _obj(
                {
                    "title": _str("Rol, nombre del proyecto o certificación"),
                    "organization": _str("Organización emisora"),
                    "dates": _str("Rango de fechas"),
                    "details": _str_array("Detalles breves"),
                }
            ),
        },
        "skills": _str_array("Habilidades técnicas, herramientas e idiomas"),
    }
)

TAILOR_PATCH_SCHEMA = _obj(
    {
        "entries": {
            "type": "array",
            "description": "Una por cada entrada del CV, en el orden en que debe aparecer",
            "items": _obj(
                {
                    "id": _str("El id exacto de la entrada, ej 'exp0'"),
                    "keep": {
                        "type": "boolean",
                        "description": "false para omitir esta entrada del CV final",
                    },
                    "bullets": _str_array(
                        "Bullets reescritos, en orden. Vacío conserva los originales"
                    ),
                }
            ),
        },
        "skills": _str_array("Skills del CV original, reordenadas por relevancia a la oferta"),
        "matched_keywords": _str_array("Requisitos de la oferta que el candidato sí cubre"),
        "missing_requirements": _str_array("Requisitos de la oferta que el candidato no cubre"),
        "summary": _str("Resumen profesional de 2 frases, o string vacío"),
        "headline": _str("Titular de LinkedIn, máximo 220 caracteres"),
        "about": _str("Sección Acerca de de LinkedIn, 3 a 4 frases"),
        "recruiter_message": _str("Mensaje al recruiter, máximo 90 palabras"),
    }
)
