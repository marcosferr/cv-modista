"""Escalera de recuperación de JSON. Costo cero de cupo.

Los free models rompen el JSON de tres maneras distintas y cada una tiene su arreglo:

- lo envuelven en fences markdown o en un bloque de razonamiento  -> se recorta
- le ponen prosa antes o después                                  -> escaneo de llaves balanceadas
- lo truncan a mitad de camino (`finish_reason: "length"`)        -> json-repair cierra lo abierto

Solo si los tres fallan se gasta una llamada de reparación, y nunca por truncamiento:
eso no se repara pidiendo de nuevo, se reintenta más corto o se rota de modelo.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import json_repair

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)(?:```|$)", re.DOTALL)
_THINK_RE = re.compile(r"<(think|thinking|reasoning)>.*?(</\1>|$)", re.DOTALL | re.IGNORECASE)


class JsonRecoveryError(ValueError):
    """La escalera completa falló: no hay ningún objeto JSON rescatable."""


def strip_wrappers(raw: str) -> str:
    """Quita bloques de razonamiento y fences markdown."""
    text = _THINK_RE.sub("", raw or "").strip()
    match = _FENCE_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text


def scan_balanced_object(text: str) -> str | None:
    """Devuelve el primer objeto JSON de nivel superior, respetando strings y escapes.

    Si nunca cierra (respuesta truncada) devuelve el resto del texto igual, para que
    json-repair pueda cerrar las estructuras abiertas.
    """
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    return text[start:]


def extract_json(raw: str) -> dict[str, Any]:
    """Recorre la escalera y devuelve un dict, o levanta JsonRecoveryError."""
    if not raw or not raw.strip():
        raise JsonRecoveryError("respuesta vacía")

    stripped = strip_wrappers(raw)
    candidates = [c for c in (scan_balanced_object(stripped), stripped, raw) if c]

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            pass
        else:
            if isinstance(parsed, dict):
                return parsed

    for candidate in candidates:
        try:
            parsed = json_repair.loads(candidate)
        except (ValueError, TypeError) as exc:  # pragma: no cover - json-repair casi nunca levanta
            log.debug("json-repair falló: %s", exc)
            continue
        if isinstance(parsed, dict) and parsed:
            log.info("JSON recuperado con json-repair (%d chars crudos)", len(raw))
            return parsed
        # A veces el modelo devuelve el objeto envuelto en una lista de un elemento.
        if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
            return parsed[0]

    raise JsonRecoveryError(f"sin objeto JSON rescatable en {len(raw)} chars")
