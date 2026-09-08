"""Prueba un modelo real con una sola llamada y vuelca la salida cruda.

Con OpenRouter free y su cupo diario no se puede iterar un prompt clickeando la UI.
Esto gasta exactamente una llamada y muestra lo único que importa cuando algo falla:
qué devolvió el modelo tal cual, y si cortó por `length`.
"""

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.llm import cost, quota, registry
from apps.llm.errors import LlmError
from apps.llm.parsing import JsonRecoveryError, extract_json
from apps.llm.providers import get_provider
from apps.llm.providers.base import Request

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "n": {"type": "integer"}},
    "required": ["ok", "n"],
    "additionalProperties": False,
}


class Command(BaseCommand):
    help = "Prueba un modelo con una llamada real. Respeta LLM_PROVIDER."

    def add_arguments(self, parser):
        parser.add_argument("--provider", choices=["openrouter", "bedrock"],
                            help="Por defecto, el de LLM_PROVIDER.")
        parser.add_argument("--model", help="Fuerza un modelo concreto.")
        parser.add_argument("--prompt", default='Devolvé este JSON exacto: {"ok": true, "n": 3}')
        parser.add_argument("--max-tokens", type=int, default=300)
        parser.add_argument("--no-schema", action="store_true",
                            help="Sin structured output, para ver qué emite en crudo.")
        parser.add_argument("--pool", action="store_true", help="Solo mostrar estado y salir")

    def handle(self, *args, **options):
        provider = get_provider(options.get("provider"))
        self.stdout.write(self.style.NOTICE(f"Proveedor: {provider.name}"))

        if options["pool"]:
            return self._estado(provider)

        if options["model"]:
            if provider.name == "bedrock":
                settings.BEDROCK_MODEL_ID = options["model"]
                settings.BEDROCK_FALLBACK_MODEL_ID = ""
            else:
                raise CommandError("--model solo aplica a bedrock; en openrouter rota el anillo.")

        request = Request(
            purpose="probe", system="Respondés solo con JSON.", user=options["prompt"],
            schema=None if options["no_schema"] else SCHEMA,
            schema_name="probe", max_tokens=options["max_tokens"], max_models=1,
        )
        try:
            attempt = next(provider.attempts(request))
        except LlmError as exc:
            raise CommandError(f"[{exc.kind}] {exc}") from exc
        except StopIteration as exc:
            raise CommandError("Ningún modelo respondió.") from exc

        self.stdout.write(f"Modelo: {attempt.model_id}")
        self.stdout.write(f"finish_reason: {attempt.finish_reason} | {attempt.latency_ms}ms")
        self.stdout.write(f"tokens: {attempt.prompt_tokens} in / {attempt.completion_tokens} out")
        if provider.name == "bedrock":
            usd = cost.cost_usd(attempt.model_id, attempt.prompt_tokens, attempt.completion_tokens)
            self.stdout.write(f"costo: USD {usd:.6f}")
        if attempt.notes:
            self.stdout.write(f"rotación: {attempt.notes}")

        if attempt.payload is not None:
            self.stdout.write(self.style.SUCCESS(
                f"\nEstructura garantizada (tool use): {json.dumps(attempt.payload, ensure_ascii=False)}"))
            return

        if attempt.finish_reason == "length":
            self.stdout.write(self.style.ERROR(
                "\nTruncado. No se repara: hay que acortar la entrada o rotar de modelo."))
        self.stdout.write(self.style.NOTICE("\n--- salida cruda ---"))
        self.stdout.write(attempt.raw or "(vacía)")
        try:
            self.stdout.write(self.style.SUCCESS(
                f"\nJSON recuperado: {json.dumps(extract_json(attempt.raw), ensure_ascii=False)[:400]}"))
        except JsonRecoveryError as exc:
            self.stdout.write(self.style.ERROR(f"\nJSON irrecuperable: {exc}"))

    def _estado(self, provider):
        if provider.name == "openrouter":
            for model in registry.describe_pool():
                estado = "sano" if model["healthy"] else f"cooldown {model['cooldown']}s"
                marcas = " ".join(filter(None, [
                    "structured" if model["structured"] else "-",
                    "TINY" if model.get("tiny") else "",
                ]))
                self.stdout.write(f"{model['id']:<50} ctx={model['context']:>8} "
                                  f"{marcas:<18} {estado}")
            self.stdout.write(self.style.NOTICE(f"\nCupo: {quota.status()}"))
        else:
            self.stdout.write(f"Modelos: {' -> '.join(provider.models())}")
            for model_id in provider.models():
                precio_in, precio_out = cost.price_for(model_id)
                self.stdout.write(f"  {model_id:<28} USD {precio_in}/1k in · {precio_out}/1k out")
            self.stdout.write(self.style.NOTICE(f"\nPresupuesto: {cost.status()}"))
