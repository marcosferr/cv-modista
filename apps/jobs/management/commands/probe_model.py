"""Pega una vez contra un modelo real y vuelca la salida cruda.

Con 50 llamadas diarias no se puede iterar un prompt clickeando la UI. Esto gasta
exactamente una llamada y muestra lo único que importa cuando algo falla: qué devolvió
el modelo tal cual, y si cortó por `length`.
"""

import json

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.llm import quota, registry
from apps.llm.parsing import JsonRecoveryError, extract_json


class Command(BaseCommand):
    help = "Prueba un modelo :free de OpenRouter con una sola llamada."

    def add_arguments(self, parser):
        parser.add_argument("--model", help="Id del modelo. Por defecto, el primero sano.")
        parser.add_argument("--prompt", default='Devolvé este JSON exacto: {"ok": true, "n": 3}')
        parser.add_argument("--max-tokens", type=int, default=300)
        parser.add_argument("--pool", action="store_true", help="Solo listar el pool y salir")

    def handle(self, *args, **options):
        if options["pool"]:
            for model in registry.describe_pool():
                estado = "sano" if model["healthy"] else f"cooldown {model['cooldown']}s"
                marca = "structured" if model["structured"] else "-"
                self.stdout.write(f"{model['id']:<50} ctx={model['context']:>8} {marca:<11} {estado}")
            self.stdout.write(self.style.NOTICE(f"\nCupo: {quota.status()}"))
            return

        if not settings.OPENROUTER_API_KEY:
            raise CommandError("Falta OPENROUTER_API_KEY en el .env")

        model_id = options["model"] or registry.candidates()[0]["id"]
        self.stdout.write(f"Modelo: {model_id}")

        response = httpx.post(
            f"{settings.OPENROUTER_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENROUTER_API_KEY}"},
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": options["prompt"]}],
                "temperature": 0,
                "max_tokens": options["max_tokens"],
            },
            timeout=settings.OPENROUTER_TIMEOUT,
        )
        self.stdout.write(f"HTTP {response.status_code}")
        if response.status_code != 200:
            raise CommandError(response.text[:600])

        body = response.json()
        choice = body["choices"][0]
        raw = (choice.get("message") or {}).get("content") or ""
        finish = choice.get("finish_reason")

        self.stdout.write(f"finish_reason: {finish}")
        self.stdout.write(f"usage: {json.dumps(body.get('usage') or {})}")
        self.stdout.write(self.style.NOTICE("\n--- salida cruda ---"))
        self.stdout.write(raw or "(vacía)")

        if finish == "length":
            self.stdout.write(self.style.ERROR(
                "\nTruncado. Esto no se repara: hay que bajar max_tokens de entrada o rotar."))
        try:
            self.stdout.write(self.style.SUCCESS(
                f"\nJSON recuperado: {json.dumps(extract_json(raw), ensure_ascii=False)[:400]}"))
        except JsonRecoveryError as exc:
            self.stdout.write(self.style.ERROR(f"\nJSON irrecuperable: {exc}"))
