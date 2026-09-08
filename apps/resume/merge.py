"""Merge del patch del LLM sobre el CV parseado.

Los hechos ya están protegidos por diseño: el modelo solo emite bullets, nunca empresas,
cargos, fechas ni instituciones. Todo eso se copia verbatim del parse. Inventar un
empleador es imposible.

Lo que queda por vigilar es el riesgo real de un reescritor: **inventar métricas**.
"Reduje la latencia" convertido en "Reduje la latencia un 40%" es un dato falso en un CV.
Por cada bullet reescrito se extraen sus tokens numéricos y se exige que ya estuvieran
en la entrada original. Se avisa, no se bloquea: la decisión es del candidato.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

# Números con separadores de miles o decimales, con % opcional: 12.000  4,5  800  74%
_NUMBER_RE = re.compile(r"\d[\d.,]*\s*%?")
# Años sueltos: casi siempre vienen del contexto y no son una métrica inventada.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

SKILL_MATCH_THRESHOLD = 85

# Si el modelo pide descartar más de esta fracción de los extras, no es criterio
# editorial: se ignoran todos sus descartes.
MAX_DROP_RATIO = 0.5


def assign_ids(parsed: dict) -> dict:
    """Numera las entradas del parse. Estos ids son el contrato con el LLM."""
    for index, entry in enumerate(parsed.get("experience") or []):
        entry["id"] = f"exp{index}"
    for index, entry in enumerate(parsed.get("education") or []):
        entry["id"] = f"edu{index}"
    for index, entry in enumerate(parsed.get("extras") or []):
        entry["id"] = f"xtr{index}"
    return parsed


def normalize_number(token: str) -> str:
    """'12.000' '12,000' y '12000' colapsan al mismo token.

    Se normaliza agresivamente a propósito: acá los falsos positivos son peores que los
    falsos negativos, porque un usuario que ve avisos de más deja de leerlos.
    """
    return re.sub(r"[.,\s%]", "", token)


def numbers_in(text: str) -> set[str]:
    found = set()
    for match in _NUMBER_RE.findall(text or ""):
        normalized = normalize_number(match)
        if normalized and not _YEAR_RE.match(normalized):
            found.add(normalized)
    return found


def find_invented_numbers(new_bullet: str, source_texts: list[str]) -> list[str]:
    """Tokens numéricos del bullet nuevo que no aparecen en ninguna fuente original."""
    source_numbers: set[str] = set()
    for text in source_texts:
        source_numbers |= numbers_in(text)
    return sorted(numbers_in(new_bullet) - source_numbers)


def _matches_known_skill(candidate: str, known: list[str]) -> bool:
    """Match difuso: 'Postgres' cuenta como 'PostgreSQL', 'Python 3' como 'Python'."""
    return any(
        fuzz.token_set_ratio(candidate.lower(), item.lower()) >= SKILL_MATCH_THRESHOLD
        for item in known
    )


def _ordered_ids(patch: dict, prefix: str, fallback: list[dict]) -> list[str]:
    """Orden final de una sección.

    Solo las entradas que el modelo quiere CONSERVAR mandan en el orden. Si además se
    ignoró un descarte, esa entrada vuelve a su lugar original en vez de quedar
    promovida al principio: el modelo la mencionó para sacarla, no para destacarla.
    """
    priorizadas = [
        str(entry.get("id", ""))
        for entry in patch.get("entries") or []
        if str(entry.get("id", "")).startswith(prefix) and entry.get("keep", True)
    ]
    seen = set(priorizadas)
    resto = [e["id"] for e in fallback if e.get("id") and e["id"] not in seen]
    return priorizadas + resto


def merge_patch(parsed: dict, patch: dict, *, include_summary: bool = False) -> tuple[dict, list[dict]]:
    """Devuelve (cv_final, avisos). El cv_final es lo que consume la plantilla LaTeX."""
    parsed = assign_ids(parsed)
    warnings: list[dict] = []

    by_id = {
        entry["id"]: entry
        for group in ("experience", "education", "extras")
        for entry in (parsed.get(group) or [])
        if entry.get("id")
    }
    patch_by_id = {
        str(entry.get("id", "")): entry
        for entry in (patch.get("entries") or [])
        if entry.get("id")
    }

    def build(prefix: str, group: str, text_field: str, droppable: bool = True) -> list[dict]:
        entries = parsed.get(group) or []
        ids = _ordered_ids(patch, prefix, entries)

        # Un modelo flojo usa keep=false para acortar en vez de para editar. Visto en
        # producción: descartó 1 de 2 empleos y 6 de 7 extras, incluidas tres
        # certificaciones AWS, en una postulación a arquitecto de plataforma.
        pedidos = [i for i in ids if i in patch_by_id and not patch_by_id[i].get("keep", True)]
        honrar = droppable and entries and len(pedidos) <= max(1, int(len(entries) * MAX_DROP_RATIO))
        if pedidos and not honrar:
            warnings.append({
                "kind": "drops_ignored",
                "entry_id": prefix,
                "label": SECTION_LABELS.get(prefix, prefix),
                "message": (
                    f"El modelo quiso descartar {len(pedidos)} de {len(entries)} entradas; "
                    "se conservaron todas."
                    if droppable else
                    f"El modelo quiso descartar {len(pedidos)} entrada(s); en esta sección "
                    "no se descarta nada, porque un hueco sin explicar pesa más que una "
                    "línea poco relevante."
                ),
            })

        out = []
        for entry_id in ids:
            source = by_id.get(entry_id)
            if source is None:
                continue
            instruction = patch_by_id.get(entry_id, {})

            if honrar and instruction and not instruction.get("keep", True):
                warnings.append({
                    "kind": "dropped_entry",
                    "entry_id": entry_id,
                    "label": source.get("organization") or source.get("institution")
                    or source.get("title") or entry_id,
                    "message": "El modelo omitió esta entrada del CV final.",
                })
                continue

            original = list(source.get(text_field) or [])
            rewritten = [b for b in (instruction.get("bullets") or []) if b.strip()]

            if rewritten:
                for bullet in rewritten:
                    invented = find_invented_numbers(bullet, original)
                    if invented:
                        warnings.append({
                            "kind": "invented_number",
                            "entry_id": entry_id,
                            "label": source.get("organization") or source.get("institution")
                            or source.get("title") or entry_id,
                            "numbers": invented,
                            "text": bullet,
                            "message": f"Cifra sin respaldo en el CV original: {', '.join(invented)}",
                        })
                final_texts = rewritten
            else:
                final_texts = original

            merged = {k: v for k, v in source.items() if k != text_field}
            merged[text_field] = final_texts
            merged["original_" + text_field] = original
            out.append(merged)
        return out

    # La experiencia y la formación no se descartan nunca: un hueco laboral sin
    # explicar hace más daño que una entrada poco relevante, y el formato Harvard es
    # cronológico inverso, así que el hueco se ve. Lo irrelevante se acorta, no se borra.
    experience = build("exp", "experience", "bullets", droppable=False)
    education = build("edu", "education", "details", droppable=False)
    extras = build("xtr", "extras", "details", droppable=True)

    # Skills: solo se reordenan las del CV. Una que no matchea con ninguna original se
    # descarta, porque agregar una skill que el candidato no declaró es inventar.
    original_skills = list(parsed.get("skills") or [])
    ordered_skills: list[str] = []
    for skill in patch.get("skills") or []:
        if _matches_known_skill(skill, original_skills):
            if skill not in ordered_skills:
                ordered_skills.append(skill)
        else:
            warnings.append({
                "kind": "invented_skill",
                "entry_id": "skills",
                "label": "Skills",
                "text": skill,
                "message": f'"{skill}" no figura en el CV original; se descartó.',
            })
    for skill in original_skills:
        if not _matches_known_skill(skill, ordered_skills):
            ordered_skills.append(skill)

    final_cv = {
        "contact": parsed.get("contact") or {},
        "summary": (patch.get("summary") or "").strip() if include_summary else "",
        "experience": experience,
        "education": education,
        "extras": extras,
        "skills": ordered_skills,
    }
    return final_cv, warnings


SECTION_LABELS = {"exp": "Experiencia", "edu": "Formación", "xtr": "Liderazgo y actividades"}


def build_match_report(patch: dict, warnings: list[dict]) -> dict:
    matched = patch.get("matched_keywords") or []
    missing = patch.get("missing_requirements") or []
    total = len(matched) + len(missing)
    return {
        "matched": matched,
        "missing": missing,
        "score": round(100 * len(matched) / total) if total else 0,
        "flagged_numbers": sum(1 for w in warnings if w["kind"] == "invented_number"),
        "dropped_entries": sum(1 for w in warnings if w["kind"] == "dropped_entry"),
        "drops_ignored": sum(1 for w in warnings if w["kind"] == "drops_ignored"),
    }


def build_linkedin_pack(patch: dict) -> dict:
    return {
        "headline": (patch.get("headline") or "").strip(),
        "about": (patch.get("about") or "").strip(),
        "recruiter_message": (patch.get("recruiter_message") or "").strip(),
    }
