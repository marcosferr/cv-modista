"""Selección del proveedor de LLM."""

from __future__ import annotations

from functools import lru_cache

from django.conf import settings

from apps.llm.providers.base import Attempt, Provider, Request

PROVIDERS = {"openrouter", "bedrock"}


@lru_cache(maxsize=4)
def _build(name: str) -> Provider:
    if name == "bedrock":
        from apps.llm.providers.bedrock import BedrockProvider

        return BedrockProvider()
    from apps.llm.providers.openrouter import OpenRouterProvider

    return OpenRouterProvider()


def get_provider(name: str | None = None) -> Provider:
    chosen = (name or settings.LLM_PROVIDER or "openrouter").lower()
    if chosen not in PROVIDERS:
        raise ValueError(f"LLM_PROVIDER desconocido: {chosen}. Opciones: {sorted(PROVIDERS)}")
    return _build(chosen)


__all__ = ["Attempt", "Provider", "Request", "get_provider", "PROVIDERS"]
