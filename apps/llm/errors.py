"""Errores compartidos por los proveedores de LLM."""

from __future__ import annotations

# Errores que no se arreglan rotando de modelo ni reintentando.
FATAL_KINDS = {"no_key", "quota_exhausted", "budget_exhausted"}


class LlmError(RuntimeError):
    """Fallo de transporte o de política: HTTP, credenciales, cupo, presupuesto."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.fatal = kind in FATAL_KINDS


class ContentRejected(Exception):
    """El modelo respondió, pero lo que devolvió no sirve.

    Es distinto de LlmError a propósito: acá la llamada salió bien y se pagó, lo que
    falla es el contenido. El proveedor lo trata como señal de rotar a otro modelo.

    kinds: truncated | bad_json | copied_example | schema
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
